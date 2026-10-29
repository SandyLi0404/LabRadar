"""Change-detection tests.

The threshold tests matter most. An over-sensitive rule would emit an event for
most of the department every week, which in practice means nobody reads the
change feed and a real departure goes unnoticed.
"""

from __future__ import annotations

from datetime import date

import pytest

from labradar.activity import compute_signals, unmapped_signals
from labradar.changes import (
    ActivitySnapshot,
    FacultySnapshot,
    diff_activity,
    diff_faculty,
    is_activity_shift,
    summarize,
)
from labradar.dedup import WorkRecord


def snap(person_id="34100", faculty_id=1, name="Josh Alman", **fields):
    areas = fields.pop("area_ids", frozenset())
    return FacultySnapshot(
        faculty_id=faculty_id,
        person_id=person_id,
        full_name=name,
        fields=fields,
        area_ids=areas,
    )


class TestDirectoryDiff:
    def test_new_person_reports_as_added(self):
        changes = diff_faculty({}, {"34100": snap()})
        assert [c.change_type for c in changes] == ["added"]
        assert changes[0].new_value == "Josh Alman"

    def test_departed_person_reports_as_removed(self):
        changes = diff_faculty({"34100": snap()}, {})
        assert [c.change_type for c in changes] == ["removed"]
        assert changes[0].old_value == "Josh Alman"

    def test_unchanged_run_reports_nothing(self):
        before = {"34100": snap(title="Associate Professor")}
        after = {"34100": snap(title="Associate Professor")}
        assert diff_faculty(before, after) == []

    def test_promotion_reports_a_title_change(self):
        before = {"34100": snap(title="Assistant Professor")}
        after = {"34100": snap(title="Associate Professor")}
        changes = diff_faculty(before, after)
        assert [c.change_type for c in changes] == ["title_changed"]
        assert changes[0].old_value == "Assistant Professor"
        assert changes[0].new_value == "Associate Professor"

    def test_rename_is_not_a_departure_and_arrival(self):
        """Keying on person ID is what prevents a rename looking like turnover."""
        before = {"34100": snap(name="Josh Alman")}
        after = {"34100": snap(name="Joshua Alman")}
        changes = diff_faculty(before, after)
        assert [c.change_type for c in changes] == ["name_changed"]

    def test_untracked_field_churn_is_ignored(self):
        """Office moves say nothing about a group's health."""
        before = {"34100": snap(title="Prof", office="504 CSB")}
        after = {"34100": snap(title="Prof", office="620 CEPSR")}
        assert diff_faculty(before, after) == []

    def test_area_membership_changes_are_reported(self):
        before = {"34100": snap(area_ids=frozenset({1, 2}))}
        after = {"34100": snap(area_ids=frozenset({2, 3}))}
        changes = diff_faculty(before, after)
        assert [c.change_type for c in changes] == ["area_added", "area_removed"]
        assert changes[0].new_value == 3
        assert changes[1].old_value == 1

    def test_several_people_change_at_once(self):
        before = {"1": snap("1", 1, "A A"), "2": snap("2", 2, "B B")}
        after = {"2": snap("2", 2, "B B"), "3": snap("3", 3, "C C")}
        assert summarize(diff_faculty(before, after)) == {"added": 1, "removed": 1}


class TestActivityThreshold:
    def steady(self, **kw):
        return ActivitySnapshot(faculty_id=1, last_pub_year=2026, works_since=20, **kw)

    def test_identical_activity_is_not_a_shift(self):
        assert not is_activity_shift(self.steady(), self.steady())

    @pytest.mark.parametrize("after_count", [19, 21])
    def test_single_paper_jitter_is_ignored(self, after_count):
        """The case that would otherwise fire for most of the department weekly."""
        before = self.steady()
        after = ActivitySnapshot(1, last_pub_year=2026, works_since=after_count)
        assert not is_activity_shift(before, after)

    def test_small_relative_move_on_a_large_count_is_ignored(self):
        before = ActivitySnapshot(1, last_pub_year=2026, works_since=40)
        after = ActivitySnapshot(1, last_pub_year=2026, works_since=42)
        assert not is_activity_shift(before, after)

    def test_large_relative_move_reports(self):
        before = ActivitySnapshot(1, last_pub_year=2026, works_since=8)
        after = ActivitySnapshot(1, last_pub_year=2026, works_since=2)
        assert is_activity_shift(before, after)

    def test_last_publication_year_moving_always_reports(self):
        before = ActivitySnapshot(1, last_pub_year=2024, works_since=20)
        after = ActivitySnapshot(1, last_pub_year=2026, works_since=20)
        assert is_activity_shift(before, after)

    def test_falling_silent_always_reports_even_by_one_paper(self):
        """1 -> 0 is below both thresholds but is the whole point of the app."""
        before = ActivitySnapshot(1, last_pub_year=2024, works_since=1)
        after = ActivitySnapshot(1, last_pub_year=2024, works_since=0)
        assert is_activity_shift(before, after)

    def test_resuming_after_silence_always_reports(self):
        before = ActivitySnapshot(1, last_pub_year=2020, works_since=0)
        after = ActivitySnapshot(1, last_pub_year=2020, works_since=1)
        assert is_activity_shift(before, after)

    def test_thresholds_are_configurable(self):
        before = ActivitySnapshot(1, last_pub_year=2026, works_since=20)
        after = ActivitySnapshot(1, last_pub_year=2026, works_since=18)
        assert not is_activity_shift(before, after)
        assert is_activity_shift(before, after, min_delta=2, min_pct=0.05)


class TestUnmappedPeople:
    """Silence from an unmapped person is our ignorance, not a finding."""

    def test_unmapped_before_is_not_a_shift(self):
        before = ActivitySnapshot(1, None, 0, linked_author_count=0)
        after = ActivitySnapshot(1, 2026, 20, linked_author_count=1)
        assert not is_activity_shift(before, after)

    def test_unmapped_after_is_not_a_shift(self):
        before = ActivitySnapshot(1, 2026, 20, linked_author_count=1)
        after = ActivitySnapshot(1, None, 0, linked_author_count=0)
        assert not is_activity_shift(before, after)

    def test_mapping_someone_does_not_flood_the_feed(self):
        previous = {i: ActivitySnapshot(i, None, 0, 0) for i in range(1, 30)}
        current = {i: ActivitySnapshot(i, 2026, 12, 1) for i in range(1, 30)}
        assert diff_activity(previous, current) == []


class TestActivityDiff:
    def test_reports_only_people_past_the_threshold(self):
        previous = {
            1: ActivitySnapshot(1, 2026, 20),
            2: ActivitySnapshot(2, 2022, 3),
        }
        current = {
            1: ActivitySnapshot(1, 2026, 21),  # jitter
            2: ActivitySnapshot(2, 2022, 0),  # fell silent
        }
        changes = diff_activity(previous, current)
        assert [c.entity_id for c in changes] == [2]
        assert changes[0].change_type == "activity_shift"

    def test_carries_both_sides_for_display(self):
        previous = {1: ActivitySnapshot(1, 2023, 6)}
        current = {1: ActivitySnapshot(1, 2026, 1)}
        change = diff_activity(previous, current)[0]
        assert change.old_value == {"works_since": 6, "last_pub_year": 2023}
        assert change.new_value == {"works_since": 1, "last_pub_year": 2026}

    def test_newly_added_person_yields_no_activity_event(self):
        """Their arrival is already reported by the directory diff."""
        assert diff_activity({}, {1: ActivitySnapshot(1, 2026, 20)}) == []


class TestActivitySignals:
    AS_OF = date(2026, 9, 22)

    def test_unmapped_is_distinguishable_from_silent(self):
        unmapped = unmapped_signals(as_of=self.AS_OF)
        silent, _ = compute_signals(
            [WorkRecord("W1", "An Old Paper With A Sufficiently Long Title", 2015)],
            as_of=self.AS_OF,
            linked_author_count=1,
        )
        assert unmapped.is_unmapped
        assert not silent.is_unmapped
        assert unmapped.works_since == silent.works_since == 0
        assert silent.last_pub_year == 2015
        assert unmapped.last_pub_year is None

    def test_years_since_last_publication(self):
        signals, _ = compute_signals(
            [WorkRecord("W1", "An Old Paper With A Sufficiently Long Title", 2019)],
            as_of=self.AS_OF,
            linked_author_count=1,
        )
        assert signals.years_since_last_publication == 7
        assert signals.is_inactive

    def test_raw_and_deduped_counts_are_both_reported(self):
        """The interface shows the correction instead of applying it silently."""
        records = [
            WorkRecord("W1", "A Sufficiently Long Paper Title Here", 2025),
            WorkRecord("W2", "A Sufficiently Long Paper Title Here", 2026),
        ]
        signals, _ = compute_signals(
            records, as_of=self.AS_OF, linked_author_count=1
        )
        assert signals.works_since == 1
        assert signals.raw_records_since == 2

    def test_returns_clusters_for_persistence(self):
        _, clusters = compute_signals(
            [WorkRecord("W1", "A Sufficiently Long Paper Title Here", 2026)],
            as_of=self.AS_OF,
            linked_author_count=1,
        )
        assert len(clusters) == 1
        assert clusters[0].canonical_year == 2026
