from __future__ import annotations

import pytest

from src.arb import arb_margin, find_arbs
from src.models import Event


class TestArbMargin:
    def test_positive_arb(self):
        # 1/2.20 + 1/2.30 ≈ 0.889 → margin ≈ 0.111
        margin = arb_margin([2.20, 2.30])
        assert margin > 0

    def test_no_arb(self):
        # 1/1.90 + 1/1.90 ≈ 1.053 → margin ≈ -0.053
        margin = arb_margin([1.90, 1.90])
        assert margin < 0

    def test_exact_break_even(self):
        # 1/2.0 + 1/2.0 = 1.0 → margin = 0
        assert arb_margin([2.0, 2.0]) == pytest.approx(0.0)

    def test_three_way_arb(self):
        # 1/3.80 + 1/4.00 + 1/2.80 ≈ 0.870 → margin ≈ 0.130
        margin = arb_margin([3.80, 4.00, 2.80])
        assert margin > 0

    def test_three_way_no_arb(self):
        margin = arb_margin([2.50, 3.00, 2.80])
        assert margin < 0

    def test_empty_odds(self):
        assert arb_margin([]) == -1.0

    def test_zero_odds(self):
        assert arb_margin([2.0, 0]) == -1.0


class TestFindArbs:
    def test_two_way_arb_detected(self, two_way_arb_event: Event):
        results = find_arbs([two_way_arb_event], min_margin=0.01)
        assert len(results) == 1
        opp = results[0]
        assert opp.margin > 0.01
        assert opp.outcome_count == 2
        assert opp.market_key == "h2h"
        # Best odds should come from different books
        books = {b.bookmaker_key for b in opp.best_outcomes}
        assert len(books) == 2

    def test_no_arb_skipped(self, no_arb_event: Event):
        results = find_arbs([no_arb_event], min_margin=0.01)
        assert len(results) == 0

    def test_three_way_arb_detected(self, three_way_arb_event: Event):
        results = find_arbs([three_way_arb_event], min_margin=0.01)
        assert len(results) == 1
        opp = results[0]
        assert opp.outcome_count == 3
        assert opp.sport_key == "soccer_epl"

    def test_below_threshold_skipped(self, two_way_arb_event: Event):
        results = find_arbs([two_way_arb_event], min_margin=0.50)
        assert len(results) == 0

    def test_spread_arb_detected(self, spread_arb_event: Event):
        results = find_arbs([spread_arb_event], min_margin=0.01)
        assert len(results) == 1
        opp = results[0]
        assert opp.market_key == "spreads"
        assert opp.point == 3.5

    def test_totals_arb_detected(self, totals_arb_event: Event):
        results = find_arbs([totals_arb_event], min_margin=0.01)
        assert len(results) == 1
        opp = results[0]
        assert opp.market_key == "totals"
        assert opp.point == 215.5
