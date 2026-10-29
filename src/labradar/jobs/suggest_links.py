"""Propose OpenAlex links for unmapped faculty, in bulk.

Writes proposals into the curation file with the evidence that produced them,
so every automatic decision arrives as a reviewable diff rather than as a silent
row in a database. Human decisions are never overwritten: an entry already
carrying a confirmed or rejected link is left exactly as the curator left it.

Three outcomes per person:

    confirmed  subject matter agrees and one profile is unambiguous
    candidate  a plausible profile exists but something is unclear
    none       no Columbia-affiliated profile matched the name at all

Declining is a normal, useful outcome. A wrong link yields a believable number
attached to the wrong researcher; an honest gap renders as "unknown", which the
interface already handles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from labradar.curation import CANDIDATE, CONFIRMED, REJECTED, CurationFile, FacultyLinks, Link
from labradar.matching import Decision, decide, query_name, score_candidates
from labradar.sources.openalex import BudgetExhausted, OpenAlexClient

log = logging.getLogger(__name__)

AUTHOR = "auto-suggester"


@dataclass
class FacultyInput:
    """What the matcher needs to know about one person."""

    person_id: str
    full_name: str
    interests_raw: str | None = None
    area_names: list[str] = field(default_factory=list)


@dataclass
class SuggestionReport:
    confirmed: list[tuple[str, str, Decision]] = field(default_factory=list)
    candidates: list[tuple[str, str, Decision]] = field(default_factory=list)
    unmatched: list[tuple[str, str, Decision]] = field(default_factory=list)
    skipped_already_curated: int = 0
    searched: int = 0
    budget_spent_usd: float = 0.0
    stopped_early: bool = False

    def summary(self) -> str:
        return (
            f"searched {self.searched}, confirmed {len(self.confirmed)}, "
            f"candidates {len(self.candidates)}, no match {len(self.unmatched)}, "
            f"skipped {self.skipped_already_curated} already curated, "
            f"${self.budget_spent_usd:.4f} spent"
        )


def _note(decision: Decision) -> str:
    return f"{decision.reason}. Proposed automatically; confirm or correct by hand."


def suggest(
    faculty: list[FacultyInput],
    curation: CurationFile,
    client: OpenAlexClient,
    *,
    include_curated: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
) -> SuggestionReport:
    """Search for each unmapped person and record a proposal.

    The curation file is mutated in place; the caller decides whether to write
    it out, so a dry run costs nothing but the searches.
    """
    now = now or datetime.now(timezone.utc)
    report = SuggestionReport()

    for person in faculty:
        entry = curation.entries.get(person.person_id)
        if entry is None:
            entry = FacultyLinks(person_id=person.person_id, name=person.full_name)
            curation.entries[person.person_id] = entry

        # Never overturn a human. A curated entry is the answer, not a starting
        # point, and re-searching it would spend budget to re-ask a settled
        # question.
        human_curated = any(
            l.curated_by not in (None, AUTHOR) or l.status == REJECTED
            for l in entry.links
        )
        # A human decision is final, and --all does not override it. Proposing
        # alongside one produced a second is_primary link and an invalid file.
        if human_curated or (not include_curated and entry.is_mapped):
            report.skipped_already_curated += 1
            continue

        if limit is not None and report.searched >= limit:
            break

        unscoped = False
        # Search on the name with initials removed; scoring still uses the
        # directory's full name, so precision is unaffected.
        search_as = query_name(person.full_name)
        try:
            candidates = client.search_candidates(search_as)
            if not candidates:
                # OpenAlex records a *last known* affiliation, so a recent hire
                # or a long prior career leaves someone invisible to the
                # Columbia-scoped search. Widen, but never auto-confirm the
                # result: without the institution constraint the only evidence
                # left is name and subject matter.
                candidates = client.search_by_name(search_as)
                unscoped = bool(candidates)
        except BudgetExhausted as exc:
            log.warning("stopping suggestions early: %s", exc)
            report.stopped_early = True
            break
        except Exception as exc:
            log.warning("search failed for %s: %s", person.full_name, exc)
            continue

        report.searched += 1
        scored = score_candidates(
            person.full_name, person.interests_raw, person.area_names, candidates
        )
        decision = decide(scored, already_rejected=entry.rejected_ids)
        if unscoped and decision.status == "confirmed":
            decision.status = "candidate"
            decision.confidence = min(decision.confidence, 0.5)
            decision.reason = (
                f"{decision.reason}. Found only by unscoped name search -- "
                "OpenAlex does not list them at Columbia, so verify this is the "
                "right person"
            )
            decision.rejected = []

        entry.name = person.full_name
        # Replace only this tool's previous guesses, leaving human rows intact.
        entry.links = [l for l in entry.links if l.curated_by != AUTHOR]

        if decision.chosen is not None:
            entry.links.append(
                Link(
                    openalex_id=decision.chosen.openalex_id,
                    status=CONFIRMED if decision.status == "confirmed" else CANDIDATE,
                    is_primary=decision.status == "confirmed",
                    confidence=decision.confidence,
                    note=_note(decision),
                    curated_by=AUTHOR,
                    curated_at=now,
                    evidence=decision.chosen.evidence(),
                )
            )

        # Slivers left over once a real profile is identified are duplicates of
        # it. Recording them stops the same wrong suggestion returning weekly.
        for fragment in decision.rejected:
            entry.links.append(
                Link(
                    openalex_id=fragment.openalex_id,
                    status=REJECTED,
                    note=(
                        f"Fragment profile ({fragment.works_count} works, "
                        f"last {fragment.last_year}) under the same name as the "
                        "confirmed profile."
                    ),
                    curated_by=AUTHOR,
                    curated_at=now,
                    evidence=fragment.evidence(),
                )
            )

        row = (person.person_id, person.full_name, decision)
        if decision.status == "confirmed":
            report.confirmed.append(row)
        elif decision.chosen is not None:
            report.candidates.append(row)
        else:
            report.unmatched.append(row)

    report.budget_spent_usd = client.budget.spent_usd
    return report
