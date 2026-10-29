"""Diff the two most recent runs of a job and record what moved.

Nothing is recomputed from scratch here: the history already sits in the
versioned tables, so this job is a comparison and can be re-run or backfilled
without touching the underlying observations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from labradar.changes import (
    ActivitySnapshot,
    Change,
    FacultySnapshot,
    diff_activity,
    diff_faculty,
    summarize,
)
from labradar.config import get_settings
from labradar.db.models import (
    ChangeEvent,
    Faculty,
    FacultyActivity,
    FacultyArea,
    FacultyVersion,
    FetchRun,
)

log = logging.getLogger(__name__)

SOURCE = "detect_changes"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def recent_run_ids(session: Session, source: str, limit: int = 2) -> list[int]:
    """The most recent successful run IDs for a source, newest first."""
    return list(
        session.scalars(
            select(FetchRun.id)
            .where(
                FetchRun.source == source,
                FetchRun.status.in_(("success", "partial")),
            )
            .order_by(FetchRun.id.desc())
            .limit(limit)
        )
    )


def faculty_state_at(session: Session, run_id: int) -> dict[str, FacultySnapshot]:
    """Reconstruct directory state as of a run, from the version history.

    A version row is in force at a run if it was created no later than that run
    and had not yet been closed by it.
    """
    rows = session.execute(
        select(
            Faculty.id,
            Faculty.person_id,
            Faculty.full_name,
            FacultyVersion.title,
            FacultyVersion.email,
            FacultyVersion.homepage_url,
            FacultyVersion.interests_raw,
        )
        .join(FacultyVersion, FacultyVersion.faculty_id == Faculty.id)
        .where(
            FacultyVersion.fetch_run_id <= run_id,
            Faculty.first_seen_run <= run_id,
            Faculty.last_seen_run >= run_id,
        )
        .order_by(FacultyVersion.faculty_id, FacultyVersion.valid_from.desc())
    ).all()

    state: dict[str, FacultySnapshot] = {}
    for row in rows:
        if row.person_id in state:
            continue  # ordered newest-first, so the first row wins
        state[row.person_id] = FacultySnapshot(
            faculty_id=row.id,
            person_id=row.person_id,
            full_name=row.full_name,
            fields={
                "title": row.title,
                "email": row.email,
                "homepage_url": row.homepage_url,
                "interests_raw": row.interests_raw,
            },
        )

    area_rows = session.execute(
        select(FacultyArea.faculty_id, FacultyArea.research_area_id).where(
            FacultyArea.fetch_run_id <= run_id,
            FacultyArea.source == "area_page",
        )
    ).all()
    by_faculty: dict[int, set[int]] = {}
    for faculty_id, area_id in area_rows:
        by_faculty.setdefault(faculty_id, set()).add(area_id)
    for snapshot in state.values():
        snapshot.area_ids = frozenset(by_faculty.get(snapshot.faculty_id, set()))

    return state


def activity_state_at(session: Session, run_id: int) -> dict[int, ActivitySnapshot]:
    rows = session.execute(
        select(
            FacultyActivity.faculty_id,
            FacultyActivity.last_pub_year,
            FacultyActivity.works_since,
            FacultyActivity.linked_author_count,
        ).where(FacultyActivity.fetch_run_id == run_id)
    ).all()
    return {
        r.faculty_id: ActivitySnapshot(
            faculty_id=r.faculty_id,
            last_pub_year=r.last_pub_year,
            works_since=r.works_since,
            linked_author_count=r.linked_author_count,
        )
        for r in rows
    }


def _persist(
    session: Session,
    changes: list[Change],
    run: FetchRun,
    previous_run_id: int | None,
    at: datetime,
) -> None:
    for change in changes:
        session.add(
            ChangeEvent(
                fetch_run_id=run.id,
                prev_fetch_run_id=previous_run_id,
                entity_type=change.entity_type,
                entity_id=change.entity_id,
                change_type=change.change_type,
                field=change.field,
                old_value=change.old_value,
                new_value=change.new_value,
                detected_at=at,
            )
        )


def run(session: Session) -> FetchRun:
    """Compare the latest directory and activity runs against their predecessors."""
    settings = get_settings()
    at = _now()
    fetch_run = FetchRun(source=SOURCE, started_at=at, status="running")
    session.add(fetch_run)
    session.flush()

    try:
        changes: list[Change] = []
        stats: dict[str, object] = {}

        directory_runs = recent_run_ids(session, "columbia_directory")
        if len(directory_runs) >= 2:
            current_id, previous_id = directory_runs[0], directory_runs[1]
            changes += diff_faculty(
                faculty_state_at(session, previous_id),
                faculty_state_at(session, current_id),
            )
            _persist(session, changes, fetch_run, previous_id, at)
            stats["directory_compared"] = [previous_id, current_id]
        else:
            # First run has nothing to compare against; that is not a failure.
            stats["directory_compared"] = None

        activity_runs = recent_run_ids(session, "compute_activity")
        if len(activity_runs) >= 2:
            current_id, previous_id = activity_runs[0], activity_runs[1]
            activity_changes = diff_activity(
                activity_state_at(session, previous_id),
                activity_state_at(session, current_id),
                min_delta=settings.activity_shift_min_delta,
                min_pct=settings.activity_shift_min_pct,
            )
            _persist(session, activity_changes, fetch_run, previous_id, at)
            changes += activity_changes
            stats["activity_compared"] = [previous_id, current_id]
        else:
            stats["activity_compared"] = None

        stats["changes"] = summarize(changes)
        stats["total_changes"] = len(changes)

        fetch_run.status = "success"
        fetch_run.stats = stats
    except Exception as exc:
        fetch_run.status = "failed"
        fetch_run.error = str(exc)
        raise
    finally:
        fetch_run.finished_at = _now()

    return fetch_run
