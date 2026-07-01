from __future__ import annotations

import pytest

from src.stake import guaranteed_profit, stake_split


class TestStakeSplit:
    def test_stakes_sum_to_total(self):
        odds = [2.20, 2.30]
        total = 100.0
        stakes = stake_split(odds, total)
        assert sum(stakes) == pytest.approx(total)

    def test_equal_payouts(self):
        odds = [2.20, 2.30]
        stakes = stake_split(odds, 100.0)
        payouts = [s * o for s, o in zip(stakes, odds)]
        assert payouts[0] == pytest.approx(payouts[1])

    def test_three_way_equal_payouts(self):
        odds = [3.80, 4.00, 2.80]
        stakes = stake_split(odds, 1000.0)
        payouts = [s * o for s, o in zip(stakes, odds)]
        assert payouts[0] == pytest.approx(payouts[1])
        assert payouts[1] == pytest.approx(payouts[2])

    def test_higher_odds_get_lower_stake(self):
        odds = [2.20, 2.30]
        stakes = stake_split(odds, 100.0)
        # Higher odds → lower stake needed
        assert stakes[1] < stakes[0]

    def test_invalid_odds_raises(self):
        with pytest.raises(ValueError):
            stake_split([2.0, 0], 100.0)

    def test_empty_odds_raises(self):
        with pytest.raises(ValueError):
            stake_split([], 100.0)


class TestGuaranteedProfit:
    def test_arb_has_positive_profit(self):
        odds = [2.20, 2.30]
        total = 100.0
        stakes = stake_split(odds, total)
        profit = guaranteed_profit(stakes, odds, total)
        assert profit > 0

    def test_no_arb_negative_profit(self):
        odds = [1.90, 1.90]
        total = 100.0
        stakes = stake_split(odds, total)
        profit = guaranteed_profit(stakes, odds, total)
        assert profit < 0

    def test_profit_matches_margin(self):
        odds = [2.20, 2.30]
        total = 100.0
        stakes = stake_split(odds, total)
        profit = guaranteed_profit(stakes, odds, total)
        # Margin-derived expected profit
        inverse_sum = sum(1 / o for o in odds)
        expected_payout = total / inverse_sum
        expected_profit = expected_payout - total
        assert profit == pytest.approx(expected_profit, rel=1e-6)

    def test_three_way_profit(self):
        odds = [3.80, 4.00, 2.80]
        total = 500.0
        stakes = stake_split(odds, total)
        profit = guaranteed_profit(stakes, odds, total)
        assert profit > 0
