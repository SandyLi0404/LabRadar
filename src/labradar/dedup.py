"""Work-level deduplication for OpenAlex records.

OpenAlex stores each *version* of a paper as a separate work record: the arXiv
preprint, the conference proceedings version and the journal article are three
rows, each with its own DOI and often its own publication year. Counting raw
records therefore overstates a researcher's recent output by roughly 15-20%,
which is exactly the signal this project exists to report.

Two keying schemes that look obvious both fail on real data (measured over Josh
Alman's corpus, kept as a fixture in tests/fixtures/):

  * DOI-first keying merges *nothing* -- all 16 duplicate groups had differing
    DOIs, because every version is independently registered.
  * ``title_norm || year`` fails on 12 of those 16, because versions straddle
    year boundaries (one group spans 2020 -> 2021 -> 2024).

So clustering keys on the normalized title and tolerates a year gap. DOI is used
only as a fast path for byte-identical records, never as a splitter.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Widest version spread observed in the reference corpus was 4 years (a 2020
# preprint and its 2024 journal version), so cluster counts are flat for
# tolerances 4-8 and this value sits inside that plateau. Exact-year keying
# (tolerance 0) is the failure mode.
YEAR_TOLERANCE = 5

# Below this length a normalized title is too generic to merge on alone
# ("frontmatter", "editorial"). The shortest real title in the reference corpus
# normalizes to 29 characters, so this threshold cannot cause a false merge there.
MIN_TITLE_LEN = 15

# OpenAlex occasionally carries a nonsense publication year -- a Columbia
# author record came back with a count filed under the year 10000. Anything
# outside this range is treated as unknown rather than displayed, since a
# single bad value would otherwise dominate a "most recent publication" figure.
MIN_PLAUSIBLE_YEAR = 1900
FUTURE_YEAR_ALLOWANCE = 1  # papers accepted for next year are legitimate


def plausible_year(year: int | None, *, as_of: int | None = None) -> int | None:
    """Return the year if it could be a real publication year, else None."""
    if year is None:
        return None
    try:
        year = int(year)
    except (TypeError, ValueError):
        return None
    ceiling = (as_of or datetime.date.today().year) + FUTURE_YEAR_ALLOWANCE
    return year if MIN_PLAUSIBLE_YEAR <= year <= ceiling else None


_NON_ALNUM = re.compile(r"[^a-z0-9]")
_DOI_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/")

# Journals deposit each supplementary table and figure as its own record
# ("Table S4 from Transcriptomic Plasticity Is a Hallmark of ..."). They are
# parts of a paper, not papers: one genomics profile had 28 of them among 42
# records in a year. Their titles all differ, so dedup cannot merge them.
_SUPPLEMENT_PART = (
    r"^(?:supplementary\s+)?(?:table|figure|fig\.?|data|movie|video|file|appendix|note)"
)
# "Table S4 from ..." is unambiguous on its own.
_NUMBERED_SUPPLEMENT = re.compile(_SUPPLEMENT_PART + r"\s*s?\d+[a-z]?\s+from\s", re.I)
# "Data from ..." could open a real paper's title, so it counts only on a record
# OpenAlex itself does not type as a publication.
_UNNUMBERED_SUPPLEMENT = re.compile(_SUPPLEMENT_PART + r"\s+from\s", re.I)
NON_PUBLICATION_TYPES = frozenset({"other", "dataset"})


def is_supplement(title: str | None, work_type: str | None = None) -> bool:
    if not title:
        return False
    title = title.strip()
    if _NUMBERED_SUPPLEMENT.match(title):
        return True
    return work_type in NON_PUBLICATION_TYPES and bool(_UNNUMBERED_SUPPLEMENT.match(title))


# Venues register a paper's demo, poster or evaluation artifact as a record of
# its own: "Demo: RollingPol: ...", "Big Bird: ... (SOSP 2026 Evaluation
# Artifact)". Each is the same paper, so the label is ignored when matching.
_COMPANION_PREFIX = re.compile(
    r"^\s*(?:demo|poster|artifact|artefact|extended\s+abstract)\s*[:\-–]\s*", re.I
)
_COMPANION_SUFFIX = re.compile(
    r"\s*[(\[][^()\[\]]*\b(?:artifact|artefact|evaluation|demo|poster)\b[^()\[\]]*[)\]]\s*$",
    re.I,
)


def strip_companion_label(title: str) -> str:
    return _COMPANION_SUFFIX.sub("", _COMPANION_PREFIX.sub("", title))


def is_companion(title: str | None) -> bool:
    return bool(title) and strip_companion_label(title) != title


def title_norm(title: str | None) -> str:
    """Fold a title down to bare lowercase alphanumerics.

    Stripping punctuation and spacing absorbs the formatting drift between
    arXiv, proceedings and journal metadata for the same paper; dropping
    companion labels merges a paper with its demo or artifact record.
    """
    if not title:
        return ""
    decomposed = unicodedata.normalize("NFKD", strip_companion_label(title))
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub("", without_accents.lower())


def doi_norm(doi: str | None) -> str | None:
    """Normalize a DOI to its bare ``10.xxxx/...`` form, or None."""
    if not doi:
        return None
    stripped = _DOI_PREFIX.sub("", doi.strip().lower())
    return stripped or None


@dataclass(frozen=True)
class WorkRecord:
    """One OpenAlex work row, reduced to the fields dedup needs."""

    openalex_id: str
    title: str | None
    publication_year: int | None
    doi: str | None = None
    work_type: str | None = None
    is_preprint: bool = False
    publication_date: str | None = None  # ISO date, for ordering within a year
    url: str | None = None  # where a reader can open it; see paper_url

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "WorkRecord":
        location = raw.get("primary_location") or {}
        source = location.get("source") or {}
        work_type = raw.get("type")
        source_type = source.get("type")
        return cls(
            openalex_id=raw["id"].rsplit("/", 1)[-1],
            title=raw.get("title"),
            publication_year=plausible_year(raw.get("publication_year")),
            doi=raw.get("doi"),
            work_type=work_type,
            is_preprint=work_type == "preprint" or source_type == "repository",
            publication_date=raw.get("publication_date"),
            url=paper_url(raw),
        )


def is_arxiv(url: str | None) -> bool:
    return bool(url) and "arxiv.org/" in url


def paper_url(raw: dict[str, Any]) -> str | None:
    """The best page to send a reader to: arXiv, then the DOI, then anything.

    arXiv first because it is free to read and stable; a DOI often lands on a
    paywall. Either location may carry the arXiv link, depending on which
    version OpenAlex treats as primary.
    """
    landings = [
        (raw.get(key) or {}).get("landing_page_url")
        for key in ("primary_location", "best_oa_location")
    ]
    arxiv = next((u for u in landings if is_arxiv(u)), None)
    doi = raw.get("doi")
    return arxiv or doi or next((u for u in landings if u), None)


@dataclass
class WorkCluster:
    """One real paper, with every record that collapsed into it."""

    cluster_key: str
    canonical_year: int | None
    records: list[WorkRecord] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def representative(self) -> WorkRecord:
        """The published version if there is one, else the newest record.

        Sorting preprints last means a journal article represents the cluster
        even when a preprint shares its canonical year. A demo or artifact
        record never represents a paper that is itself present.
        """
        return max(
            self.records,
            key=lambda r: (not is_companion(r.title), r.publication_year or 0, not r.is_preprint),
        )

    @property
    def url(self) -> str | None:
        """An arXiv link from any version, else the representative's link.

        The representative is the published version, but its link is often a
        paywalled DOI; the preprint in the same cluster is the same paper and
        free to read.
        """
        arxiv = next((r.url for r in self.records if is_arxiv(r.url)), None)
        return arxiv or self.representative.url or next(
            (r.url for r in self.records if r.url), None
        )


def _collapse_identical_dois(records: Sequence[WorkRecord]) -> list[WorkRecord]:
    """Drop byte-identical re-listings of the same DOI.

    This is the only role DOI plays in clustering. It deliberately cannot split
    a title group: legitimate versions carry *different* DOIs, so splitting on
    DOI would undo the merge this module exists to perform.
    """
    seen: set[str] = set()
    out: list[WorkRecord] = []
    for record in records:
        key = doi_norm(record.doi)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(record)
    return out


def _split_by_year_gap(
    records: list[WorkRecord], year_tolerance: int
) -> list[list[WorkRecord]]:
    """Partition same-title records wherever consecutive years gap too far apart.

    Records with no year ride along with the earliest cluster rather than
    forming a spurious paper of their own.
    """
    dated = sorted(
        (r for r in records if r.publication_year is not None),
        key=lambda r: r.publication_year,  # type: ignore[arg-type,return-value]
    )
    undated = [r for r in records if r.publication_year is None]

    if not dated:
        return [undated] if undated else []

    clusters: list[list[WorkRecord]] = [[dated[0]]]
    for record in dated[1:]:
        previous = clusters[-1][-1].publication_year
        assert previous is not None and record.publication_year is not None
        if record.publication_year - previous <= year_tolerance:
            clusters[-1].append(record)
        else:
            clusters.append([record])

    clusters[0].extend(undated)
    return clusters


def cluster_works(
    records: Iterable[WorkRecord],
    *,
    year_tolerance: int = YEAR_TOLERANCE,
    min_title_len: int = MIN_TITLE_LEN,
) -> list[WorkCluster]:
    """Collapse version-duplicated records into one cluster per real paper.

    The caller passes the union of every confirmed OpenAlex profile for one
    person, so cross-profile duplicates merge on the same pass as within-profile
    ones. Cluster membership depends on which records are present, which is why
    no global dedup key is stored on the work row itself.
    """
    deduped = _collapse_identical_dois(list(records))

    by_title: dict[str, list[WorkRecord]] = {}
    for record in deduped:
        by_title.setdefault(title_norm(record.title), []).append(record)

    clusters: list[WorkCluster] = []
    for normalized, group in by_title.items():
        # Untitled records can never be matched to anything; keep each separate.
        if not normalized:
            clusters.extend(
                WorkCluster(f"id:{r.openalex_id}", r.publication_year, [r])
                for r in group
            )
            continue

        # Too generic to merge on title alone -- fall back to DOI, else title+year.
        if len(normalized) < min_title_len:
            by_fallback: dict[str, list[WorkRecord]] = {}
            for record in group:
                key = doi_norm(record.doi) or f"{normalized}|{record.publication_year}"
                by_fallback.setdefault(key, []).append(record)
            clusters.extend(
                WorkCluster(f"short:{key}", _canonical_year(members), members)
                for key, members in by_fallback.items()
            )
            continue

        for ordinal, members in enumerate(_split_by_year_gap(group, year_tolerance)):
            clusters.append(
                WorkCluster(f"{normalized}#{ordinal}", _canonical_year(members), members)
            )

    return clusters


def _canonical_year(records: Sequence[WorkRecord]) -> int | None:
    """The published year wins over the preprint year, so take the maximum.

    Companion records (a demo, an artifact) are dated by their own venue, which
    can trail the paper; they set the year only when the paper itself is absent.
    """
    papers = [r for r in records if not is_companion(r.title)] or records
    years = [r.publication_year for r in papers if r.publication_year is not None]
    return max(years) if years else None


def count_since(clusters: Iterable[WorkCluster], since_year: int) -> int:
    """Distinct papers published in ``since_year`` or later."""
    return sum(
        1
        for c in clusters
        if c.canonical_year is not None and c.canonical_year >= since_year
    )


def count_raw_since(records: Iterable[WorkRecord], since_year: int) -> int:
    """Undeduplicated record count for the same span, kept for transparency.

    Reported alongside the deduplicated figure so the correction stays visible
    rather than being silently applied.
    """
    return sum(
        1
        for r in records
        if r.publication_year is not None and r.publication_year >= since_year
    )


def last_publication_year(clusters: Iterable[WorkCluster]) -> int | None:
    """Most recent canonical year, or None when nothing is dated.

    Merging with ``max`` makes this immune to duplicate inflation, which is why
    it stays trustworthy even where the windowed counts need dedup.
    """
    years = [c.canonical_year for c in clusters if c.canonical_year is not None]
    return max(years) if years else None
