"""Derive the displayed activity signals, and rebuild the search index.

Works are deduplicated across the union of a person's confirmed profiles, so a
paper that OpenAlex has split into a preprint, a proceedings entry and a journal
article counts once. The clusters are persisted, not just the totals, so the
reported number can always be traced back to the records behind it.

Only works still attached to a profile in the latest OpenAlex run are counted.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from labradar.activity import compute_signals, unmapped_signals
from labradar.config import get_settings
from labradar.db.models import (
    Faculty,
    FacultyLab,
    Lab,
    LabActivity,
    FacultyActivity,
    FacultyOpenAlexLink,
    FacultySearch,
    FacultyVersion,
    FacultyWorkCluster,
    FetchRun,
    OpenAlexAuthor,
    OpenAlexAuthorWork,
    OpenAlexWork,
    ResearchArea,
    FacultyArea,
)
from labradar.curation import CONFIRMED
from labradar.dedup import WorkRecord

log = logging.getLogger(__name__)

SOURCE = "compute_activity"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def latest_openalex_run_id(session: Session) -> int | None:
    """The most recent OpenAlex fetch that stored anything.

    Works are counted relative to this run, so a stale authorship that OpenAlex
    has since dropped stops contributing without being erased.
    """
    return session.scalar(
        select(func.max(FetchRun.id)).where(
            FetchRun.source == "openalex",
            FetchRun.status.in_(("success", "partial")),
        )
    )


def current_works_for(
    session: Session, faculty_id: int, openalex_run_id: int
) -> list[WorkRecord]:
    """Every work still attached to this person's confirmed profiles."""
    rows = session.execute(
        select(
            OpenAlexWork.openalex_id,
            OpenAlexWork.title,
            OpenAlexWork.publication_year,
            OpenAlexWork.doi,
            OpenAlexWork.work_type,
            OpenAlexWork.is_preprint,
            OpenAlexWork.publication_date,
            OpenAlexWork.url,
        )
        .join(OpenAlexAuthorWork, OpenAlexAuthorWork.openalex_work_id == OpenAlexWork.id)
        .join(
            FacultyOpenAlexLink,
            FacultyOpenAlexLink.openalex_author_id
            == OpenAlexAuthorWork.openalex_author_id,
        )
        .where(
            FacultyOpenAlexLink.faculty_id == faculty_id,
            FacultyOpenAlexLink.link_status == CONFIRMED,
            OpenAlexAuthorWork.last_seen_run == openalex_run_id,
        )
        .distinct()
    ).all()

    return [
        WorkRecord(
            openalex_id=r.openalex_id,
            title=r.title,
            publication_year=r.publication_year,
            doi=r.doi,
            work_type=r.work_type,
            is_preprint=r.is_preprint,
            publication_date=r.publication_date.isoformat() if r.publication_date else None,
            url=r.url,
        )
        for r in rows
    ]


def work_ids_by_openalex_id(session: Session, openalex_ids: set[str]) -> dict[str, int]:
    if not openalex_ids:
        return {}
    rows = session.execute(
        select(OpenAlexWork.openalex_id, OpenAlexWork.id).where(
            OpenAlexWork.openalex_id.in_(openalex_ids)
        )
    ).all()
    return {r.openalex_id: r.id for r in rows}


def rebuild_search(session: Session, run: FetchRun) -> int:
    """Rebuild the weighted search document for every current faculty member.

    A generated column cannot span three tables, and triggers across all of them
    would be harder to reason about than regenerating ~70 rows once per run.
    """
    session.execute(delete(FacultySearch))
    session.flush()

    inserted = session.execute(
        text(
            """
            INSERT INTO faculty_search (faculty_id, fetch_run_id, document)
            SELECT
                f.id,
                :run_id,
                setweight(to_tsvector('english', coalesce(f.full_name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(a.area_names, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(v.title, '')), 'C') ||
                setweight(to_tsvector('english', coalesce(v.interests_raw, '')), 'D')
            FROM faculty f
            LEFT JOIN faculty_version v
                   ON v.faculty_id = f.id AND v.valid_to IS NULL
            LEFT JOIN (
                SELECT fa.faculty_id, string_agg(ra.name, ' ') AS area_names
                FROM faculty_area fa
                JOIN research_area ra ON ra.id = fa.research_area_id
                WHERE fa.valid_to IS NULL
                GROUP BY fa.faculty_id
            ) a ON a.faculty_id = f.id
            WHERE f.is_current
            """
        ),
        {"run_id": run.id},
    )
    return inserted.rowcount or 0


def compute_lab_activity(
    session: Session, run: FetchRun, as_of: date, openalex_run_id: int | None
) -> dict[str, int]:
    """Aggregate each lab's output from its members.

    The union of the members' works is deduplicated in a single pass, so a paper
    written by two people in the same group counts once for the group rather
    than twice. That is the number a student comparing labs actually wants.

    Lab websites are not used as the publication source: surveying them found a
    quarter delegating to Google Scholar, a quarter with no list at all, and the
    rest in bespoke undated formats with nothing to deduplicate on.
    """
    stats = {"labs": 0, "labs_with_activity": 0}

    for lab in session.scalars(select(Lab).where(Lab.is_current)):
        stats["labs"] += 1
        member_ids = list(
            session.scalars(
                select(FacultyLab.faculty_id).where(
                    FacultyLab.lab_id == lab.id, FacultyLab.valid_to.is_(None)
                )
            )
        )

        records: list[WorkRecord] = []
        mapped_members = 0
        if openalex_run_id is not None:
            for faculty_id in member_ids:
                member_records = current_works_for(session, faculty_id, openalex_run_id)
                if member_records:
                    mapped_members += 1
                records.extend(member_records)

        if records:
            signals, _ = compute_signals(
                records, as_of=as_of, linked_author_count=mapped_members
            )
            stats["labs_with_activity"] += 1
            last_pub = signals.last_pub_year
            last_year = signals.works_since
            total = signals.works_total
        else:
            last_pub, last_year, total = None, 0, 0

        session.execute(
            delete(LabActivity).where(
                LabActivity.lab_id == lab.id, LabActivity.fetch_run_id == run.id
            )
        )
        session.add(
            LabActivity(
                lab_id=lab.id,
                fetch_run_id=run.id,
                as_of_date=as_of,
                last_pub_year=last_pub,
                works_since=last_year,
                works_total=total,
                member_count=len(member_ids),
                mapped_member_count=mapped_members,
            )
        )

    return stats


def run(session: Session, as_of: date | None = None) -> FetchRun:
    """Recompute signals for every current faculty member."""
    settings = get_settings()
    as_of = as_of or date.today()
    at = _now()

    fetch_run = FetchRun(source=SOURCE, started_at=at, status="running")
    session.add(fetch_run)
    session.flush()

    try:
        openalex_run_id = latest_openalex_run_id(session)
        faculty_rows = session.scalars(
            select(Faculty).where(Faculty.is_current)
        ).all()

        stats = {
            "faculty": len(faculty_rows),
            "mapped": 0,
            "unmapped": 0,
            "clusters_written": 0,
            "records_collapsed": 0,
        }

        for faculty in faculty_rows:
            link_count = session.scalar(
                select(func.count())
                .select_from(FacultyOpenAlexLink)
                .where(
                    FacultyOpenAlexLink.faculty_id == faculty.id,
                    FacultyOpenAlexLink.link_status == CONFIRMED,
                )
            )

            if not link_count or openalex_run_id is None:
                signals = unmapped_signals(as_of=as_of)
                clusters = []
                stats["unmapped"] += 1
            else:
                records = current_works_for(session, faculty.id, openalex_run_id)
                signals, clusters = compute_signals(
                    records, as_of=as_of, linked_author_count=link_count
                )
                stats["mapped"] += 1
                stats["records_collapsed"] += len(records) - len(clusters)

            session.execute(
                delete(FacultyWorkCluster).where(
                    FacultyWorkCluster.faculty_id == faculty.id,
                    FacultyWorkCluster.fetch_run_id == fetch_run.id,
                )
            )

            if clusters:
                ids = work_ids_by_openalex_id(
                    session,
                    {c.representative.openalex_id for c in clusters},
                )
                for cluster in clusters:
                    session.add(
                        FacultyWorkCluster(
                            faculty_id=faculty.id,
                            fetch_run_id=fetch_run.id,
                            cluster_key=cluster.cluster_key,
                            canonical_year=cluster.canonical_year,
                            record_count=cluster.record_count,
                            representative_work_id=ids.get(
                                cluster.representative.openalex_id
                            ),
                            url=cluster.url,
                        )
                    )
                stats["clusters_written"] += len(clusters)

            session.execute(
                delete(FacultyActivity).where(
                    FacultyActivity.faculty_id == faculty.id,
                    FacultyActivity.fetch_run_id == fetch_run.id,
                )
            )
            session.add(
                FacultyActivity(
                    faculty_id=faculty.id,
                    fetch_run_id=fetch_run.id,
                    as_of_date=signals.as_of_date,
                    last_pub_year=signals.last_pub_year,
                    works_since=signals.works_since,
                    works_total=signals.works_total,
                    raw_records_since=signals.raw_records_since,
                    linked_author_count=signals.linked_author_count,
                )
            )

        session.flush()
        stats.update(
            compute_lab_activity(session, fetch_run, as_of, openalex_run_id)
        )
        stats["search_rows"] = rebuild_search(session, fetch_run)

        fetch_run.status = "success"
        fetch_run.stats = stats
    except Exception as exc:
        fetch_run.status = "failed"
        fetch_run.error = str(exc)
        raise
    finally:
        fetch_run.finished_at = _now()

    return fetch_run
