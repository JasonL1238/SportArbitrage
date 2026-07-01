"""Round-trip and property-based backtests for odds normalization."""
from __future__ import annotations

import pytest

from src.normalize import american_to_decimal, decimal_to_american, ensure_decimal, implied_probability


AMERICAN_SAMPLES = [
    -500, -400, -300, -200, -150, -110, -105,
    100, 105, 110, 150, 200, 300, 500, 1000,
]

DECIMAL_SAMPLES = [
    1.05, 1.10, 1.20, 1.50, 1.91, 2.00, 2.10, 2.50, 3.00, 5.00, 10.00,
]


@pytest.mark.parametrize("american", AMERICAN_SAMPLES)
def test_american_decimal_round_trip(american: int):
    dec = american_to_decimal(american)
    back = decimal_to_american(dec)
    assert back == american


@pytest.mark.parametrize("decimal", DECIMAL_SAMPLES)
def test_decimal_american_round_trip(decimal: float):
    american = decimal_to_american(decimal)
    back = american_to_decimal(american)
    assert back == pytest.approx(decimal, rel=0.02)


@pytest.mark.parametrize("decimal", DECIMAL_SAMPLES)
def test_implied_probability_sums_for_fair_market(decimal: float):
    """Two equal outcomes at decimal d should sum to 2/d implied prob."""
    p = implied_probability(decimal)
    assert p == pytest.approx(1 / decimal, rel=1e-9)
    assert p * 2 == pytest.approx(2 / decimal, rel=1e-9)


@pytest.mark.parametrize("american", [150, -110, 200, -200])
def test_ensure_decimal_american(american: int):
    assert ensure_decimal(american, "american") == pytest.approx(american_to_decimal(american))


@pytest.mark.parametrize("decimal", DECIMAL_SAMPLES)
def test_ensure_decimal_passthrough(decimal: float):
    assert ensure_decimal(decimal, "decimal") == decimal


class TestArbMathViaAmericanOdds:
    """Backtest arb detection using American odds converted to decimal."""

    @pytest.mark.parametrize(
        "american_a,american_b,expect_arb",
        [
            (150, 150, True),   # +150 each → 2.5 decimal each → arb
            (-110, -110, False),
            (200, -200, False),  # 3.0 and 1.5 → no arb
            (180, 180, True),
        ],
    )
    def test_two_way_margin_from_american(self, american_a, american_b, expect_arb):
        from src.arb import arb_margin
        odds = [american_to_decimal(american_a), american_to_decimal(american_b)]
        margin = arb_margin(odds)
        if expect_arb:
            assert margin > 0
        else:
            assert margin <= 0
