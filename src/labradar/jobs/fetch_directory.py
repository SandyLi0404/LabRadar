"""Seed job: the department directory and the eleven area pages.

Writes the identity layer. Everything is tied to one ``fetch_run``, and existing
rows are closed out rather than overwritten, so the previous state stays
queryable for change detection.

A professor who disappears from the directory is marked ``is_current = False``.
They are never deleted -- "who left" is one of the questions this project
exists to answer.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from labradar.config import get_settings
from labradar.db.models import (
    Faculty,
    FacultyArea,
    FacultyInterestTerm,
    FacultyVersion,
    FetchRun,
    ResearchArea,
)
from labradar.sources.columbia import (
    FACULTY_URL,
    RESEARCH_AREAS,
    TEACHING_TRACK,
    FacultyRecord,
    area_url,
    parse_area_page,
    parse_faculty_directory,
)

log = logging.getLogger(__name__)

SOURCE = "columbia_directory"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_research_areas(session: Session) -> dict[str, ResearchArea]:
    """Ensure the eleven canonical areas exist, and return them by slug."""
    existing = {a.slug: a for a in session.scalars(select(ResearchArea))}
    for slug, name in RESEARCH_AREAS.items():
        if slug not in existing:
            area = ResearchArea(slug=slug, name=name, url=area_url(slug))
            session.add(area)
            existing[slug] = area
    session.flush()
    return existing


def fetch_pages(client: httpx.Client, delay: float) -> tuple[str, dict[str, str]]:
    """Fetch the directory and every area page, pausing between requests."""
    directory = client.get(FACULTY_URL)
    directory.raise_for_status()

    areas: dict[str, str] = {}
    for slug in RESEARCH_AREAS:
        time.sleep(delay)
        response = client.get(area_url(slug))
        if response.status_code != 200:
            log.warning("area page %s returned %s; skipping", slug, response.status_code)
            continue
        areas[slug] = response.text

    return directory.text, areas


def _close_current(session: Session, model, faculty_id: int, at: datetime) -> None:
    rows = session.scalars(
        select(model).where(model.faculty_id == faculty_id, model.valid_to.is_(None))
    )
    for row in rows:
        row.valid_to = at


def upsert_faculty(
    session: Session, record: FacultyRecord, run: FetchRun, at: datetime
) -> tuple[Faculty, bool]:
    """Insert or update one directory entry. Returns (faculty, version_written).

    A new version row is written only when the content hash moves, so an
    unchanged re-run leaves the history untouched.
    """
    faculty = session.scalar(
        select(Faculty).where(Faculty.person_id == record.person_id)
    )
    if faculty is None:
        faculty = Faculty(
            person_id=record.person_id,
            slug=record.slug,
            full_name=record.full_name,
            first_seen_run=run.id,
            last_seen_run=run.id,
            is_current=True,
        )
        session.add(faculty)
        session.flush()
    else:
        faculty.full_name = record.full_name
        faculty.slug = record.slug
        faculty.last_seen_run = run.id
        faculty.is_current = True

    current = session.scalar(
        select(FacultyVersion).where(
            FacultyVersion.faculty_id == faculty.id,
            FacultyVersion.valid_to.is_(None),
        )
    )
    if current is not None and current.content_hash == record.content_hash:
        return faculty, False

    if current is not None:
        current.valid_to = at

    session.add(
        FacultyVersion(
            faculty_id=faculty.id,
            fetch_run_id=run.id,
            title=record.title,
            email=record.email,
            office=record.office,
            phone=record.phone,
            homepage_url=record.homepage_url,
            scholar_url=record.scholar_url,
            cv_url=record.cv_url,
            interests_raw=record.interests_raw,
            content_hash=record.content_hash,
            valid_from=at,
        )
    )
    return faculty, True


def sync_interest_terms(
    session: Session, faculty: Faculty, record: FacultyRecord, run: FetchRun, at: datetime
) -> None:
    """Replace the current term set when the interests text has changed."""
    wanted = {t.lower(): t for t in record.interest_terms}
    current = {
        row.term_norm: row
        for row in session.scalars(
            select(FacultyInterestTerm).where(
                FacultyInterestTerm.faculty_id == faculty.id,
                FacultyInterestTerm.valid_to.is_(None),
            )
        )
    }

    for norm, row in current.items():
        if norm not in wanted:
            row.valid_to = at

    for norm, raw in wanted.items():
        if norm not in current:
            session.add(
                FacultyInterestTerm(
                    faculty_id=faculty.id,
                    term_raw=raw,
                    term_norm=norm,
                    fetch_run_id=run.id,
                    valid_from=at,
                )
            )


def sync_area_memberships(
    session: Session,
    memberships: dict[str, list[str]],
    areas: dict[str, ResearchArea],
    faculty_by_person: dict[str, Faculty],
    run: FetchRun,
    at: datetime,
) -> dict[str, int]:
    """Apply area rosters, closing memberships that have gone away.

    Area pages list cross-listed and affiliated people who have no directory
    entry -- they have no faculty row to attach to, so they are counted and
    reported in the run stats rather than silently dropped.
    """
    stats = {"area_links_added": 0, "area_links_closed": 0, "area_members_unknown": 0}

    wanted: set[tuple[int, int]] = set()
    for slug, person_ids in memberships.items():
        area = areas.get(slug)
        if area is None:
            continue
        for person_id in person_ids:
            faculty = faculty_by_person.get(person_id)
            if faculty is None:
                stats["area_members_unknown"] += 1
                continue
            wanted.add((faculty.id, area.id))

    current_rows = session.scalars(
        select(FacultyArea).where(
            FacultyArea.valid_to.is_(None), FacultyArea.source == "area_page"
        )
    ).all()
    current = {(row.faculty_id, row.research_area_id): row for row in current_rows}

    for key, row in current.items():
        if key not in wanted:
            row.valid_to = at
            stats["area_links_closed"] += 1

    for faculty_id, area_id in sorted(wanted - set(current)):
        session.add(
            FacultyArea(
                faculty_id=faculty_id,
                research_area_id=area_id,
                source="area_page",
                fetch_run_id=run.id,
                valid_from=at,
            )
        )
        stats["area_links_added"] += 1

    return stats


def run(session: Session, client: httpx.Client | None = None) -> FetchRun:
    """Fetch and store the directory plus area rosters under one run."""
    settings = get_settings()
    at = _now()
    fetch_run = FetchRun(source=SOURCE, started_at=at, status="running")
    session.add(fetch_run)
    session.flush()

    owns_client = client is None
    client = client or httpx.Client(
        timeout=30.0, headers={"User-Agent": settings.user_agent}
    )

    try:
        directory_html, area_html = fetch_pages(client, settings.request_delay_seconds)
        parsed = parse_faculty_directory(directory_html)
        # Teaching-track appointments are dropped at ingest, not filtered later.
        # The department directory's title is authoritative, and this project
        # covers research groups only, so a lecturer never enters the database.
        records = [r for r in parsed if r.appointment_track != TEACHING_TRACK]
        excluded = len(parsed) - len(records)
        if not records:
            raise RuntimeError(
                "directory parsed to zero faculty -- the page markup has probably "
                "changed; refusing to mark everyone as departed"
            )

        areas = seed_research_areas(session)
        stats = {
            "faculty_seen": len(records),
            "teaching_track_excluded": excluded,
            "versions_written": 0,
        }

        faculty_by_person: dict[str, Faculty] = {}
        for record in records:
            faculty, wrote_version = upsert_faculty(session, record, fetch_run, at)
            faculty_by_person[record.person_id] = faculty
            if wrote_version:
                stats["versions_written"] += 1
                sync_interest_terms(session, faculty, record, fetch_run, at)

        session.flush()

        memberships = {
            slug: parse_area_page(html, slug).person_ids
            for slug, html in area_html.items()
        }
        stats.update(
            sync_area_memberships(
                session, memberships, areas, faculty_by_person, fetch_run, at
            )
        )
        stats["areas_fetched"] = len(area_html)

        # Anyone absent from this run has left the directory. Mark, never delete.
        seen_ids = {r.person_id for r in records}
        departed = 0
        for faculty in session.scalars(select(Faculty).where(Faculty.is_current)):
            if faculty.person_id not in seen_ids:
                faculty.is_current = False
                _close_current(session, FacultyVersion, faculty.id, at)
                departed += 1
        stats["faculty_departed"] = departed

        fetch_run.status = "success"
        fetch_run.stats = stats
    except Exception as exc:
        fetch_run.status = "failed"
        fetch_run.error = str(exc)
        raise
    finally:
        fetch_run.finished_at = _now()
        if owns_client:
            client.close()

    return fetch_run
