"""End-to-end pipeline backtests: API JSON → parse → filter → arbs → dedup."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.arb import filter_stale_odds, find_arbs
from src.dedup import make_dedup_key, should_alert
from src.odds_client import OddsClient
from src.stake import guaranteed_profit, stake_split
from tests.fixtures.api_snapshots import (
    EMPTY_BOOKMAKERS,
    MULTI_EVENT_SNAPSHOT,
    NBA_TWO_WAY_ARB,
    SOCCER_THREE_WAY,
    STALE_ODDS_SNAPSHOT,
)


class TestParseToArbPipeline:
    def test_nba_snapshot_finds_h2h_arb(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        assert len(events) == 1
        assert events[0].home_team == "Los Angeles Lakers"

        results = find_arbs(events, min_margin=0.01, total_stake=100.0)
        h2h = [r for r in results if r.market_key == "h2h"]
        assert len(h2h) == 1
        assert h2h[0].margin > 0.01
        assert h2h[0].guaranteed_profit > 0

    def test_multi_event_scan_finds_one_arb(self):
        events = OddsClient.parse_events(MULTI_EVENT_SNAPSHOT)
        assert len(events) == 2
        results = find_arbs(events, min_margin=0.01)
        assert len(results) == 1
        assert results[0].event_id == "evt_nba_1"

    def test_soccer_three_way_pipeline(self):
        events = OddsClient.parse_events(SOCCER_THREE_WAY)
        results = find_arbs(events, min_margin=0.01)
        assert len(results) == 1
        assert results[0].outcome_count == 3
        assert results[0].sport_key == "soccer_epl"

    def test_empty_bookmakers_produces_no_arbs(self):
        events = OddsClient.parse_events(EMPTY_BOOKMAKERS)
        assert events[0].bookmakers == []
        assert find_arbs(events) == []


class TestStaleOddsPipeline:
    def test_stale_book_removed_before_scan(self):
        events = OddsClient.parse_events(STALE_ODDS_SNAPSHOT)
        raw_results = find_arbs(events, min_margin=0.0)
        assert len(raw_results) >= 1  # stale book creates arb

        filtered = filter_stale_odds(events, max_age_seconds=300)
        clean_results = find_arbs(filtered, min_margin=0.01)
        assert len(clean_results) == 0

    def test_only_fresh_book_remains(self):
        events = OddsClient.parse_events(STALE_ODDS_SNAPSHOT)
        filtered = filter_stale_odds(events, max_age_seconds=300)
        keys = [bm.key for bm in filtered[0].bookmakers]
        assert keys == ["fresh_book"]


class TestDedupPipeline:
    def _run_pipeline(self, snapshot: list[dict], min_margin: float = 0.01):
        events = OddsClient.parse_events(snapshot)
        events = filter_stale_odds(events, max_age_seconds=3600)
        return find_arbs(events, min_margin=min_margin, total_stake=100.0)

    def test_same_snapshot_same_dedup_key(self):
        opps = self._run_pipeline(NBA_TWO_WAY_ARB)
        assert len(opps) >= 1
        keys = [make_dedup_key(o) for o in opps]
        # Re-run should produce identical keys
        opps2 = self._run_pipeline(NBA_TWO_WAY_ARB)
        keys2 = [make_dedup_key(o) for o in opps2]
        assert keys == keys2

    def test_alert_cooldown_simulation(self):
        opps = self._run_pipeline(NBA_TWO_WAY_ARB)
        opp = next(r for r in opps if r.market_key == "h2h")

        send1, _ = should_alert(opp, None)
        assert send1 is True

        last = {"sent_at": datetime.now(UTC), "margin": opp.margin}
        send2, reason = should_alert(opp, last)
        assert send2 is False
        assert reason == "cooldown"

    def test_improved_margin_re_alerts(self):
        opps = self._run_pipeline(NBA_TWO_WAY_ARB)
        opp = next(r for r in opps if r.market_key == "h2h")
        last = {"sent_at": datetime.now(UTC), "margin": opp.margin - 0.01}
        send, reason = should_alert(opp, last)
        assert send is True
        assert reason is None


class TestFullMathPipeline:
    """Verify stake math end-to-end from raw JSON."""

    @pytest.mark.parametrize("snapshot,min_arbs", [
        (NBA_TWO_WAY_ARB, 1),
        (SOCCER_THREE_WAY, 1),
        (MULTI_EVENT_SNAPSHOT, 1),
    ])
    def test_profit_positive_for_all_detected_arbs(self, snapshot, min_arbs):
        events = OddsClient.parse_events(snapshot)
        results = find_arbs(events, min_margin=0.01, total_stake=100.0)
        assert len(results) >= min_arbs
        for opp in results:
            odds = [b.decimal_odds for b in opp.best_outcomes]
            assert opp.guaranteed_profit > 0
            assert sum(opp.stakes) == pytest.approx(100.0, rel=1e-9)
            assert opp.guaranteed_profit == pytest.approx(
                guaranteed_profit(opp.stakes, odds, 100.0), rel=1e-9
            )

    def test_custom_stake_propagates(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        for stake in [25.0, 100.0, 1000.0]:
            opp = find_arbs(events, min_margin=0.01, total_stake=stake)[0]
            assert opp.total_stake == stake
            assert sum(opp.stakes) == pytest.approx(stake, rel=1e-9)


class TestSequentialScanBacktest:
    """Simulate multiple scans over time with shifting odds."""

    def test_odds_deterioration_closes_arb(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        t0 = find_arbs(events, min_margin=0.01)
        assert len(t0) >= 1

        # Worsen the best Celtics price
        for bm in events[0].bookmakers:
            if bm.key == "fanduel":
                for m in bm.markets:
                    for o in m.outcomes:
                        if o.name == "Boston Celtics":
                            o.price = 1.90
        t1 = find_arbs(events, min_margin=0.01)
        h2h_t0 = [r for r in t0 if r.market_key == "h2h"][0]
        h2h_t1 = [r for r in t1 if r.market_key == "h2h"]
        if h2h_t1:
            assert h2h_t1[0].margin < h2h_t0.margin
        else:
            assert h2h_t1 == []

    def test_odds_improvement_increases_margin(self):
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        base = find_arbs(events, min_margin=0.0)[0].margin

        for bm in events[0].bookmakers:
            if bm.key == "fanduel":
                for m in bm.markets:
                    for o in m.outcomes:
                        if o.name == "Boston Celtics":
                            o.price = 2.50
        improved = find_arbs(events, min_margin=0.0)[0].margin
        assert improved > base

    def test_repeated_scans_with_dedup_keys(self):
        """Same structural arb across scans should dedup; margin change should not."""
        events = OddsClient.parse_events(NBA_TWO_WAY_ARB)
        scan1 = find_arbs(events, min_margin=0.01)
        key1 = make_dedup_key(scan1[0])

        for bm in events[0].bookmakers:
            for m in bm.markets:
                for o in m.outcomes:
                    o.price *= 1.02
        scan2 = find_arbs(events, min_margin=0.01)
        key2 = make_dedup_key(scan2[0])

        assert key1 == key2
        assert scan2[0].margin != scan1[0].margin
