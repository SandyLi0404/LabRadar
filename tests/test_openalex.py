"""Client tests, run against a stub transport so no calls leave the machine.

The budget behaviour is the part worth pinning: OpenAlex meters in dollars, and
a job that blows the daily allowance silently would leave the activity data
half-refreshed with no obvious cause.
"""

from __future__ import annotations

import httpx
import pytest

from labradar.sources.openalex import (
    AuthorProfile,
    BudgetExhausted,
    OpenAlexClient,
    OpenAlexError,
)

HEADERS_FREE = {"x-ratelimit-cost-usd": "0", "x-ratelimit-remaining-usd": "0.9"}
HEADERS_FILTER = {"x-ratelimit-cost-usd": "0.0001", "x-ratelimit-remaining-usd": "0.8"}


def make_client(handler, **kwargs) -> OpenAlexClient:
    transport = httpx.MockTransport(handler)
    return OpenAlexClient(
        "test-key", client=httpx.Client(transport=transport), **kwargs
    )


def author_payload(openalex_id="A1", **overrides):
    payload = {
        "id": f"https://openalex.org/{openalex_id}",
        "display_name": "Josh Alman",
        "orcid": "https://orcid.org/0009-0002-2204-1359",
        "works_count": 63,
        "cited_by_count": 900,
        "last_known_institutions": [{"display_name": "Columbia University"}],
        "counts_by_year": [
            {"year": 2026, "works_count": 2},
            {"year": 2025, "works_count": 12},
        ],
        "topics": [{"display_name": "Complexity and Algorithms in Graphs"}],
    }
    payload.update(overrides)
    return payload


class TestKeyRequirement:
    @pytest.mark.parametrize("missing", [None, ""])
    def test_refuses_to_start_without_a_key(self, missing):
        with pytest.raises(OpenAlexError, match="OPENALEX_API_KEY"):
            OpenAlexClient(missing)

    def test_error_points_at_the_registration_page(self):
        with pytest.raises(OpenAlexError, match="openalex.org/settings/api"):
            OpenAlexClient(None)

    def test_key_is_sent_on_every_request(self):
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("api_key"))
            return httpx.Response(200, json=author_payload(), headers=HEADERS_FREE)

        client = make_client(handler)
        client.author("A1")
        client.author("A2")
        assert seen == ["test-key", "test-key"]

    def test_rejected_key_is_reported_clearly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={}, headers=HEADERS_FREE)

        with pytest.raises(OpenAlexError, match="rejected the API key"):
            make_client(handler).author("A1")


class TestBudget:
    def test_tracks_spend_and_remaining_from_headers(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=author_payload(), headers=HEADERS_FILTER)

        client = make_client(handler)
        client.author("A1")
        client.author("A2")
        assert client.budget.requests == 2
        assert client.budget.spent_usd == pytest.approx(0.0002)
        assert client.budget.remaining_usd == pytest.approx(0.8)

    def test_author_lookups_are_recorded_as_free(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=author_payload(), headers=HEADERS_FREE)

        client = make_client(handler)
        client.author("A1")
        assert client.budget.spent_usd == 0.0

    def test_stops_before_the_allowance_runs_out(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=author_payload(),
                headers={
                    "x-ratelimit-cost-usd": "0.0001",
                    "x-ratelimit-remaining-usd": "0.005",
                },
            )

        client = make_client(handler, reserve_usd=0.01)
        client.author("A1")  # header observed here
        with pytest.raises(BudgetExhausted, match="stopping before it runs out"):
            client.author("A2")

    def test_reserve_leaves_headroom_for_diagnosis(self):
        """The guard must fire while budget remains, not at absolute zero."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=author_payload(),
                headers={"x-ratelimit-remaining-usd": "0.02"},
            )

        client = make_client(handler, reserve_usd=0.05)
        client.author("A1")
        with pytest.raises(BudgetExhausted):
            client.author("A2")
        assert client.budget.remaining_usd == pytest.approx(0.02)

    def test_429_is_reported_as_budget_exhaustion(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={}, headers={"retry-after": "3600"})

        with pytest.raises(BudgetExhausted, match="3600"):
            make_client(handler).author("A1")

    def test_missing_headers_do_not_crash_accounting(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=author_payload(), headers={})

        client = make_client(handler)
        client.author("A1")
        assert client.budget.requests == 1
        assert client.budget.remaining_usd is None


class TestAuthorProfile:
    def test_parses_the_fields_the_activity_layer_needs(self):
        profile = AuthorProfile.from_api(author_payload())
        assert profile.openalex_id == "A1"
        assert profile.display_name == "Josh Alman"
        assert profile.works_count == 63
        assert profile.last_known_institution == "Columbia University"
        assert profile.counts_by_year == {2026: 2, 2025: 12}
        assert profile.topics == ["Complexity and Algorithms in Graphs"]

    def test_tolerates_a_profile_with_no_institution(self):
        payload = author_payload(last_known_institutions=None)
        assert AuthorProfile.from_api(payload).last_known_institution is None

    def test_tolerates_a_profile_with_no_year_counts(self):
        payload = author_payload(counts_by_year=None)
        assert AuthorProfile.from_api(payload).counts_by_year == {}


class TestWorksPaging:
    def test_follows_the_cursor_to_the_end(self):
        pages = [
            {
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "title": "First Paper With A Long Enough Title",
                        "publication_year": 2025,
                    }
                ],
                "meta": {"next_cursor": "page2"},
            },
            {
                "results": [
                    {
                        "id": "https://openalex.org/W2",
                        "title": "Second Paper With A Long Enough Title",
                        "publication_year": 2026,
                    }
                ],
                "meta": {"next_cursor": None},
            },
        ]
        calls: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.params.get("cursor"))
            return httpx.Response(
                200, json=pages[len(calls) - 1], headers=HEADERS_FILTER
            )

        works = make_client(handler).works("A1")
        assert [w.openalex_id for w in works] == ["W1", "W2"]
        assert calls == ["*", "page2"]

    def test_stops_on_an_empty_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": [], "meta": {"next_cursor": "endless"}},
                headers=HEADERS_FILTER,
            )

        assert make_client(handler).works("A1") == []

    def test_marks_repository_hosted_records_as_preprints(self):
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "A Refined Laser Method and Faster Matrix Multiplication",
                    "publication_year": 2020,
                    "type": "preprint",
                    "primary_location": {"source": {"type": "repository"}},
                }
            ],
            "meta": {"next_cursor": None},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload, headers=HEADERS_FILTER)

        assert make_client(handler).works("A1")[0].is_preprint is True

    def test_page_guard_truncates_rather_than_looping_forever(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "title": "Looping Paper With A Long Enough Title",
                            "publication_year": 2025,
                        }
                    ],
                    "meta": {"next_cursor": "always-more"},
                },
                headers=HEADERS_FILTER,
            )

        works = make_client(handler).works("A1", max_pages=3)
        assert len(works) == 3


class TestCandidateSearch:
    def test_scopes_the_search_to_the_institution(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["filter"] = request.url.params.get("filter", "")
            return httpx.Response(
                200, json={"results": [author_payload()]}, headers=HEADERS_FILTER
            )

        candidates = make_client(handler).search_candidates("Josh Alman")
        assert "ror.org/00hj8s172" in seen["filter"]
        assert "display_name.search:Josh Alman" in seen["filter"]
        assert candidates[0].display_name == "Josh Alman"
