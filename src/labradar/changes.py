"""Diffing one run against the previous one.

Two failure modes shape this module. Too sensitive and every run emits an event
for everybody, burying the departures nobody should miss. Too blunt and a
professor who stopped publishing years ago never surfaces at all.

The thresholds below are the compromise: ordinary indexing jitter is ignored,
but crossing into or out of "nothing in the last year" always reports, because
that is the transition a student actually cares about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# Directory fields worth reporting. Office and phone churn without meaning
# anything for a research group's health, so they are recorded in history but
# not surfaced as change events.
TRACKED_FIELDS = ("title", "email", "homepage_url", "interests_raw")


@dataclass(frozen=True)
class Change:
    """One reported difference."""

    entity_type: str
    change_type: str
    entity_id: int | None = None
    field: str | None = None
    old_value: Any = None
    new_value: Any = None


@dataclass
class ActivitySnapshot:
    """The subset of activity signals that change detection compares."""

    faculty_id: int
    last_pub_year: int | None
    works_since: int
    linked_author_count: int = 1

    @property
    def is_unmapped(self) -> bool:
        return self.linked_author_count == 0


@dataclass
class FacultySnapshot:
    """A faculty member's comparable state at one run."""

    faculty_id: int
    person_id: str
    full_name: str
    fields: dict[str, Any] = field(default_factory=dict)
    area_ids: frozenset[int] = frozenset()


def diff_faculty(
    previous: Mapping[str, FacultySnapshot],
    current: Mapping[str, FacultySnapshot],
) -> list[Change]:
    """Compare two runs' directory state, keyed on person ID.

    Keying on the department's person ID rather than on name means a rename
    reports as one ``name_changed`` event instead of a departure plus an
    arrival, which would otherwise be indistinguishable from real turnover.
    """
    changes: list[Change] = []

    for person_id, snapshot in current.items():
        if person_id not in previous:
            changes.append(
                Change(
                    entity_type="faculty",
                    change_type="added",
                    entity_id=snapshot.faculty_id,
                    new_value=snapshot.full_name,
                )
            )

    for person_id, before in previous.items():
        after = current.get(person_id)
        if after is None:
            changes.append(
                Change(
                    entity_type="faculty",
                    change_type="removed",
                    entity_id=before.faculty_id,
                    old_value=before.full_name,
                )
            )
            continue

        if before.full_name != after.full_name:
            changes.append(
                Change(
                    entity_type="faculty",
                    change_type="name_changed",
                    entity_id=after.faculty_id,
                    field="full_name",
                    old_value=before.full_name,
                    new_value=after.full_name,
                )
            )

        for name in TRACKED_FIELDS:
            old = before.fields.get(name)
            new = after.fields.get(name)
            if old != new:
                changes.append(
                    Change(
                        entity_type="faculty",
                        change_type=f"{name}_changed",
                        entity_id=after.faculty_id,
                        field=name,
                        old_value=old,
                        new_value=new,
                    )
                )

        for area_id in sorted(after.area_ids - before.area_ids):
            changes.append(
                Change(
                    entity_type="faculty_area",
                    change_type="area_added",
                    entity_id=after.faculty_id,
                    new_value=area_id,
                )
            )
        for area_id in sorted(before.area_ids - after.area_ids):
            changes.append(
                Change(
                    entity_type="faculty_area",
                    change_type="area_removed",
                    entity_id=after.faculty_id,
                    old_value=area_id,
                )
            )

    return changes


def is_activity_shift(
    before: ActivitySnapshot,
    after: ActivitySnapshot,
    *,
    min_delta: int = 2,
    min_pct: float = 0.25,
) -> bool:
    """Whether a change in publishing is worth reporting.

    Reports when the most recent publication year moves, when the last-year
    count moves by at least ``min_delta`` papers *and* at least ``min_pct`` of
    its previous value, or whenever someone crosses the "nothing in the last
    year" line in either direction.

    Requiring both an absolute and a relative move is what keeps this quiet:
    a prolific researcher drifting 40 -> 42 is noise, while 2 -> 0 is not.
    """
    # Nothing to compare while the person is unmapped; silence here is our
    # ignorance, not a finding about them.
    if before.is_unmapped or after.is_unmapped:
        return False

    crossed_silence = (before.works_since == 0) != (after.works_since == 0)
    if crossed_silence:
        return True

    if before.last_pub_year != after.last_pub_year:
        return True

    delta = abs(after.works_since - before.works_since)
    if delta < min_delta:
        return False
    if before.works_since == 0:
        return True
    return delta >= before.works_since * min_pct


def diff_activity(
    previous: Mapping[int, ActivitySnapshot],
    current: Mapping[int, ActivitySnapshot],
    *,
    min_delta: int = 2,
    min_pct: float = 0.25,
) -> list[Change]:
    """Report activity shifts for everyone present in both runs."""
    changes: list[Change] = []
    for faculty_id, after in current.items():
        before = previous.get(faculty_id)
        if before is None:
            continue
        if not is_activity_shift(
            before, after, min_delta=min_delta, min_pct=min_pct
        ):
            continue
        changes.append(
            Change(
                entity_type="activity",
                change_type="activity_shift",
                entity_id=faculty_id,
                field="works_since",
                old_value={
                    "works_since": before.works_since,
                    "last_pub_year": before.last_pub_year,
                },
                new_value={
                    "works_since": after.works_since,
                    "last_pub_year": after.last_pub_year,
                },
            )
        )
    return changes


def summarize(changes: Sequence[Change]) -> dict[str, int]:
    """Counts by change type, for the run's stats blob."""
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.change_type] = counts.get(change.change_type, 0) + 1
    return counts
