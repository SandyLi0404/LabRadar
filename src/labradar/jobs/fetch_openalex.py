"""Activity job: pull profiles and works for every confirmed OpenAlex link.

Only curated links are fetched. Automatic name matching is not used at runtime
because it cannot distinguish a researcher's real profile from the single-work
fragments OpenAlex splits off, and those fragments all look maximally recent.

Per-ID profile lookups are free; works pages cost $0.0001 each. The budget guard
in the client stops the run while allowance remains rather than failing halfway
through with nothing left to diagnose with.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from labradar.config import get_settings
from labradar.curation import CurationFile
from labradar.db.models import (
    Faculty,
    FacultyOpenAlexLink,
    FetchRun,
    OpenAlexAuthor,
    OpenAlexAuthorSnapshot,
    OpenAlexAuthorWork,
    OpenAlexWork,
)
from labradar.dedup import WorkRecord, is_supplement, title_norm
from labradar.sources.openalex import (
    AuthorProfile,
    BudgetExhausted,
    OpenAlexClient,
)

log = logging.getLogger(__name__)

SOURCE = "openalex"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sync_links(session: Session, curation: CurationFile) -> dict[str, int]:
    """Mirror the curated file into the database.

    The file stays authoritative: a link removed there is removed here, so the
    database can never disagree with the reviewed decisions in git.
    """
    stats = {"links_confirmed": 0, "links_rejected": 0, "links_removed": 0}

    faculty_by_person = {
        f.person_id: f for f in session.scalars(select(Faculty))
    }
    authors = {a.openalex_id: a for a in session.scalars(select(OpenAlexAuthor))}

    wanted: set[tuple[int, str]] = set()

    for entry in curation:
        faculty = faculty_by_person.get(entry.person_id)
        if faculty is None:
            log.warning(
                "curation lists person_id %s which is not in the directory",
                entry.person_id,
            )
            continue

        for link in entry.links:
            author = authors.get(link.openalex_id)
            if author is None:
                author = OpenAlexAuthor(openalex_id=link.openalex_id)
                session.add(author)
                session.flush()
                authors[link.openalex_id] = author

            wanted.add((faculty.id, link.openalex_id))
            row = session.scalar(
                select(FacultyOpenAlexLink).where(
                    FacultyOpenAlexLink.faculty_id == faculty.id,
                    FacultyOpenAlexLink.openalex_author_id == author.id,
                )
            )
            if row is None:
                row = FacultyOpenAlexLink(
                    faculty_id=faculty.id, openalex_author_id=author.id
                )
                session.add(row)

            row.link_status = link.status
            row.is_primary = link.is_primary
            row.confidence = link.confidence
            row.note = link.note
            row.curated_by = link.curated_by
            row.curated_at = link.curated_at
            row.evidence = link.evidence

            if link.is_confirmed:
                stats["links_confirmed"] += 1
            elif link.status == "rejected":
                stats["links_rejected"] += 1

    session.flush()

    by_id = {a.id: a.openalex_id for a in authors.values()}
    for row in session.scalars(select(FacultyOpenAlexLink)).all():
        key = (row.faculty_id, by_id.get(row.openalex_author_id))
        if key not in wanted:
            session.delete(row)
            stats["links_removed"] += 1

    return stats


def upsert_author(session: Session, profile: AuthorProfile) -> OpenAlexAuthor:
    author = session.scalar(
        select(OpenAlexAuthor).where(OpenAlexAuthor.openalex_id == profile.openalex_id)
    )
    if author is None:
        author = OpenAlexAuthor(openalex_id=profile.openalex_id)
        session.add(author)
    author.display_name = profile.display_name
    author.orcid = profile.orcid
    author.last_known_institution = profile.last_known_institution
    session.flush()
    return author


def _as_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def upsert_works(
    session: Session,
    author: OpenAlexAuthor,
    records: list[WorkRecord],
    run: FetchRun,
) -> int:
    """Store works and refresh their authorship run bounds.

    ``last_seen_run`` is what lets ``compute_activity`` ignore works OpenAlex has
    since detached from this profile, without losing the record that they were
    once attached.
    """
    if not records:
        return 0

    existing = {
        w.openalex_id: w
        for w in session.scalars(
            select(OpenAlexWork).where(
                OpenAlexWork.openalex_id.in_([r.openalex_id for r in records])
            )
        )
    }

    for record in records:
        work = existing.get(record.openalex_id)
        if work is None:
            work = OpenAlexWork(openalex_id=record.openalex_id)
            session.add(work)
            existing[record.openalex_id] = work
        work.doi = record.doi
        work.title = record.title
        work.title_norm = title_norm(record.title)
        work.publication_year = record.publication_year
        work.work_type = record.work_type
        work.is_preprint = record.is_preprint
        work.publication_date = _as_date(record.publication_date)
        work.url = record.url

    session.flush()

    links = {
        row.openalex_work_id: row
        for row in session.scalars(
            select(OpenAlexAuthorWork).where(
                OpenAlexAuthorWork.openalex_author_id == author.id
            )
        )
    }
    for record in records:
        work = existing[record.openalex_id]
        row = links.get(work.id)
        if row is None:
            session.add(
                OpenAlexAuthorWork(
                    openalex_author_id=author.id,
                    openalex_work_id=work.id,
                    first_seen_run=run.id,
                    last_seen_run=run.id,
                )
            )
        else:
            row.last_seen_run = run.id

    return len(records)


def run(
    session: Session,
    curation: CurationFile,
    client: OpenAlexClient | None = None,
) -> FetchRun:
    """Refresh every confirmed profile under one run."""
    settings = get_settings()
    at = _now()
    fetch_run = FetchRun(source=SOURCE, started_at=at, status="running")
    session.add(fetch_run)
    session.flush()

    owns_client = client is None
    client = client or OpenAlexClient(
        settings.openalex_api_key, reserve_usd=settings.openalex_reserve_usd
    )

    stats = dict(sync_links(session, curation))
    stats.update(
        {"authors_fetched": 0, "works_seen": 0, "authors_failed": 0, "supplements_skipped": 0}
    )
    budget_stopped = False

    try:
        targets = sorted({oid for _, oid in curation.all_confirmed()})
        for openalex_id in targets:
            try:
                profile = client.author(openalex_id)
                records = client.works(openalex_id)
            except BudgetExhausted as exc:
                # Stop cleanly and keep what we have; the run is partial, not failed.
                log.warning("stopping early: %s", exc)
                budget_stopped = True
                break
            except Exception as exc:
                log.warning("author %s failed: %s", openalex_id, exc)
                stats["authors_failed"] += 1
                continue

            # Skipped records keep their old last_seen_run, so the staleness
            # filter stops counting any stored before this rule existed.
            papers = [r for r in records if not is_supplement(r.title, r.work_type)]
            stats["supplements_skipped"] += len(records) - len(papers)
            author = upsert_author(session, profile)
            stats["works_seen"] += upsert_works(session, author, papers, fetch_run)
            session.add(
                OpenAlexAuthorSnapshot(
                    openalex_author_id=author.id,
                    fetch_run_id=fetch_run.id,
                    works_count=profile.works_count,
                    cited_by_count=profile.cited_by_count,
                    counts_by_year={str(k): v for k, v in profile.counts_by_year.items()},
                    topics={"topics": profile.topics},
                    captured_at=_now(),
                )
            )
            stats["authors_fetched"] += 1

        stats["budget_spent_usd"] = round(client.budget.spent_usd, 6)
        stats["budget_remaining_usd"] = client.budget.remaining_usd
        stats["api_requests"] = client.budget.requests
        stats["budget_stopped_early"] = budget_stopped

        fetch_run.status = (
            "partial" if budget_stopped or stats["authors_failed"] else "success"
        )
        fetch_run.stats = stats
    except Exception as exc:
        fetch_run.status = "failed"
        fetch_run.error = str(exc)
        fetch_run.stats = stats
        raise
    finally:
        fetch_run.finished_at = _now()
        if owns_client:
            client.close()

    return fetch_run
