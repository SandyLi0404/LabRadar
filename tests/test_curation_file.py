"""Consistency checks on the real curated mapping in the repository.

These exist because a wrong person ID is invisible: it produces a perfectly
plausible number attached to the wrong researcher. During development the IDs
for two faculty were initially taken from an area roster rather than looked up,
which would have credited a 466-work publication record to a different person.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from labradar.curation import CONFIRMED, load
from labradar.sources.columbia import parse_faculty_directory

ROOT = Path(__file__).resolve().parents[1]
CURATION_PATH = ROOT / "curation" / "openalex_links.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "columbia_faculty.html"


@pytest.fixture(scope="module")
def curation():
    return load(CURATION_PATH)


@pytest.fixture(scope="module")
def directory():
    return {
        r.person_id: r
        for r in parse_faculty_directory(
            FIXTURE.read_text(encoding="utf-8", errors="replace")
        )
    }


class TestFileIsValid:
    def test_it_parses(self, curation):
        assert curation.entries

    def test_it_covers_the_whole_directory(self, curation, directory):
        assert set(curation.entries) == set(directory)


class TestPersonIdsAreReal:
    def test_every_key_exists_in_the_directory(self, curation, directory):
        unknown = sorted(set(curation.entries) - set(directory))
        assert unknown == [], f"curation references unknown person_ids: {unknown}"

    def test_recorded_names_match_the_directory(self, curation, directory):
        """The guard that catches an ID copied from the wrong place."""
        mismatched = [
            (pid, entry.name, directory[pid].full_name)
            for pid, entry in curation.entries.items()
            if entry.name and entry.name != directory[pid].full_name
        ]
        assert mismatched == []

    @pytest.mark.parametrize(
        "person_id,expected_name",
        [("34100", "Josh Alman"), ("68", "Kathleen McKeown"), ("91", "Steven K Feiner")],
    )
    def test_the_mapped_people_are_who_we_think(
        self, curation, directory, person_id, expected_name
    ):
        assert directory[person_id].full_name == expected_name
        assert curation.get(person_id).is_mapped


class TestLinkHygiene:
    def test_no_openalex_id_is_claimed_by_two_people(self, curation):
        """One author profile belonging to two faculty would double-count it."""
        owners: dict[str, list[str]] = {}
        for person_id, openalex_id in curation.all_confirmed():
            owners.setdefault(openalex_id, []).append(person_id)
        shared = {k: v for k, v in owners.items() if len(v) > 1}
        assert shared == {}

    def test_openalex_ids_are_well_formed(self, curation):
        for entry in curation:
            for link in entry.links:
                assert link.openalex_id.startswith("A"), link.openalex_id
                assert link.openalex_id[1:].isdigit(), link.openalex_id

    def test_confirmed_links_carry_a_justification(self, curation):
        """A confirmed identity claim should say why it was accepted."""
        unexplained = [
            (e.person_id, l.openalex_id)
            for e in curation
            for l in e.links
            if l.status == CONFIRMED and not l.note
        ]
        assert unexplained == []

    def test_rejections_say_why(self, curation):
        unexplained = [
            (e.person_id, l.openalex_id)
            for e in curation
            for l in e.links
            if l.status == "rejected" and not l.note
        ]
        assert unexplained == []

    def test_candidates_are_not_counted_as_mapped(self, curation):
        """A proposed link must not silently feed the activity numbers."""
        alman = curation.get("34100")
        statuses = {l.openalex_id: l.status for l in alman.links}
        assert statuses["A5005816789"] == "candidate"
        assert "A5005816789" not in alman.confirmed_ids
