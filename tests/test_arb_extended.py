"""Extended arb engine tests: stale filtering, grouping, edge cases."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.arb import filter_stale_odds, find_arbs
from src.models import Bookmaker, BookmakerMarket, Event, Outcome
from tests.conftest import _bm, _make_event


def _event_with_timestamps(
    fresh_seconds_ago: int,
    stale_seconds_ago: int,
) -> Event:
    now = datetime.now(UTC)
    fresh_ts = now - timedelta(seconds=fresh_seconds_ago)
    stale_ts = now - timedelta(seconds=stale_seconds_ago)
    return _make_event(
        bookmakers=[
            Bookmaker(
                key="fresh",
                title="Fresh Book",
                markets=[
                    BookmakerMarket(
                        key="h2h",
                        last_update=fresh_ts,
                        outcomes=[
                            Outcome(name="Team A", price=2.20),
                            Outcome(name="Team B", price=1.70),
                        ],
                    )
                ],
            ),
            Bookmaker(
                key="stale",
                title="Stale Book",
                markets=[
                    BookmakerMarket(
                        key="h2h",
                        last_update=stale_ts,
                        outcomes=[
                            Outcome(name="Team A", price=1.50),
                            Outcome(name="Team B", price=3.00),
                        ],
                    )
                ],
            ),
        ],
    )


class TestFilterStaleOdds:
    def test_removes_stale_markets(self):
        event = _event_with_timestamps(fresh_seconds_ago=30, stale_seconds_ago=600)
        filtered = filter_stale_odds([event], max_age_seconds=300)
        books = filtered[0].bookmakers
        assert len(books) == 1
        assert books[0].key == "fresh"

    def test_keeps_all_when_within_age(self):
        event = _event_with_timestamps(fresh_seconds_ago=30, stale_seconds_ago=200)
        filtered = filter_stale_odds([event], max_age_seconds=300)
        assert len(filtered[0].bookmakers) == 2

    def test_removes_bookmaker_with_only_stale_markets(self):
        now = datetime.now(UTC)
        event = _make_event(
            bookmakers=[
                Bookmaker(
                    key="all_stale",
                    title="All Stale",
                    markets=[
                        BookmakerMarket(
                            key="h2h",
                            last_update=now - timedelta(hours=1),
                            outcomes=[Outcome(name="Team A", price=2.0), Outcome(name="Team B", price=2.0)],
                        ),
                        BookmakerMarket(
                            key="spreads",
                            last_update=now - timedelta(hours=1),
                            outcomes=[
                                Outcome(name="Team A", price=1.91, point=-3.5),
                                Outcome(name="Team B", price=1.91, point=3.5),
                            ],
                        ),
                    ],
                ),
            ],
        )
        filtered = filter_stale_odds([event], max_age_seconds=300)
        assert filtered[0].bookmakers == []

    def test_partial_staleness_keeps_fresh_markets_only(self):
        now = datetime.now(UTC)
        event = _make_event(
            bookmakers=[
                Bookmaker(
                    key="mixed",
                    title="Mixed Book",
                    markets=[
                        BookmakerMarket(
                            key="h2h",
                            last_update=now - timedelta(seconds=60),
                            outcomes=[Outcome(name="Team A", price=2.0), Outcome(name="Team B", price=2.0)],
                        ),
                        BookmakerMarket(
                            key="spreads",
                            last_update=now - timedelta(hours=2),
                            outcomes=[
                                Outcome(name="Team A", price=2.20, point=-3.5),
                                Outcome(name="Team B", price=1.70, point=3.5),
                            ],
                        ),
                    ],
                ),
            ],
        )
        filtered = filter_stale_odds([event], max_age_seconds=300)
        assert len(filtered[0].bookmakers) == 1
        assert len(filtered[0].bookmakers[0].markets) == 1
        assert filtered[0].bookmakers[0].markets[0].key == "h2h"

    def test_stale_filter_changes_arb_outcome(self):
        """Removing stale inflated odds should eliminate a false arb."""
        event = _event_with_timestamps(fresh_seconds_ago=30, stale_seconds_ago=600)
        before = find_arbs([event], min_margin=0.0)
        after = find_arbs(filter_stale_odds([event], 300), min_margin=0.0)
        assert len(before) >= 1
        assert len(after) == 0


class TestMarketGrouping:
    def test_spread_opposite_points_grouped(self):
        event = _make_event(
            bookmakers=[
                _bm("book_a", "A", "spreads", [
                    Outcome(name="Team A", price=2.10, point=-3.5),
                    Outcome(name="Team B", price=1.80, point=3.5),
                ]),
                _bm("book_b", "B", "spreads", [
                    Outcome(name="Team A", price=1.85, point=-3.5),
                    Outcome(name="Team B", price=2.15, point=3.5),
                ]),
            ],
        )
        results = find_arbs([event], min_margin=0.0)
        assert len(results) == 1
        assert results[0].point == 3.5

    def test_different_spread_lines_are_separate(self):
        event = _make_event(
            bookmakers=[
                _bm("book_a", "A", "spreads", [
                    Outcome(name="Team A", price=2.20, point=-3.5),
                    Outcome(name="Team B", price=1.70, point=3.5),
                ]),
                _bm("book_b", "B", "spreads", [
                    Outcome(name="Team A", price=1.70, point=-4.5),
                    Outcome(name="Team B", price=2.20, point=4.5),
                ]),
            ],
        )
        # Each line forms its own group; include negative margins for inspection
        results = find_arbs([event], min_margin=-1.0)
        points = {r.point for r in results}
        assert points == {3.5, 4.5}
        for r in results:
            assert r.outcome_count == 2

    def test_h2h_and_spreads_both_detected(self):
        event = _make_event(
            bookmakers=[
                Bookmaker(
                    key="book_a",
                    title="A",
                    markets=[
                        BookmakerMarket(
                            key="h2h",
                            last_update=datetime.now(UTC),
                            outcomes=[Outcome(name="Team A", price=2.20), Outcome(name="Team B", price=1.70)],
                        ),
                        BookmakerMarket(
                            key="spreads",
                            last_update=datetime.now(UTC),
                            outcomes=[
                                Outcome(name="Team A", price=2.15, point=-3.5),
                                Outcome(name="Team B", price=1.80, point=3.5),
                            ],
                        ),
                    ],
                ),
                Bookmaker(
                    key="book_b",
                    title="B",
                    markets=[
                        BookmakerMarket(
                            key="h2h",
                            last_update=datetime.now(UTC),
                            outcomes=[Outcome(name="Team A", price=1.80), Outcome(name="Team B", price=2.30)],
                        ),
                        BookmakerMarket(
                            key="spreads",
                            last_update=datetime.now(UTC),
                            outcomes=[
                                Outcome(name="Team A", price=1.85, point=-3.5),
                                Outcome(name="Team B", price=2.10, point=3.5),
                            ],
                        ),
                    ],
                ),
            ],
        )
        results = find_arbs([event], min_margin=0.01)
        markets = {r.market_key for r in results}
        assert "h2h" in markets
        assert "spreads" in markets

    def test_multiple_events_independent(self):
        arb = _make_event(
            event_id="arb_evt",
            bookmakers=[
                _bm("a", "A", "h2h", [Outcome(name="Team A", price=2.20), Outcome(name="Team B", price=1.70)]),
                _bm("b", "B", "h2h", [Outcome(name="Team A", price=1.80), Outcome(name="Team B", price=2.30)]),
            ],
        )
        no_arb = _make_event(
            event_id="no_arb_evt",
            bookmakers=[
                _bm("a", "A", "h2h", [Outcome(name="Team A", price=1.90), Outcome(name="Team B", price=1.90)]),
            ],
        )
        results = find_arbs([arb, no_arb], min_margin=0.01)
        event_ids = {r.event_id for r in results}
        assert "arb_evt" in event_ids
        assert "no_arb_evt" not in event_ids


class TestBestPriceSelection:
    def test_picks_highest_odds_per_outcome(self):
        event = _make_event(
            bookmakers=[
                _bm("low", "Low", "h2h", [Outcome(name="Team A", price=1.50), Outcome(name="Team B", price=2.50)]),
                _bm("mid", "Mid", "h2h", [Outcome(name="Team A", price=2.00), Outcome(name="Team B", price=2.00)]),
                _bm("high", "High", "h2h", [Outcome(name="Team A", price=2.50), Outcome(name="Team B", price=1.50)]),
            ],
        )
        results = find_arbs([event], min_margin=0.0)
        opp = results[0]
        by_name = {b.outcome_name: b for b in opp.best_outcomes}
        assert by_name["Team A"].bookmaker_key == "high"
        assert by_name["Team B"].bookmaker_key == "low"

    def test_same_book_can_win_multiple_outcomes(self):
        event = _make_event(
            bookmakers=[
                _bm("winner", "Winner", "h2h", [Outcome(name="Team A", price=2.50), Outcome(name="Team B", price=2.50)]),
                _bm("loser", "Loser", "h2h", [Outcome(name="Team A", price=1.50), Outcome(name="Team B", price=1.50)]),
            ],
        )
        results = find_arbs([event], min_margin=0.0)
        opp = results[0]
        books = {b.bookmaker_key for b in opp.best_outcomes}
        assert books == {"winner"}


class TestEdgeCases:
    def test_empty_bookmakers(self):
        event = _make_event(bookmakers=[])
        assert find_arbs([event]) == []

    def test_empty_events_list(self):
        assert find_arbs([]) == []

    def test_negative_odds_in_margin(self):
        from src.arb import arb_margin
        assert arb_margin([2.0, -1.0]) == -1.0

    def test_opp_metadata_populated(self, two_way_arb_event):
        results = find_arbs([two_way_arb_event], min_margin=0.01, total_stake=250.0)
        opp = results[0]
        assert opp.home_team == "Team A"
        assert opp.away_team == "Team B"
        assert opp.total_stake == 250.0
        assert opp.implied_prob_sum == pytest.approx(sum(1 / (b.effective_decimal_odds or b.decimal_odds) for b in opp.best_outcomes))
        assert opp.margin_pct.endswith("%")
        assert opp.detected_at.tzinfo is not None

    def test_fees_and_slippage_reduce_margin(self, two_way_arb_event):
        raw = find_arbs([two_way_arb_event], min_margin=0.01, total_stake=100.0)
        after_costs = find_arbs(
            [two_way_arb_event],
            min_margin=0.01,
            total_stake=100.0,
            fee_rate=0.10,
            slippage_bps=50,
        )

        assert len(raw) == 1
        assert len(after_costs) == 1
        assert after_costs[0].margin < raw[0].margin
        assert after_costs[0].fees_applied == 0.10
        assert after_costs[0].slippage_applied == 50

    def test_large_costs_can_remove_arb(self, two_way_arb_event):
        after_costs = find_arbs(
            [two_way_arb_event],
            min_margin=0.01,
            total_stake=100.0,
            fee_rate=0.40,
            slippage_bps=500,
        )

        assert after_costs == []

    def test_liquidity_cap_is_recorded(self, two_way_arb_event):
        for bookmaker in two_way_arb_event.bookmakers:
            for market in bookmaker.markets:
                for outcome in market.outcomes:
                    outcome.liquidity = 25.0

        opp = find_arbs([two_way_arb_event], min_margin=0.01, total_stake=100.0)[0]

        assert opp.max_executable_stake is not None
        assert opp.max_executable_stake < 100.0
