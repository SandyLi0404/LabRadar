"""Candidate-matching tests.

Built around two real cases where the obvious approach picks the wrong person.
Ranking Columbia-affiliated candidates by publication count gives Zhou Yu (NLP)
a mathematical finance profile with 392 works, and Daniel Bauer (NLP) an
insurance and mortality profile with 237. Both produce believable activity
numbers for the wrong researcher, which is the failure this module exists to
prevent.
"""

from __future__ import annotations

import pytest

from labradar.matching import (
    decide,
    names_compatible,
    score_candidates,
    subject_tokens,
    tokenize,
    topic_overlap,
)
from labradar.sources.openalex import AuthorProfile


def profile(openalex_id, name, works, topics, *, year=2026, orcid=None):
    return AuthorProfile(
        openalex_id=openalex_id,
        display_name=name,
        orcid=orcid,
        works_count=works,
        cited_by_count=0,
        last_known_institution="Columbia University",
        counts_by_year={year: 1},
        topics=topics,
    )


NLP = ["Natural Language Processing Techniques", "Topic Modeling"]
FINANCE = ["Stochastic processes and financial applications", "Risk and Portfolio Optimization"]
INSURANCE = ["Insurance, Mortality, Demography", "Risk Management"]


class TestNameCompatibility:
    def test_middle_initial_is_not_a_difference(self):
        assert names_compatible("Daniel Bauer", "Daniel J. Bauer")

    def test_word_order_does_not_matter(self):
        """OpenAlex writes the same researcher both ways."""
        assert names_compatible("Zhou Yu", "Yu Zhou")

    def test_a_spelled_out_middle_name_still_matches(self):
        assert names_compatible("Julia B. Hirschberg", "Julia Bell Hirschberg")

    def test_the_name_check_is_deliberately_permissive(self):
        """'Zhou Yu' passes against 'Xun Yu Zhou', a different researcher.

        Documented rather than fixed: tightening this would also reject real
        middle-name variants. The name check only narrows the field, and the
        test below shows subject matter rejecting this pair on the merits.
        """
        assert names_compatible("Zhou Yu", "Xun Yu Zhou")

    def test_unrelated_names_do_not_match(self):
        assert not names_compatible("Josh Alman", "Kathleen McKeown")

    def test_missing_name_never_matches(self):
        assert not names_compatible("Josh Alman", None)


class TestTopicOverlap:
    def test_matching_subject_scores(self):
        subjects = subject_tokens("natural language processing, dialog", ["NLP & Speech"])
        assert topic_overlap(subjects, profile("A1", "X", 50, NLP)) > 0.2

    def test_unrelated_subject_scores_zero(self):
        subjects = subject_tokens("natural language processing", ["NLP & Speech"])
        assert topic_overlap(subjects, profile("A1", "X", 50, INSURANCE)) == 0.0

    def test_stopwords_do_not_manufacture_overlap(self):
        """'and'/'of'/'research' appear everywhere and must not count."""
        assert "and" not in tokenize("Insurance and Research of Mortality")
        assert "research" not in tokenize("Insurance and Research of Mortality")

    def test_no_topics_scores_zero(self):
        subjects = subject_tokens("anything", [])
        assert topic_overlap(subjects, profile("A1", "X", 50, [])) == 0.0


class TestTheMisleadingCases:
    """The two profiles that publication count would have chosen."""

    def test_finance_profile_does_not_win_over_the_nlp_one(self):
        candidates = [
            profile("A_FINANCE", "Xun Yu Zhou", 392, FINANCE),
            profile("A_NLP", "Zhou Yu", 279, NLP),
        ]
        decision = decide(
            score_candidates("Zhou Yu", "natural language processing, dialog systems",
                             ["NLP & Speech"], candidates)
        )
        assert decision.status == "confirmed"
        assert decision.chosen.openalex_id == "A_NLP"

    def test_insurance_profile_does_not_win_over_the_nlp_one(self):
        candidates = [
            profile("A_INSURANCE", "Daniel J. Bauer", 237, INSURANCE),
            profile("A_NLP", "Daniel Bauer", 39, NLP),
        ]
        decision = decide(
            score_candidates("Daniel Bauer", "natural language processing",
                             ["NLP & Speech"], candidates)
        )
        assert decision.status == "confirmed"
        assert decision.chosen.openalex_id == "A_NLP"
        assert decision.chosen.works_count == 39  # the smaller profile is right


class TestFragments:
    """A one-paper profile can score perfect topic overlap; it must not win."""

    def test_a_fragment_does_not_outrank_the_real_career(self):
        candidates = [
            profile("A_FRAGMENT", "Baishakhi Ray", 1, NLP),
            profile("A_REAL", "Baishakhi Ray", 199, NLP),
        ]
        decision = decide(
            score_candidates("Baishakhi Ray", "software engineering", [], candidates)
        )
        assert decision.chosen.openalex_id == "A_REAL"

    def test_fragments_are_recorded_as_rejected(self):
        """So the same wrong suggestion does not return every week."""
        candidates = [
            profile("A_FRAGMENT", "Baishakhi Ray", 1, NLP),
            profile("A_REAL", "Baishakhi Ray", 199, NLP),
        ]
        decision = decide(
            score_candidates("Baishakhi Ray", "natural language", ["NLP & Speech"], candidates)
        )
        assert [c.openalex_id for c in decision.rejected] == ["A_FRAGMENT"]

    def test_only_small_profiles_yields_a_review_not_a_confirmation(self):
        candidates = [profile("A1", "New Person", 2, NLP)]
        decision = decide(
            score_candidates("New Person", "natural language", ["NLP & Speech"], candidates)
        )
        assert decision.status == "candidate"
        assert "small" in decision.reason

    def test_previously_rejected_ids_are_not_re_proposed(self):
        candidates = [profile("A_REJECTED", "Someone Here", 300, NLP)]
        decision = decide(
            score_candidates("Someone Here", "natural language", ["NLP & Speech"], candidates),
            already_rejected={"A_REJECTED"},
        )
        assert decision.status == "none"


class TestDeclining:
    """Declining is a normal outcome; a wrong link is worse than a gap."""

    def test_no_candidates_declines(self):
        assert decide(score_candidates("Nobody Here", None, [], [])).status == "none"

    def test_no_name_match_declines(self):
        candidates = [profile("A1", "Someone Else Entirely", 300, NLP)]
        decision = decide(score_candidates("Jane Doe", "nlp", ["NLP & Speech"], candidates))
        assert decision.status == "none"
        assert decision.chosen is None

    def test_no_topic_signal_asks_for_review(self):
        candidates = [profile("A1", "Jane Doe", 300, INSURANCE)]
        decision = decide(score_candidates("Jane Doe", "nlp", ["NLP & Speech"], candidates))
        assert decision.status == "candidate"

    def test_two_similar_subjects_ask_for_review(self):
        candidates = [
            profile("A1", "Jane Doe", 300, NLP),
            profile("A2", "Jane Doe", 280, NLP),
        ]
        decision = decide(
            score_candidates("Jane Doe", "natural language processing",
                             ["NLP & Speech"], candidates)
        )
        assert decision.status == "candidate"
        assert "needs a human" in decision.reason

    def test_confidence_never_reaches_certainty(self):
        candidates = [profile("A1", "Jane Doe", 300, NLP)]
        decision = decide(
            score_candidates("Jane Doe", "natural language processing",
                             ["NLP & Speech"], candidates)
        )
        assert 0 < decision.confidence <= 0.95


class TestEvidence:
    def test_evidence_records_what_the_decision_rested_on(self):
        candidates = [profile("A1", "Jane Doe", 300, NLP, orcid="x")]
        decision = decide(
            score_candidates("Jane Doe", "natural language processing",
                             ["NLP & Speech"], candidates)
        )
        evidence = decision.chosen.evidence()
        assert evidence["works_count"] == 300
        assert evidence["name_matches"] is True
        assert evidence["has_orcid"] is True
        assert evidence["topic_score"] > 0
        assert evidence["topics"]
