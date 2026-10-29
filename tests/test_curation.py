"""Curation-file tests.

The mapping is the source of truth for identity, so a malformed file must fail
loudly at load time rather than quietly producing wrong activity numbers.
"""

from __future__ import annotations

import pytest

from labradar.curation import (
    CANDIDATE,
    CONFIRMED,
    REJECTED,
    CurationError,
    Link,
    dump,
    load,
)


def write(tmp_path, text: str):
    path = tmp_path / "links.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoading:
    def test_missing_file_is_an_empty_mapping(self, tmp_path):
        """Before curation begins, everyone is legitimately unmapped."""
        curation = load(tmp_path / "absent.yaml")
        assert curation.entries == {}
        assert curation.mapped_count == 0

    def test_bare_string_is_shorthand_for_confirmed(self, tmp_path):
        path = write(
            tmp_path,
            """
            faculty:
              "34100":
                name: Josh Alman
                links:
                  - A5006981411
            """,
        )
        entry = load(path).get("34100")
        assert entry.name == "Josh Alman"
        assert entry.confirmed_ids == ["A5006981411"]
        assert entry.links[0].status == CONFIRMED

    def test_list_shorthand_without_a_name(self, tmp_path):
        path = write(tmp_path, 'faculty:\n  "34100":\n    - A5006981411\n')
        assert load(path).confirmed_ids("34100") == ["A5006981411"]

    def test_null_entry_means_deliberately_unmapped(self, tmp_path):
        path = write(tmp_path, 'faculty:\n  "34100":\n')
        entry = load(path).get("34100")
        assert entry is not None
        assert not entry.is_mapped

    def test_multiple_confirmed_ids_are_kept(self, tmp_path):
        """OpenAlex splits one person across profiles; all confirmed ones count."""
        path = write(
            tmp_path,
            """
            faculty:
              "34100":
                links:
                  - openalex_id: A5006981411
                    is_primary: true
                  - openalex_id: A5005816789
            """,
        )
        entry = load(path).get("34100")
        assert entry.confirmed_ids == ["A5006981411", "A5005816789"]
        assert entry.primary_id == "A5006981411"

    def test_rejections_are_preserved(self, tmp_path):
        path = write(
            tmp_path,
            """
            faculty:
              "34100":
                links:
                  - openalex_id: A5006981411
                  - openalex_id: A5125667327
                    status: rejected
                    note: single-paper fragment
            """,
        )
        entry = load(path).get("34100")
        assert entry.confirmed_ids == ["A5006981411"]
        assert entry.rejected_ids == {"A5125667327"}

    def test_primary_defaults_to_the_first_confirmed_link(self, tmp_path):
        path = write(
            tmp_path, 'faculty:\n  "1":\n    links: [A1, A2]\n'
        )
        assert load(path).get("1").primary_id == "A1"

    def test_curated_at_is_parsed(self, tmp_path):
        path = write(
            tmp_path,
            """
            faculty:
              "1":
                links:
                  - openalex_id: A1
                    curated_at: "2026-09-22T10:00:00"
            """,
        )
        assert load(path).get("1").links[0].curated_at.year == 2026


class TestValidation:
    def test_unknown_status_is_rejected_by_name(self, tmp_path):
        path = write(
            tmp_path,
            'faculty:\n  "1":\n    links:\n      - openalex_id: A1\n        status: maybe\n',
        )
        with pytest.raises(CurationError, match="unknown status"):
            load(path)

    def test_missing_openalex_id_is_rejected(self, tmp_path):
        path = write(tmp_path, 'faculty:\n  "1":\n    links:\n      - note: oops\n')
        with pytest.raises(CurationError, match="missing openalex_id"):
            load(path)

    def test_duplicate_id_for_one_person_is_rejected(self, tmp_path):
        path = write(tmp_path, 'faculty:\n  "1":\n    links: [A1, A1]\n')
        with pytest.raises(CurationError, match="appears more than once"):
            load(path)

    def test_two_primaries_are_rejected(self, tmp_path):
        """Ambiguity here would silently pick one at random downstream."""
        path = write(
            tmp_path,
            """
            faculty:
              "1":
                links:
                  - openalex_id: A1
                    is_primary: true
                  - openalex_id: A2
                    is_primary: true
            """,
        )
        with pytest.raises(CurationError, match="is_primary"):
            load(path)

    def test_a_rejected_primary_does_not_count_as_a_conflict(self, tmp_path):
        path = write(
            tmp_path,
            """
            faculty:
              "1":
                links:
                  - openalex_id: A1
                    is_primary: true
                  - openalex_id: A2
                    is_primary: true
                    status: rejected
            """,
        )
        assert load(path).get("1").primary_id == "A1"


class TestQueries:
    @pytest.fixture
    def curation(self, tmp_path):
        path = write(
            tmp_path,
            """
            faculty:
              "34100":
                links: [A5006981411, A5005816789]
              "24532":
                links:
                  - openalex_id: A9999
                    status: rejected
              "31472":
            """,
        )
        return load(path)

    def test_mapped_count_ignores_rejected_only_entries(self, curation):
        assert curation.mapped_count == 1

    def test_unmapped_ids_include_rejected_only_and_empty(self, curation):
        assert set(curation.unmapped_person_ids) == {"24532", "31472"}

    def test_all_confirmed_flattens_to_pairs(self, curation):
        assert list(curation.all_confirmed()) == [
            ("34100", "A5006981411"),
            ("34100", "A5005816789"),
        ]

    def test_confirmed_ids_for_unknown_person_is_empty(self, curation):
        assert curation.confirmed_ids("00000") == []


class TestRoundTrip:
    def test_dump_then_load_preserves_decisions(self, tmp_path):
        source = write(
            tmp_path,
            """
            faculty:
              "34100":
                name: Josh Alman
                links:
                  - openalex_id: A5006981411
                    is_primary: true
                    note: primary profile
                  - openalex_id: A5125667327
                    status: rejected
                    note: single-paper fragment
            """,
        )
        original = load(source)
        target = tmp_path / "out.yaml"
        dump(original, target)
        reloaded = load(target)

        before = original.get("34100")
        after = reloaded.get("34100")
        assert after.name == before.name
        assert after.confirmed_ids == before.confirmed_ids
        assert after.rejected_ids == before.rejected_ids
        assert after.primary_id == before.primary_id
        assert after.links[1].note == "single-paper fragment"

    def test_dump_creates_the_directory(self, tmp_path):
        from labradar.curation import CurationFile, FacultyLinks

        curation = CurationFile(
            entries={"1": FacultyLinks("1", "A B", [Link("A1")])}
        )
        target = tmp_path / "nested" / "links.yaml"
        dump(curation, target)
        assert load(target).confirmed_ids("1") == ["A1"]
