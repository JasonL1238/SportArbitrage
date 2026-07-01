"""Parametrized backtests across curated odds scenarios.

Each scenario replays a realistic bookmaker snapshot through find_arbs and
validates margin, profit, stake math, and best-price selection.
"""
from __future__ import annotations

import pytest

from src.arb import arb_margin, find_arbs
from src.stake import guaranteed_profit, stake_split
from tests.conftest import build_event_from_scenario
from tests.fixtures.backtest_scenarios import (
    ALL_SCENARIOS,
    MARGIN_BACKTEST_CASES,
    STAKE_BACKTEST_CASES,
    BacktestScenario,
)


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
class TestScenarioBacktests:
  def test_arb_detection_matches_expectation(self, scenario: BacktestScenario):
      event = build_event_from_scenario(scenario)
      results = find_arbs(
          [event],
          min_margin=scenario.min_margin,
          total_stake=scenario.total_stake,
      )

      if scenario.name == "different_lines_no_merge":
          # Two separate spread lines — neither forms a complete arb pair
          assert len(results) == 0
          return

      if scenario.expect_arb:
          assert len(results) >= 1, f"Expected arb for {scenario.name}"
          opp = _pick_market_result(results, scenario)
          assert opp.margin >= scenario.min_margin
      else:
          matching = _matching_results(results, scenario)
          assert len(matching) == 0, (
              f"Unexpected arb for {scenario.name}: margin={matching[0].margin if matching else 'n/a'}"
          )

  def test_margin_matches_expected(self, scenario: BacktestScenario):
      if scenario.expected_margin is None:
          pytest.skip("no expected margin")

      if scenario.name == "different_lines_no_merge":
          return

      event = build_event_from_scenario(scenario)
      # Use a low floor so negative-margin groups are still returned for verification
      results = find_arbs([event], min_margin=-1.0, total_stake=scenario.total_stake)
      opp = _pick_market_result(results, scenario)
      assert opp.margin == pytest.approx(scenario.expected_margin, rel=1e-6)

  def test_stake_math_is_internally_consistent(self, scenario: BacktestScenario):
      event = build_event_from_scenario(scenario)
      results = find_arbs(
          [event],
          min_margin=0.0,
          total_stake=scenario.total_stake,
      )
      if scenario.name == "different_lines_no_merge":
          return
      if not results:
          return

      opp = _pick_market_result(results, scenario)
      assert sum(opp.stakes) == pytest.approx(scenario.total_stake, rel=1e-9)
      odds = [b.decimal_odds for b in opp.best_outcomes]
      payouts = [s * o for s, o in zip(opp.stakes, odds)]
      assert payouts[0] == pytest.approx(payouts[-1], rel=1e-9)
      assert opp.guaranteed_profit == pytest.approx(
          guaranteed_profit(opp.stakes, odds, scenario.total_stake), rel=1e-9
      )

  def test_best_book_selection(self, scenario: BacktestScenario):
      if scenario.expected_best_books is None:
          pytest.skip("no expected book mapping")

      event = build_event_from_scenario(scenario)
      results = find_arbs([event], min_margin=0.0, total_stake=scenario.total_stake)
      opp = _pick_market_result(results, scenario)

      actual = {b.outcome_name: b.bookmaker_key for b in opp.best_outcomes}
      assert actual == scenario.expected_best_books


def _pick_market_result(results, scenario: BacktestScenario):
    matching = _matching_results(results, scenario)
    assert matching, f"No result for {scenario.name}"
    return matching[0]


def _matching_results(results, scenario: BacktestScenario):
    return [
        r for r in results
        if r.market_key == scenario.market_key
        and (scenario.point is None or r.point == abs(scenario.point))
    ]


@pytest.mark.parametrize("case", STAKE_BACKTEST_CASES, ids=lambda c: f"stake_{c['stake']}_{c['odds']}")
class TestStakeBacktests:
    def test_stakes_sum_to_total(self, case: dict):
        stakes = stake_split(case["odds"], case["stake"])
        assert sum(stakes) == pytest.approx(case["stake"], rel=1e-9)

    def test_equal_payouts(self, case: dict):
        odds = case["odds"]
        stake = case["stake"]
        stakes = stake_split(odds, stake)
        payouts = [s * o for s, o in zip(stakes, odds)]
        for p in payouts[1:]:
            assert p == pytest.approx(payouts[0], rel=1e-9)

    def test_profit_formula(self, case: dict):
        odds = case["odds"]
        stake = case["stake"]
        stakes = stake_split(odds, stake)
        profit = guaranteed_profit(stakes, odds, stake)
        inverse_sum = sum(1 / o for o in odds)
        expected = stake / inverse_sum - stake
        assert profit == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize("case", MARGIN_BACKTEST_CASES, ids=lambda c: str(c["odds"]))
class TestMarginBacktests:
    def test_margin_sign(self, case: dict):
        margin = arb_margin(case["odds"])
        if case.get("expect_zero"):
            assert margin == pytest.approx(0.0, abs=1e-12)
        elif case.get("expect_positive"):
            assert margin > 0
        else:
            assert margin < 0

    def test_margin_formula(self, case: dict):
        odds = case["odds"]
        assert arb_margin(odds) == pytest.approx(1 - sum(1 / o for o in odds), rel=1e-12)


class TestMultiScenarioBatch:
    """Simulate scanning many events at once (like a real scan)."""

    def test_all_arb_scenarios_found_in_batch(self):
        arb_scenarios = [s for s in ALL_SCENARIOS if s.expect_arb and s.name != "different_lines_no_merge"]
        events = [build_event_from_scenario(s) for s in arb_scenarios]
        results = find_arbs(events, min_margin=0.005, total_stake=100.0)
        assert len(results) >= len(arb_scenarios)

    def test_no_arb_scenarios_produce_nothing_at_default_threshold(self):
        no_arb = [s for s in ALL_SCENARIOS if not s.expect_arb and s.name != "different_lines_no_merge"]
        for scenario in no_arb:
            event = build_event_from_scenario(scenario)
            results = find_arbs([event], min_margin=scenario.min_margin)
            matching = _matching_results(results, scenario)
            assert len(matching) == 0, scenario.name

    def test_varying_stake_sizes_produce_linear_profit(self):
        scenario = next(s for s in ALL_SCENARIOS if s.name == "classic_cross_book_4pct")
        event = build_event_from_scenario(scenario)
        profits = []
        for stake in [50.0, 100.0, 200.0, 500.0]:
            opp = find_arbs([event], min_margin=0.0, total_stake=stake)[0]
            profits.append(opp.guaranteed_profit / stake)
        assert profits[0] == pytest.approx(profits[1], rel=1e-6)
        assert profits[2] == pytest.approx(profits[3], rel=1e-6)
