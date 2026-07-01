"""Tests for OddsClient parsing and credit tracking."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.odds_client import CreditTracker, OddsClient
from tests.fixtures.api_snapshots import NBA_TWO_WAY_ARB, SOCCER_THREE_WAY


class TestCreditTracker:
    def test_starts_with_none_remaining(self):
        tracker = CreditTracker()
        assert tracker.remaining is None
        assert tracker.above_floor is True

    def test_updates_from_headers(self):
        tracker = CreditTracker()
        headers = httpx.Headers({"x-requests-remaining": "450", "x-requests-used": "50"})
        tracker.update(headers)
        assert tracker.remaining == 450
        assert tracker.used == 50
        assert tracker.last_checked is not None

    def test_above_floor_when_remaining_high(self):
        tracker = CreditTracker()
        tracker.remaining = 100
        with patch("src.odds_client.config") as cfg:
            cfg.CREDIT_FLOOR = 50
            assert tracker.above_floor is True

    def test_below_floor_when_remaining_low(self):
        tracker = CreditTracker()
        tracker.remaining = 30
        with patch("src.odds_client.config") as cfg:
            cfg.CREDIT_FLOOR = 50
            assert tracker.above_floor is False

    def test_history_appended(self):
        tracker = CreditTracker()
        headers = httpx.Headers({"x-requests-remaining": "100"})
        tracker.update(headers)
        tracker.update(headers)
        assert len(tracker.history) == 2


class TestParseEvents:
    def test_parses_nba_event_fields(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        evt = events[0]
        assert evt.id == "abc123nba"
        assert evt.sport_key == "basketball_nba"
        assert evt.home_team == "Los Angeles Lakers"
        assert evt.commence_time.tzinfo is not None

    def test_parses_bookmakers_and_markets(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        evt = events[0]
        assert len(evt.bookmakers) == 2
        keys = {bm.key for bm in evt.bookmakers}
        assert keys == {"draftkings", "fanduel"}

        dk = next(bm for bm in evt.bookmakers if bm.key == "draftkings")
        market_keys = {m.key for m in dk.markets}
        assert "h2h" in market_keys
        assert "spreads" in market_keys

    def test_parses_spread_points(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        dk = next(bm for bm in events[0].bookmakers if bm.key == "draftkings")
        spreads = next(m for m in dk.markets if m.key == "spreads")
        points = {o.point for o in spreads.outcomes}
        assert points == {-3.5, 3.5}

    def test_parses_totals(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        fd = next(bm for bm in events[0].bookmakers if bm.key == "fanduel")
        totals = next(m for m in fd.markets if m.key == "totals")
        names = {o.name for o in totals.outcomes}
        assert names == {"Over", "Under"}
        assert totals.outcomes[0].point == 220.5

    def test_parses_three_way_soccer(self):
        events = OddsClient.parse_events(SOCCER_THREE_WAY)
        evt = events[0]
        book_a = next(bm for bm in evt.bookmakers if bm.key == "book_a")
        h2h = book_a.markets[0]
        assert len(h2h.outcomes) == 3
        assert {o.name for o in h2h.outcomes} == {"Liverpool", "Draw", "Arsenal"}

    def test_last_update_parsed_as_datetime(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        mkt = events[0].bookmakers[0].markets[0]
        assert isinstance(mkt.last_update, datetime)

    def test_empty_list(self):
        assert OddsClient.parse_events([]) == []

    def test_prices_are_floats(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        price = events[0].bookmakers[0].markets[0].outcomes[0].price
        assert isinstance(price, float)


class TestOddsClientHTTP:
    def test_get_odds_skips_when_below_credit_floor(self):
        client = OddsClient(api_key="test-key", base_url="https://api.example.com/v4")
        client.credits.remaining = 10
        with patch("src.odds_client.config") as cfg:
            cfg.CREDIT_FLOOR = 50
            cfg.REGIONS = "us"
            cfg.MARKETS = "h2h"
            cfg.ODDS_FORMAT = "decimal"
            data, remaining = client.get_odds("basketball_nba")
        assert data == []
        assert remaining == 10
        client.close()

    def test_context_manager_closes_client(self):
        with OddsClient(api_key="test", base_url="https://api.example.com/v4") as client:
            assert client._client is not None
        # After exit, further close should not raise
        client.close()

    def test_credits_updated_on_get(self):
        client = OddsClient(api_key="test-key", base_url="https://api.example.com/v4")
        mock_resp = MagicMock()
        mock_resp.headers = httpx.Headers({"x-requests-remaining": "200"})
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._client, "get", return_value=mock_resp):
            with patch("src.odds_client.time.sleep"):
                with patch("src.odds_client.config") as cfg:
                    cfg.CREDIT_FLOOR = 0
                    cfg.REGIONS = "us"
                    cfg.MARKETS = "h2h"
                    cfg.ODDS_FORMAT = "decimal"
                    client.get_odds("basketball_nba")
        assert client.credits.remaining == 200
        client.close()
