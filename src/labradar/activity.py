"""Turn a person's work records into the single signal the app displays.

Activity is one number with a date: papers published since a cutoff year, and
when they last published at all. There is deliberately no "is this group active"
predicate -- the reader decides what counts, and the app only shows what
happened and when.

The cutoff spans the current calendar year and the one before it. OpenAlex
records publication years rather than dates and its index lags real publication
by months, so counting only the current year reports working researchers as
silent. Two years is the smallest span that does not manufacture false silence.

The count is therefore *not* "last year", and an earlier version of this module
called it that -- producing cards that read "5 papers last year" from a figure
covering 2025 and 2026. Callers get :func:`since_year` alongside the count so
the label is rendered from the same value the count was taken with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from labradar.dedup import (
    WorkCluster,
    WorkRecord,
    cluster_works,
    count_raw_since,
    count_since,
    last_publication_year,
)

# How many calendar years back the activity window reaches, in addition to the
# current one. See the module docstring for why this is not 0.
ACTIVITY_LOOKBACK_YEARS = 1


def since_year(as_of: date) -> int:
    """The earliest publication year the activity count includes."""
    return as_of.year - ACTIVITY_LOOKBACK_YEARS


@dataclass(frozen=True)
class ActivitySignals:
    """What we can say about one person's publishing, and as of when.

    ``linked_author_count == 0`` means nobody has mapped this person to a
    publication record yet. That is materially different from "has not
    published": the first is our ignorance, the second is a finding. Callers
    must render them differently, which is what :attr:`is_unmapped` is for.
    """

    as_of_date: date
    last_pub_year: int | None
    works_since: int
    works_total: int
    raw_records_since: int
    linked_author_count: int

    @property
    def is_unmapped(self) -> bool:
        return self.linked_author_count == 0

    @property
    def since_year(self) -> int:
        """The cutoff the count was taken from, for rendering the label."""
        return since_year(self.as_of_date)

    @property
    def is_inactive(self) -> bool:
        """Nothing published since the cutoff -- and we are in a position to know.

        False for anyone unmapped: with no linked record, silence is an absence
        of evidence, not evidence of absence.
        """
        return not self.is_unmapped and self.works_since == 0

    @property
    def years_since_last_publication(self) -> int | None:
        if self.last_pub_year is None:
            return None
        return self.as_of_date.year - self.last_pub_year



def compute_signals(
    records: Sequence[WorkRecord], *, as_of: date, linked_author_count: int
) -> tuple[ActivitySignals, list[WorkCluster]]:
    """Derive signals from the union of every confirmed profile's works.

    The union is deduplicated in one pass so a paper appearing under two of a
    person's duplicate profiles counts once. Clusters are returned alongside the
    signals so the caller can persist them and keep the counts auditable.
    """
    clusters = cluster_works(records)
    cutoff = since_year(as_of)

    return (
        ActivitySignals(
            as_of_date=as_of,
            last_pub_year=last_publication_year(clusters),
            works_since=count_since(clusters, cutoff),
            works_total=len(clusters),
            raw_records_since=count_raw_since(records, cutoff),
            linked_author_count=linked_author_count,
        ),
        clusters,
    )


def unmapped_signals(*, as_of: date) -> ActivitySignals:
    """Signals for someone with no confirmed publication record.

    The zeroes here mean "not known", and :attr:`ActivitySignals.is_unmapped` is
    what tells the caller so.
    """
    return ActivitySignals(
        as_of_date=as_of,
        last_pub_year=None,
        works_since=0,
        works_total=0,
        raw_records_since=0,
        linked_author_count=0,
    )
