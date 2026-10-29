"""Scoring OpenAlex candidates against a faculty member.

Ranking candidates by publication count is the obvious approach and it is
wrong. Measured against real Columbia searches:

    Zhou Yu (NLP)       top by works = "Xun Yu Zhou", 392 works, mathematical
                        finance. The correct profile ranks third.
    Daniel Bauer (NLP)  top by works = "Daniel J. Bauer", 237 works, insurance
                        and mortality. The correct profile ranks second.

Both would have been mislinked, and the resulting activity numbers would look
entirely plausible. What separates them is subject matter, so the decisive
signal here is overlap between a candidate's OpenAlex topics and what the
department directory says the person actually works on.

Nothing in this module writes a mapping. It proposes, with its reasoning
attached, and anything short of an unambiguous match is left for a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from labradar.sources.openalex import AuthorProfile

# Words that carry no subject information in either interests text or OpenAlex
# topic labels, and would otherwise manufacture overlap between unrelated fields.
STOPWORDS = frozenset(
    """
    a an and are as at be by for from in into of on or the to with within
    advanced applications approach approaches analysis based general method
    methods methodologies research studies study technique techniques
    topics use using various
    """.split()
)
# "theory" is NOT a stopword here: it is one of the department's eleven areas
# and the whole subject of several faculty. Removing it cost nothing elsewhere
# and is the only signal a theorist with a blank interests line has.

# A candidate below this is a fragment, not a career.
MIN_WORKS_TO_CONFIRM = 15

# Subject agreement required before a link is proposed without review.
MIN_TOPIC_SCORE_TO_CONFIRM = 0.12

# How far ahead of the runner-up the winner must be on subject matter.
MIN_TOPIC_MARGIN = 0.06

# Absent any topic signal, a candidate must tower over the runner-up on volume.
MIN_WORKS_RATIO = 8.0

_WORD = re.compile(r"[a-z0-9]+")
_INITIAL = re.compile(r"^[a-z]$")


# Suffix rules, longest first. Applied once, and only when a real stem remains.
# This is not a general stemmer -- it exists to make a directory's phrasing and
# OpenAlex's topic labels comparable, where the same field is routinely written
# two ways: "networking" against "network", "algorithms" against "algorithmic",
# "theory" against "theories". Those near-misses scored zero overlap and sent
# obvious matches to manual review.
_STEM_RULES = (
    ("ies", "y", 5),
    ("ing", "", 6),
    ("ical", "ic", 6),
    ("ment", "", 7),
    ("ed", "", 5),
    ("al", "", 6),
    ("ic", "", 6),
    ("s", "", 4),
)

# Words where a trailing "s" is part of the word, not a plural.
_NOT_PLURAL = ("ss", "us", "is")


def stem(word: str) -> str:
    """Reduce a word to a comparable stem."""
    for suffix, replacement, min_length in _STEM_RULES:
        if not word.endswith(suffix) or len(word) < min_length:
            continue
        if suffix == "s" and word.endswith(_NOT_PLURAL):
            continue
        return word[: -len(suffix)] + replacement
    return word


def tokenize(text: str | None) -> set[str]:
    """Content words, lowercased, stopwords dropped, reduced to stems."""
    if not text:
        return set()
    return {
        stem(w)
        for w in _WORD.findall(text.lower())
        if w not in STOPWORDS and len(w) > 2
    }


def name_tokens(name: str | None) -> set[str]:
    """Name parts, with single-letter initials dropped.

    Dropping initials lets "Daniel J. Bauer" match "Daniel Bauer" while keeping
    "Xun Yu Zhou" distinct from "Zhou Yu" -- the extra given name is a real
    difference, a middle initial is not.
    """
    if not name:
        return set()
    return {w for w in _WORD.findall(name.lower()) if not _INITIAL.match(w)}


def query_name(name: str) -> str:
    """The form of a name to search OpenAlex with.

    Middle initials must be dropped before querying, not merely before
    comparing. OpenAlex matches ``display_name.search`` on the tokens given, so
    an initial the profile does not carry returns nothing at all: searching
    "Daniel J Hsu" yields zero candidates while "Daniel Hsu" returns his
    217-work Columbia profile. Every faculty member the directory lists with a
    middle initial was invisible to this search until it was stripped here.
    """
    words = [w for w in name.split() if len(w.strip(".")) > 1]
    return " ".join(words) or name


def names_compatible(faculty_name: str, candidate_name: str | None) -> bool:
    """Whether two renderings could name the same person.

    Set comparison, so word order does not matter: OpenAlex writes both
    "Zhou Yu" and "Yu Zhou" for the same researcher. A subset also passes, so
    "Julia B. Hirschberg" matches a profile spelling out "Julia Bell
    Hirschberg".

    That deliberately errs towards recall, and it lets through pairs that are
    not the same person at all -- "Zhou Yu" is a subset of "Xun Yu Zhou", a
    different researcher entirely. This check only narrows the field. Subject
    matter is what actually decides, which is why an unrelated profile with
    three times the publications still loses.
    """
    faculty = name_tokens(faculty_name)
    candidate = name_tokens(candidate_name)
    if not faculty or not candidate:
        return False
    return faculty == candidate or faculty <= candidate or candidate <= faculty


def topic_overlap(subject_tokens: set[str], candidate: AuthorProfile) -> float:
    """Agreement between what we know they study and what the profile says.

    Scored against the candidate's own topic vocabulary, so a researcher with
    few listed topics is not penalised for the department's verbose interests
    line.
    """
    candidate_tokens = set()
    for topic in candidate.topics:
        candidate_tokens |= tokenize(topic)
    if not candidate_tokens or not subject_tokens:
        return 0.0
    return len(subject_tokens & candidate_tokens) / len(candidate_tokens)


def subject_tokens(interests_raw: str | None, area_names: list[str]) -> set[str]:
    """What the department says this person works on.

    Area names are included because the area pages are sourced rather than
    inferred, so they are the more reliable half when the interests line is
    terse or missing.
    """
    tokens = tokenize(interests_raw)
    for area in area_names:
        tokens |= tokenize(area)
    return tokens


@dataclass
class ScoredCandidate:
    profile: AuthorProfile
    name_ok: bool
    topic_score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def openalex_id(self) -> str:
        return self.profile.openalex_id

    @property
    def works_count(self) -> int:
        return self.profile.works_count

    @property
    def last_year(self) -> int | None:
        counts = self.profile.counts_by_year
        return max(counts) if counts else None

    def evidence(self) -> dict[str, object]:
        return {
            "works_count": self.works_count,
            "last_year": self.last_year,
            "topic_score": round(self.topic_score, 3),
            "topics": self.profile.topics[:3],
            "name_matches": self.name_ok,
            "has_orcid": bool(self.profile.orcid),
        }


@dataclass
class Decision:
    """A proposal, with the reasoning that produced it."""

    status: str  # 'confirmed' | 'candidate' | 'none'
    chosen: ScoredCandidate | None
    confidence: float
    reason: str
    rejected: list[ScoredCandidate] = field(default_factory=list)
    runners_up: list[ScoredCandidate] = field(default_factory=list)


def score_candidates(
    faculty_name: str,
    interests_raw: str | None,
    area_names: list[str],
    candidates: list[AuthorProfile],
) -> list[ScoredCandidate]:
    subjects = subject_tokens(interests_raw, area_names)
    scored = [
        ScoredCandidate(
            profile=c,
            name_ok=names_compatible(faculty_name, c.display_name),
            topic_score=topic_overlap(subjects, c),
        )
        for c in candidates
    ]
    scored.sort(key=lambda s: (s.topic_score, s.works_count), reverse=True)
    return scored


def decide(
    scored: list[ScoredCandidate],
    *,
    already_rejected: set[str] = frozenset(),
    min_works: int = MIN_WORKS_TO_CONFIRM,
    min_topic: float = MIN_TOPIC_SCORE_TO_CONFIRM,
    min_margin: float = MIN_TOPIC_MARGIN,
    min_ratio: float = MIN_WORKS_RATIO,
) -> Decision:
    """Choose a profile, or decline to.

    Declining is a normal outcome. A wrong link produces a believable number
    attached to the wrong researcher, which is worse than an honest gap the
    interface already knows how to render as "unknown".
    """
    live = [s for s in scored if s.openalex_id not in already_rejected]
    if not live:
        return Decision("none", None, 0.0, "no Columbia-affiliated candidates")

    plausible = [s for s in live if s.name_ok]
    if not plausible:
        return Decision(
            "none", None, 0.0, "no candidate's name matches", runners_up=live[:3]
        )

    # Fragments have to be set aside *before* ranking, not merely rejected
    # afterwards. Topic overlap is scored against the candidate's own
    # vocabulary, so a profile holding one paper on the right subject scores
    # near-perfectly and outranks the real career it was split from.
    substantial = [s for s in plausible if s.works_count >= min_works]
    fragments = [s for s in plausible if s.works_count < min_works]

    if not substantial:
        best = plausible[0]
        return Decision(
            "candidate",
            best,
            0.3,
            (
                f"every candidate is small (best has {best.works_count} works); "
                "may be an early-career profile or a fragment"
            ),
            runners_up=plausible[1:4],
        )

    best = substantial[0]
    rest = substantial[1:]
    runner_up = rest[0] if rest else None

    # Subject matter is the signal that separates same-name researchers.
    if best.topic_score >= min_topic:
        margin = best.topic_score - (runner_up.topic_score if runner_up else 0.0)
        if runner_up is None or margin >= min_margin:
            confidence = min(0.95, 0.6 + best.topic_score + margin)
            return Decision(
                "confirmed",
                best,
                round(confidence, 2),
                (
                    f"topics match the directory's interests "
                    f"(score {best.topic_score:.2f}"
                    + (f", {margin:.2f} clear of the runner-up" if runner_up else "")
                    + f"); {best.works_count} works"
                ),
                # Once a real profile is identified, the leftover slivers under
                # the same name are duplicates of it, so they are worth
                # recording as rejected rather than re-proposing every week.
                rejected=fragments,
                runners_up=rest[:3],
            )
        return Decision(
            "candidate",
            best,
            0.5,
            (
                f"two candidates have similar subject matter "
                f"({best.topic_score:.2f} vs {runner_up.topic_score:.2f}); needs a human"
            ),
            runners_up=rest[:3],
        )

    # No topic signal at all -- only accept an overwhelming volume difference.
    if runner_up and best.works_count >= runner_up.works_count * min_ratio:
        return Decision(
            "candidate",
            best,
            0.45,
            (
                f"no topic overlap to judge on, but {best.works_count} works vs "
                f"{runner_up.works_count}; verify the subject area"
            ),
            runners_up=rest[:3],
        )

    return Decision(
        "candidate",
        best,
        0.35,
        (
            f"subject overlap too weak to confirm "
            f"({best.topic_score:.2f} < {min_topic:.2f}); needs a human"
            if best.topic_score
            else "no subject overlap with the directory's interests; needs a human"
        ),
        runners_up=rest[:3],
    )
