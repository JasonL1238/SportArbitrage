"""``--tier core`` as a request budget, and what it costs in coverage.

The measured shape of a full pass: FanDuel 133 requests, Kambi 69, Pinnacle 16.
FanDuel's 133 is almost entirely one per-event soccer page — 126 of them — and
that is the only per-event hop in the pipeline today.  Ten sources on a five
minute interval cannot afford it; the same ten on the slate endpoints alone can.

The split is a **request budget, not a market filter**, and the difference
matters twice over.  A source with no per-event call must return exactly the same
rows in both tiers, or the two are not comparable.  A source that *does* defer
one collects genuinely less — so it must say so through
:meth:`~src.sources.base.OddsSource.capabilities`, or validation reports the
missing market as ``core_market_absent``: "a source label has probably changed",
which sends someone looking for a parsing fault that was never there.
"""
from __future__ import annotations

import httpx
import pytest

from src.schema import Market
from src.sources._common import Tier
from src.sources.fanduel import FanDuelAdapter
from src.sources.pinnacle import PinnacleAdapter
from src.sources.registry import SOURCES


def _fanduel_page(with_soccer_moneyline: bool = True) -> dict:
    """A slate page thin enough to read, in FanDuel's own shape."""
    return {
        "attachments": {
            "competitions": {"1": {"name": "English Premier League"}},
            "events": {
                "100": {
                    "name": "Arsenal v Chelsea",
                    "openDate": "2026-08-01T14:00:00Z",
                    "competitionId": "1",
                }
            },
            "markets": (
                {
                    "m1": {
                        "eventId": "100",
                        "marketType": "WIN-DRAW-WIN",
                        "marketStatus": "OPEN",
                        "runners": [],
                    }
                }
                if with_soccer_moneyline
                else {"m1": {"eventId": "100", "marketType": "OUTRIGHT_BETTING", "runners": []}}
            ),
        }
    }


class TestFanDuelDefersItsPerEventHop:
    def _adapter(self, recorded: list[str]) -> FanDuelAdapter:
        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request.url.path)
            return httpx.Response(200, json=_fanduel_page())

        return FanDuelAdapter(
            leagues=["EPL"],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_core_issues_no_per_event_requests(self) -> None:
        seen: list[str] = []
        adapter = self._adapter(seen)
        try:
            adapter.fetch_raw(tier=Tier.CORE)
        finally:
            adapter.close()
        assert seen == ["/api/content-managed-page"]
        assert not any("event-page" in path for path in seen)

    def test_full_adds_them(self) -> None:
        seen: list[str] = []
        adapter = self._adapter(seen)
        try:
            adapter.fetch_raw(tier=Tier.FULL)
        finally:
            adapter.close()
        assert any(path.endswith("/event-page") for path in seen)

    def test_the_smaller_claim_is_published_rather_than_left_to_be_inferred(self) -> None:
        """Otherwise the absent handicaps look like a market that disappeared."""
        adapter = self._adapter([])
        try:
            core = adapter.capabilities(tier=Tier.CORE)
            full = adapter.capabilities(tier=Tier.FULL)
        finally:
            adapter.close()
        assert core["EPL"] == frozenset({Market.MONEYLINE})
        assert full["EPL"] == frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL})


class TestSourcesWithNoDepthTierSayNothingChanges:
    """A source that quietly returned less under ``core`` would make the two
    tiers incomparable, which is worse than not having tiers at all."""

    def test_pinnacle_asks_for_the_same_scopes_either_way(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/markets/straight"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"id": 1, "type": "matchup"}])

        requested: dict[Tier, list[str]] = {}
        for tier in Tier:
            seen: list[str] = []

            def recording(request: httpx.Request, seen=seen) -> httpx.Response:
                seen.append(request.url.path)
                return handler(request)

            adapter = PinnacleAdapter(
                ["MLB"],
                client=httpx.Client(transport=httpx.MockTransport(recording)),
                request_pause=0.0,
            )
            try:
                adapter.fetch_raw(tier=tier)
            finally:
                adapter.close()
            requested[tier] = seen
        assert requested[Tier.CORE] == requested[Tier.FULL]

    def test_their_capabilities_do_not_change_with_the_tier(self) -> None:
        for descriptor in SOURCES:
            if descriptor.key.startswith("fanduel"):
                continue  # the one source with a real depth tier
            adapter = descriptor.replay_instance()
            try:
                assert adapter.capabilities(tier=Tier.CORE) == adapter.capabilities(
                    tier=Tier.FULL
                ), descriptor.key
            finally:
                adapter.close()


class TestEverySourceAcceptsTheTier:
    """The protocol's shape, checked against every registered adapter rather than
    asserted in a docstring."""

    @pytest.mark.parametrize("descriptor", SOURCES, ids=lambda d: d.key)
    def test_the_adapter_takes_a_tier_and_declares_capabilities(self, descriptor) -> None:
        adapter = descriptor.replay_instance()
        try:
            claims = adapter.capabilities(tier=Tier.CORE)
            assert isinstance(claims, dict) and claims
            # Every claimed market is one the pipeline models.
            for markets in claims.values():
                assert all(isinstance(market, Market) for market in markets)
        finally:
            adapter.close()

    def test_the_core_claim_is_never_wider_than_the_full_one(self) -> None:
        """A budget can only ever collect less, never more."""
        for descriptor in SOURCES:
            adapter = descriptor.replay_instance()
            try:
                core, full = (
                    adapter.capabilities(tier=Tier.CORE),
                    adapter.capabilities(tier=Tier.FULL),
                )
            finally:
                adapter.close()
            for league, markets in core.items():
                assert markets <= full[league], f"{descriptor.key}/{league}"
