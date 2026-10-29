"""Parser tests against a captured copy of the real department pages.

The fixtures are unedited saves of ``/people/faculty/`` and ``/areas/theory/``,
so these tests fail if the department changes its markup -- which is the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from labradar.sources.columbia import (
    RESEARCH_AREAS,
    FacultyRecord,
    area_url,
    display_name,
    make_slug,
    parse_area_page,
    parse_faculty_directory,
)

FIXTURES = Path(__file__).parent / "fixtures"

EXPECTED_FACULTY_COUNT = 72
EXPECTED_THEORY_MEMBERS = 16


@pytest.fixture(scope="module")
def faculty() -> list[FacultyRecord]:
    html = (FIXTURES / "columbia_faculty.html").read_text(
        encoding="utf-8", errors="replace"
    )
    return parse_faculty_directory(html)


@pytest.fixture(scope="module")
def by_person_id(faculty) -> dict[str, FacultyRecord]:
    return {r.person_id: r for r in faculty}


class TestNameHandling:
    @pytest.mark.parametrize(
        "listed,expected",
        [
            ("Alman, Josh", "Josh Alman"),
            ("Nayar, Shree K", "Shree K Nayar"),
            ("McKeown, Kathleen", "Kathleen McKeown"),
            ("Cher-Wen  Tan", "Cher-Wen Tan"),
        ],
    )
    def test_display_name_flips_surname_first_listings(self, listed, expected):
        assert display_name(listed) == expected

    @pytest.mark.parametrize(
        "listed,expected",
        [
            ("Alman, Josh", "josh-alman"),
            ("Nayar, Shree K", "shree-k-nayar"),
            ("Yannakakis, Mihalis", "mihalis-yannakakis"),
        ],
    )
    def test_slug_reads_in_natural_order(self, listed, expected):
        assert make_slug(listed) == expected


class TestDirectoryParsing:
    def test_finds_every_entry(self, faculty):
        assert len(faculty) == EXPECTED_FACULTY_COUNT

    def test_person_id_is_a_usable_natural_key(self, faculty):
        ids = [r.person_id for r in faculty]
        assert len(set(ids)) == len(ids)
        assert all(pid.isdigit() for pid in ids)

    def test_slugs_do_not_collide(self, faculty):
        slugs = [r.slug for r in faculty]
        assert len(set(slugs)) == len(slugs)

    def test_every_entry_has_name_title_and_email(self, faculty):
        assert all(r.full_name and r.title and r.email for r in faculty)

    def test_emails_look_like_addresses(self, faculty):
        assert all("@" in r.email for r in faculty if r.email)

    def test_a_known_entry_parses_completely(self, by_person_id):
        josh = by_person_id["34100"]
        assert josh.full_name == "Josh Alman"
        assert josh.slug == "josh-alman"
        assert josh.title == "Associate Professor of Computer Science"
        assert josh.email == "josh@cs.columbia.edu"
        assert josh.office == "504 CSB"
        assert josh.homepage_url == "http://joshalman.com"
        assert josh.interests_raw == (
            "theory, algorithms, complexity, algebra in computation"
        )

    def test_interests_prefix_is_stripped(self, faculty):
        assert not any(
            (r.interests_raw or "").lower().startswith("interests:") for r in faculty
        )

    def test_missing_phone_does_not_absorb_the_email(self, by_person_id):
        """The contact markup leaves <li> tags unclosed, so an empty Phone label
        sits directly against the Email link. It must stay None."""
        josh = by_person_id["34100"]
        assert josh.phone is None
        assert josh.office == "504 CSB"

    def test_phone_is_read_when_present(self, by_person_id):
        assert by_person_id["31472"].phone == "212-853-8438"

    def test_optional_fields_are_mostly_populated(self, faculty):
        """Guards against a silent selector break that would null a whole column."""
        total = len(faculty)
        assert sum(1 for r in faculty if r.interests_raw) >= total * 0.8
        assert sum(1 for r in faculty if r.homepage_url) >= total * 0.8
        assert sum(1 for r in faculty if r.office) >= total * 0.8


class TestInterestTerms:
    def test_splits_on_commas(self, by_person_id):
        assert by_person_id["34100"].interest_terms == [
            "theory",
            "algorithms",
            "complexity",
            "algebra in computation",
        ]

    def test_splits_on_trailing_and_and_drops_the_full_stop(self, by_person_id):
        assert by_person_id["31472"].interest_terms == [
            "Causal Inference",
            "Artificial Intelligence",
            "Machine Learning",
        ]

    def test_empty_interests_yield_no_terms(self):
        assert FacultyRecord("1", "A B", "a-b").interest_terms == []

    def test_terms_are_deduplicated_case_insensitively(self):
        record = FacultyRecord("1", "A B", "a-b", interests_raw="ML, ml, Robotics")
        assert record.interest_terms == ["ML", "Robotics"]


class TestContentHash:
    def test_stable_for_identical_content(self):
        a = FacultyRecord("1", "A B", "a-b", title="Prof")
        b = FacultyRecord("1", "A B", "a-b", title="Prof")
        assert a.content_hash == b.content_hash

    def test_changes_when_a_tracked_field_changes(self):
        before = FacultyRecord("1", "A B", "a-b", title="Assistant Professor")
        after = FacultyRecord("1", "A B", "a-b", title="Associate Professor")
        assert before.content_hash != after.content_hash

    def test_distinguishes_empty_from_absent_neighbours(self):
        """Fields are joined on a separator so 'ab'+'' cannot collide with 'a'+'b'."""
        left = FacultyRecord("1", "A B", "a-b", title="ab", email=None)
        right = FacultyRecord("1", "A B", "a-b", title="a", email="b")
        assert left.content_hash != right.content_hash


@pytest.fixture(scope="module")
def theory():
    html = (FIXTURES / "columbia_area_theory.html").read_text(
        encoding="utf-8", errors="replace"
    )
    return parse_area_page(html, "theory")


class TestAreaPages:
    def test_reads_the_roster(self, theory):
        assert theory.slug == "theory"
        assert theory.name == "Theory"
        assert len(theory.person_ids) == EXPECTED_THEORY_MEMBERS

    def test_roster_is_deduplicated(self, theory):
        assert len(set(theory.person_ids)) == len(theory.person_ids)

    def test_members_are_mostly_directory_faculty(self, theory, by_person_id):
        matched = [p for p in theory.person_ids if p in by_person_id]
        assert len(matched) == EXPECTED_THEORY_MEMBERS - 1

    def test_affiliates_outside_the_directory_are_still_listed(
        self, theory, by_person_id
    ):
        """Area pages include cross-listed and affiliated people with no
        directory entry. The parser reports them; the job decides what to do."""
        unmatched = [p for p in theory.person_ids if p not in by_person_id]
        assert unmatched == ["42173"]

    def test_known_member_is_present(self, theory):
        assert "34100" in theory.person_ids  # Josh Alman

    def test_empty_page_yields_empty_roster(self):
        assert parse_area_page("<html><body></body></html>", "theory").person_ids == []


class TestAreaCatalogue:
    def test_all_eleven_areas_are_known(self):
        assert len(RESEARCH_AREAS) == 11

    def test_urls_are_built_from_slugs(self):
        assert area_url("theory") == "https://www.cs.columbia.edu/areas/theory/"
