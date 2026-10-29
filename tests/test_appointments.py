"""Appointment-track classification.

The department directory's title is the ground truth here, and publication
evidence does not override it. That is a deliberate policy choice rather than an
inference: Ansaf Salleb-Aouissi is a Senior Lecturer in Discipline who runs the
PRAISE Lab and publishes, and she is still excluded, because the directory calls
her a lecturer.

These tests pin the policy so a later "improvement" that re-admits people on the
strength of their output fails loudly instead of quietly changing who the app
claims is research faculty.
"""

from __future__ import annotations

import pytest

from labradar.sources.columbia import (
    RESEARCH_TRACK,
    TEACHING_TRACK,
    classify_title,
    parse_faculty_directory,
)
from tests.test_columbia import FIXTURES


class TestTeachingTitles:
    @pytest.mark.parametrize(
        "title",
        [
            "Senior Lecturer",
            "Senior Lecturer in Discipline",
            "Senior Lecturer in the Discipline of Computer Science",
            "Senior Lecturer in Discipline, Computer Science and Operations Research",
            "Lecturer in the Discipline of Data Science in the Department of Computer Science",
        ],
    )
    def test_every_lecturer_variant_is_teaching_track(self, title):
        assert classify_title(title) == TEACHING_TRACK

    def test_teaching_professor_is_teaching_track(self):
        """Its title says Professor, but the appointment is a teaching one."""
        assert classify_title("Teaching Professor") == TEACHING_TRACK

    @pytest.mark.parametrize(
        "title", ["Adjunct Professor", "Instructor", "Professor of Practice"]
    )
    def test_other_teaching_titles(self, title):
        assert classify_title(title) == TEACHING_TRACK


class TestResearchTitles:
    @pytest.mark.parametrize(
        "title",
        [
            "Professor",
            "Associate Professor",
            "Assistant Professor",
            "Professor and Department Chair",
            "T. C. Chang Professor of Computer Science",
            "Henry and Gertrude Rothschilds Professor of Computer Science",
            "Professor, Joint with Statistics",
            "Executive Vice President for Research, Professor of Computer Science",
            "Wang Family Professor of Computer Science (in Dental Medicine)",
        ],
    )
    def test_professorial_titles_are_research_track(self, title):
        assert classify_title(title) == RESEARCH_TRACK

    def test_a_missing_title_defaults_to_research(self):
        """Better to show someone than to silently drop them on missing data."""
        assert classify_title(None) == RESEARCH_TRACK

    def test_professor_is_not_matched_inside_another_word(self):
        assert classify_title("Professorial Lecturer") == TEACHING_TRACK


@pytest.fixture(scope="module")
def records():
    html = (FIXTURES / "columbia_faculty.html").read_text(
        encoding="utf-8", errors="replace"
    )
    return parse_faculty_directory(html)


class TestAgainstTheRealDirectory:
    def test_the_split_matches_the_directory(self, records):
        teaching = [r for r in records if r.appointment_track == TEACHING_TRACK]
        assert len(teaching) == 11
        assert len(records) - len(teaching) == 61

    def test_the_known_teaching_staff_are_all_caught(self, records):
        by_name = {r.full_name: r for r in records}
        for name in (
            "Ansaf Salleb-Aouissi",
            "Daniel Bauer",
            "Adam H Cannon",
            "Jae Woo Lee",
            "Paul S Blaer",
            "Nakul Verma",
            "Yining Liu",
        ):
            assert by_name[name].appointment_track == TEACHING_TRACK, name

    def test_research_evidence_does_not_promote_a_lecturer(self, records):
        """The case the policy exists to settle.

        She runs the PRAISE Lab and publishes; the directory still calls her a
        lecturer, and the directory wins.
        """
        ansaf = next(r for r in records if r.full_name == "Ansaf Salleb-Aouissi")
        assert "Lecturer" in ansaf.title
        assert ansaf.appointment_track == TEACHING_TRACK

    def test_named_chairs_are_not_mistaken_for_teaching(self, records):
        chairs = [r for r in records if "Professor of" in (r.title or "")]
        assert chairs
        assert all(r.appointment_track == RESEARCH_TRACK for r in chairs)
