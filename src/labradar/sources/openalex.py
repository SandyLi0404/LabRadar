"""OpenAlex client with an explicit request budget.

Since February 2026 OpenAlex meters usage in dollars rather than request counts,
and the ``mailto`` polite-pool convention no longer buys any headroom -- a
keyless caller and a ``mailto`` caller get byte-identical budget headers. A free
API key raises the daily allowance from $0.10 to $1.00, so the key is required
rather than optional here.

Measured per-request costs, which drive the call strategy below:

    GET /authors/{id}      $0.0000   single record by ID, uncapped
    GET /authors?filter=   $0.0001   list/filter
    GET /authors?search=   $0.0010   full-text search

Fetching profiles one ID at a time is therefore *free*, while the batched
``filter=openalex_id:A|B|C`` form that looks more efficient is not. Search is
ten times dearer again, so it is confined to the curation tool where a human is
choosing between candidates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from labradar.dedup import WorkRecord, plausible_year

log = logging.getLogger(__name__)

API_ROOT = "https://api.openalex.org"
COLUMBIA_ROR = "https://ror.org/00hj8s172"

# Fields requested for works. Asking for only what dedup and display need keeps
# responses small; primary_location is what identifies a record as a preprint,
# and best_oa_location is where a reader can open the paper -- for CS, usually
# arXiv. publication_date orders papers within a year.
WORK_FIELDS = (
    "id,doi,title,publication_year,publication_date,type,"
    "primary_location,best_oa_location"
)

MAX_PER_PAGE = 200


class OpenAlexError(RuntimeError):
    """Raised for configuration and budget problems, not transport failures."""


class BudgetExhausted(OpenAlexError):
    """Raised before a request that would run the daily allowance down."""


@dataclass
class Budget:
    """Tracks the allowance reported by the API's rate-limit headers.

    The reserve exists so a long run stops while there is still budget left for
    an operator to investigate, rather than dying mid-way with nothing spare.
    """

    remaining_usd: float | None = None
    limit_usd: float | None = None
    reserve_usd: float = 0.01
    spent_usd: float = 0.0
    requests: int = 0

    def observe(self, headers: httpx.Headers) -> None:
        self.requests += 1
        cost = _float_header(headers, "x-ratelimit-cost-usd")
        if cost is not None:
            self.spent_usd += cost
        remaining = _float_header(headers, "x-ratelimit-remaining-usd")
        if remaining is not None:
            self.remaining_usd = remaining
        limit = _float_header(headers, "x-ratelimit-limit-usd")
        if limit is not None:
            self.limit_usd = limit

    def check(self) -> None:
        if self.remaining_usd is None:
            return
        if self.remaining_usd <= self.reserve_usd:
            raise BudgetExhausted(
                f"OpenAlex budget down to ${self.remaining_usd:.4f} "
                f"(reserve ${self.reserve_usd:.4f}); stopping before it runs out"
            )


def _float_header(headers: httpx.Headers, name: str) -> float | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass
class AuthorProfile:
    """An OpenAlex author record, reduced to what the activity layer stores."""

    openalex_id: str
    display_name: str | None
    orcid: str | None
    works_count: int
    cited_by_count: int
    last_known_institution: str | None
    counts_by_year: dict[int, int] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "AuthorProfile":
        institutions = raw.get("last_known_institutions") or []
        return cls(
            openalex_id=raw["id"].rsplit("/", 1)[-1],
            display_name=raw.get("display_name"),
            orcid=raw.get("orcid"),
            works_count=raw.get("works_count") or 0,
            cited_by_count=raw.get("cited_by_count") or 0,
            last_known_institution=(
                institutions[0].get("display_name") if institutions else None
            ),
            counts_by_year={
                year: c["works_count"]
                for c in raw.get("counts_by_year") or []
                if (year := plausible_year(c.get("year"))) is not None
            },
            topics=[t["display_name"] for t in (raw.get("topics") or [])[:5]],
        )


class OpenAlexClient:
    """Thin, budget-aware wrapper over the endpoints this project uses."""

    def __init__(
        self,
        api_key: str | None,
        *,
        client: httpx.Client | None = None,
        reserve_usd: float = 0.01,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise OpenAlexError(
                "OPENALEX_API_KEY is required. Register a free key at "
                "https://openalex.org/settings/api -- without one the daily "
                "allowance is $0.10 instead of $1.00, and `mailto` no longer "
                "raises it."
            )
        self.api_key = api_key
        self.budget = Budget(reserve_usd=reserve_usd)
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": "LabRadar/0.1 (+research aggregator)"}
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenAlexClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.budget.check()
        query = dict(params or {})
        query["api_key"] = self.api_key
        response = self._client.get(f"{API_ROOT}{path}", params=query)
        self.budget.observe(response.headers)

        if response.status_code == 401:
            raise OpenAlexError(
                "OpenAlex rejected the API key (401). Check OPENALEX_API_KEY."
            )
        if response.status_code == 429:
            raise BudgetExhausted(
                "OpenAlex returned 429; the daily allowance is spent. "
                f"Retry after {response.headers.get('retry-after', 'unknown')}s."
            )
        response.raise_for_status()
        return response.json()

    def author(self, openalex_id: str) -> AuthorProfile:
        """Fetch one author by ID. This call is free -- prefer it to filtering."""
        return AuthorProfile.from_api(self._get(f"/authors/{openalex_id}"))

    def works(self, openalex_id: str, *, max_pages: int = 20) -> list[WorkRecord]:
        """All works for one author, cursor-paged.

        ``max_pages`` is a guard against a runaway profile rather than a real
        limit; at 200 per page it allows 4000 works.
        """
        records: list[WorkRecord] = []
        cursor = "*"
        for _ in range(max_pages):
            payload = self._get(
                "/works",
                {
                    "filter": f"author.id:{openalex_id}",
                    "select": WORK_FIELDS,
                    "per-page": MAX_PER_PAGE,
                    "cursor": cursor,
                },
            )
            results = payload.get("results") or []
            records.extend(WorkRecord.from_api(w) for w in results)
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor or not results:
                break
        else:
            log.warning(
                "author %s hit the %d-page guard; results may be truncated",
                openalex_id,
                max_pages,
            )
        return records

    def search_candidates(
        self, name: str, *, institution_ror: str = COLUMBIA_ROR, limit: int = 25
    ) -> list[AuthorProfile]:
        """Find possible profiles for a name, scoped to one institution.

        Costs $0.001 per call, so this is for the curation tool only. Scoping to
        the institution is what makes the candidate list reviewable -- an
        unscoped search for a common name returns mostly unrelated people.
        """
        payload = self._get(
            "/authors",
            {
                "filter": (
                    f"last_known_institutions.ror:{institution_ror},"
                    f"display_name.search:{name}"
                ),
                "per-page": min(limit, MAX_PER_PAGE),
            },
        )
        return [AuthorProfile.from_api(a) for a in payload.get("results") or []]

    def search_by_name(self, name: str, *, limit: int = 25) -> list[AuthorProfile]:
        """Find profiles by name with no institution filter.

        A fallback for people OpenAlex has affiliated elsewhere -- a recent
        hire whose last indexed paper carries their previous employer, or
        someone with a long career before Columbia. Without the institution
        constraint the result set is far noisier, so callers must treat these
        as proposals for review rather than matches.
        """
        payload = self._get(
            "/authors",
            {"filter": f"display_name.search:{name}", "per-page": min(limit, MAX_PER_PAGE)},
        )
        return [AuthorProfile.from_api(a) for a in payload.get("results") or []]
