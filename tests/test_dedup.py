"""Dedup tests pinned to real OpenAlex data.

Counts are taken at the cutoff the app itself reports from (``since_year``).
The guards against the rejected keying schemes run over the whole corpus
instead: inside one short window most versions share a year, so a broken scheme
can land on the right number by luck.

The fixtures are the live work lists for Josh Alman's two Columbia-affiliated
OpenAlex profiles -- a 63-work primary profile (A) and a 7-work duplicate (B).
Every expected number here was measured against the API before the rule was
written, so the measurement, the rule and these assertions agree by construction.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from labradar.activity import since_year
from labradar.dedup import (
    WorkRecord,
    plausible_year,
    cluster_works,
    count_since,
    count_raw_since,
    doi_norm,
    last_publication_year,
    title_norm,
)

FIXTURES = Path(__file__).parent / "fixtures"

# The corpus was captured in 2026; the cutoff is computed against that baseline
# so the expected counts stay stable as real time moves on.
AS_OF_YEAR = 2026
CUTOFF = since_year(date(AS_OF_YEAR, 1, 1))

PROFILE_A = "A5006981411"  # the real profile: 63 works
PROFILE_B = "A5005816789"  # duplicate fragment: 7 works


def load(author_id: str) -> list[WorkRecord]:
    raw = json.loads((FIXTURES / f"works_{author_id}.json").read_text(encoding="utf-8"))
    return [WorkRecord.from_api(w) for w in raw["results"]]


@pytest.fixture
def profile_a() -> list[WorkRecord]:
    return load(PROFILE_A)


@pytest.fixture
def profile_b() -> list[WorkRecord]:
    return load(PROFILE_B)


class TestNormalization:
    def test_strips_punctuation_case_and_spacing(self):
        assert (
            title_norm("A Refined Laser Method and Faster Matrix Multiplication")
            == "arefinedlasermethodandfastermatrixmultiplication"
        )

    def test_folds_accents_so_versions_match(self):
        assert title_norm("Schrödinger's Bridge") == title_norm("Schrodingers Bridge")

    def test_handles_missing_title(self):
        assert title_norm(None) == ""

    @pytest.mark.parametrize(
        "raw",
        ["https://doi.org/10.1137/1.9781611976465.32", "10.1137/1.9781611976465.32"],
    )
    def test_doi_normalizes_to_bare_form(self, raw):
        assert doi_norm(raw) == "10.1137/1.9781611976465.32"

    def test_doi_absent(self):
        assert doi_norm(None) is None


class TestProfileACounts:
    """A single profile is already inflated by its own version duplicates."""

    def test_raw_record_counts(self, profile_a):
        assert len(profile_a) == 65
        assert count_raw_since(profile_a, CUTOFF) == 14

    def test_deduped_counts(self, profile_a):
        assert count_since(cluster_works(profile_a), CUTOFF) == 13

    def test_collapses_to_expected_paper_count(self, profile_a):
        # 46 titled papers plus one untitled record that can never be matched.
        assert len(cluster_works(profile_a)) == 47

    def test_untitled_record_stands_alone(self, profile_a):
        """OpenAlex has one titled-nothing article here; it must not pool with others.

        Every untitled record would normalize to the same empty string, so
        merging on it would silently collapse unrelated papers together.
        """
        untitled = [
            c for c in cluster_works(profile_a) if c.cluster_key.startswith("id:")
        ]
        assert len(untitled) == 1
        assert untitled[0].record_count == 1
        assert untitled[0].canonical_year == 2021  # before the cutoff


class TestMergedProfiles:
    """Cross-profile duplicates merge on the same pass as within-profile ones."""

    def test_raw_counts_inflate_further(self, profile_a, profile_b):
        assert count_raw_since(profile_a + profile_b, CUTOFF) == 16

    def test_deduped_counts(self, profile_a, profile_b):
        assert count_since(cluster_works(profile_a + profile_b), CUTOFF) == 14

    def test_the_only_shared_title_predates_the_cutoff(self, profile_a, profile_b):
        """Documents why cross-profile merging is a no-op for *this* pair.

        These two profiles overlap on exactly one paper, from 2017, so it falls
        before the cutoff. All of the inflation here is within-profile
        versioning -- which is the case for deduping everyone
        rather than only people with multiple linked profiles.
        """
        shared = {title_norm(r.title) for r in profile_a} & {
            title_norm(r.title) for r in profile_b
        }
        shared.discard("")
        assert len(shared) == 1
        years = {
            r.publication_year
            for r in profile_a + profile_b
            if title_norm(r.title) in shared
        }
        assert years == {2017}

        separately = count_since(cluster_works(profile_a), CUTOFF) + count_since(
            cluster_works(profile_b), CUTOFF
        )
        together = count_since(cluster_works(profile_a + profile_b), CUTOFF)
        assert separately == together

    def test_union_dedup_catches_an_in_window_cross_profile_duplicate(self):
        """The general case the union pass exists for, which the fixtures lack.

        Two profiles each carrying a version of the same recent paper must count
        it once, not twice.
        """
        left = [WorkRecord("W1", "Tensor Powers And Faster Multiplication", 2025)]
        right = [
            WorkRecord("W2", "Tensor Powers and faster multiplication!", 2026, "10.1/x")
        ]
        assert count_since(cluster_works(left), CUTOFF) == 1
        assert count_since(cluster_works(right), CUTOFF) == 1
        merged = cluster_works(left + right)
        assert count_since(merged, CUTOFF) == 1
        assert merged[0].canonical_year == 2026


class TestThreeVersionGroup:
    """The worst real case: preprint, conference and journal, 3 DOIs, 4 years apart."""

    TITLE = title_norm("A Refined Laser Method and Faster Matrix Multiplication")

    def find(self, clusters):
        matches = [c for c in clusters if c.cluster_key.startswith(self.TITLE)]
        assert len(matches) == 1, "expected exactly one cluster for this title"
        return matches[0]

    def test_three_records_collapse_to_one_paper(self, profile_a):
        assert self.find(cluster_works(profile_a)).record_count == 3

    def test_canonical_year_is_the_published_one(self, profile_a):
        # 2020 arXiv -> 2021 SIAM conference -> 2024 TheoretiCS journal
        assert self.find(cluster_works(profile_a)).canonical_year == 2024

    def test_versions_really_do_have_distinct_dois(self, profile_a):
        records = self.find(cluster_works(profile_a)).records
        assert len({doi_norm(r.doi) for r in records}) == 3

    def test_representative_is_not_the_preprint(self, profile_a):
        assert not self.find(cluster_works(profile_a)).representative.is_preprint


class TestRejectedKeyingSchemes:
    """Guards so a refactor back to either broken scheme fails loudly.

    Both of these look reasonable and both were measured to be wrong; without
    these tests the regression would be invisible in the UI. They count papers
    across the whole corpus, where every duplicate group is in play.
    """

    def test_doi_first_keying_merges_nothing(self, profile_a):
        """Every version is independently registered, so DOI keying is a no-op."""
        keys = {
            doi_norm(r.doi) or f"{title_norm(r.title)}|{r.publication_year}"
            for r in profile_a
        }
        assert len(keys) == len(profile_a), "DOI-first keying must not be reinstated"

    def test_title_plus_exact_year_keying_undercounts_merges(self, profile_a):
        """Version years straddle boundaries, so exact-year keying splits papers."""
        keys = {
            f"{title_norm(r.title)}|{r.publication_year}"
            for r in profile_a
            if title_norm(r.title)
        }
        # 59 title-year keys, plus the one untitled record standing alone.
        assert len(keys) + 1 == 60, "title||year keying must not be reinstated"

    def test_both_differ_from_the_correct_answer(self, profile_a):
        assert len(cluster_works(profile_a)) == 47


class TestYearTolerance:
    """The chosen constant sits inside a stable plateau.

    Below four years the 2020 preprint and 2024 journal version of the laser
    method paper split apart, so the plateau starts there.
    """

    @pytest.mark.parametrize("tolerance", [4, 5, 6, 7])
    def test_counts_are_flat_across_the_plateau(self, profile_a, tolerance):
        assert len(cluster_works(profile_a, year_tolerance=tolerance)) == 47

    def test_exact_year_matching_is_the_failure_mode(self, profile_a):
        assert len(cluster_works(profile_a, year_tolerance=0)) == 60


class TestLastPublicationYear:
    """Immune to duplicate inflation, which is why it stays trustworthy."""

    def test_uses_max_across_clusters(self, profile_a):
        assert last_publication_year(cluster_works(profile_a)) == 2026

    def test_unaffected_by_merging_a_duplicate_profile(self, profile_a, profile_b):
        assert last_publication_year(cluster_works(profile_a)) == last_publication_year(
            cluster_works(profile_a + profile_b)
        )

    def test_none_when_nothing_is_dated(self):
        records = [WorkRecord("W1", "Undated Paper About Something", None)]
        assert last_publication_year(cluster_works(records)) is None


class TestGenericTitleGuard:
    """Short titles must not vacuum unrelated papers into one cluster."""

    def test_short_titles_do_not_merge_on_title_alone(self):
        records = [
            WorkRecord("W1", "Editorial", 2024, doi="10.1/a"),
            WorkRecord("W2", "Editorial", 2025, doi="10.1/b"),
        ]
        assert len(cluster_works(records)) == 2

    def test_long_titles_still_merge_across_the_window(self):
        records = [
            WorkRecord("W1", "Faster Matrix Multiplication Via Tensor Powers", 2024),
            WorkRecord("W2", "Faster Matrix Multiplication Via Tensor Powers", 2026),
        ]
        assert len(cluster_works(records)) == 1

    def test_gap_beyond_tolerance_splits_even_long_titles(self):
        records = [
            WorkRecord("W1", "Faster Matrix Multiplication Via Tensor Powers", 2010),
            WorkRecord("W2", "Faster Matrix Multiplication Via Tensor Powers", 2026),
        ]
        assert len(cluster_works(records)) == 2


class TestIdenticalDoiFastPath:
    def test_same_doi_listed_twice_counts_once(self):
        records = [
            WorkRecord("W1", "Some Paper With A Sufficiently Long Title", 2025, "10.1/x"),
            WorkRecord("W2", "Some Paper With A Sufficiently Long Title", 2025, "10.1/x"),
        ]
        clusters = cluster_works(records)
        assert len(clusters) == 1
        assert clusters[0].record_count == 1


class TestPlausibleYear:
    """OpenAlex returned a real author record with a count filed under year 10000.

    A single value like that would dominate any "most recent publication"
    figure, which is the headline number on every card.
    """

    @pytest.mark.parametrize("year", [1950, 2000, 2026])
    def test_ordinary_years_pass(self, year):
        assert plausible_year(year, as_of=2026) == year

    def test_next_year_is_allowed_for_accepted_papers(self):
        assert plausible_year(2027, as_of=2026) == 2027

    @pytest.mark.parametrize("year", [10000, 2028, 1890, -5])
    def test_implausible_years_become_unknown(self, year):
        assert plausible_year(year, as_of=2026) is None

    @pytest.mark.parametrize("value", [None, "not a year", ""])
    def test_junk_becomes_unknown(self, value):
        assert plausible_year(value, as_of=2026) is None

    def test_a_bad_year_cannot_inflate_recency(self):
        records = [
            WorkRecord.from_api(
                {"id": "https://openalex.org/W1", "title": "A Real Paper Title Here",
                 "publication_year": 2019}
            ),
            WorkRecord.from_api(
                {"id": "https://openalex.org/W2", "title": "A Bogus Dated Paper Title",
                 "publication_year": 10000}
            ),
        ]
        assert last_publication_year(cluster_works(records)) == 2019
