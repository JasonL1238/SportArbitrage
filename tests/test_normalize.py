"""Odds conversion, including the values that are not prices at all."""
from __future__ import annotations

import pytest

from src.normalize import (
    MAX_DECIMAL_ODDS,
    MIN_DECIMAL_ODDS,
    american_to_decimal,
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)

#: Every value a real book publishes, spanning both signs and both extremes.
AMERICAN_SAMPLES = [
    -10000, -5000, -1000, -500, -400, -300, -200, -150, -110, -105, -100,
    100, 105, 110, 150, 200, 300, 500, 1000, 2400, 10000,
]

DECIMAL_SAMPLES = [1.05, 1.10, 1.20, 1.50, 1.91, 2.00, 2.10, 2.50, 3.00, 5.00, 10.00, 25.0]


class TestAmericanToDecimal:
    def test_positive_odds(self) -> None:
        assert american_to_decimal(150) == pytest.approx(2.50)

    def test_negative_odds(self) -> None:
        assert american_to_decimal(-110) == pytest.approx(1 + 100 / 110)

    def test_even_money_from_either_spelling(self) -> None:
        assert american_to_decimal(100) == pytest.approx(2.0)
        assert american_to_decimal(-100) == pytest.approx(2.0)

    def test_heavy_favourite(self) -> None:
        assert american_to_decimal(-500) == pytest.approx(1.20)

    def test_big_underdog(self) -> None:
        assert american_to_decimal(1000) == pytest.approx(11.0)

    @pytest.mark.parametrize("bad", [0, 1, -1, 50, -50, 99, -99, 99.9])
    def test_values_between_minus_100_and_plus_100_are_not_prices(self, bad: float) -> None:
        """The dangerous case, because the result would look entirely plausible:
        -50 would convert to 3.00, indistinguishable from a genuine +200."""
        with pytest.raises(ValueError, match="do not describe a price"):
            american_to_decimal(bad)


class TestDecimalToAmerican:
    def test_positive_american(self) -> None:
        assert decimal_to_american(2.50) == 150

    def test_negative_american(self) -> None:
        assert decimal_to_american(1.50) == -200

    def test_even_money_canonicalises_to_plus_100(self) -> None:
        assert decimal_to_american(2.0) == 100

    @pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -1.0])
    def test_odds_at_or_below_evens_are_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError):
            decimal_to_american(bad)


class TestRoundTrip:
    @pytest.mark.parametrize("american", [a for a in AMERICAN_SAMPLES if a != -100])
    def test_american_survives_a_round_trip_exactly(self, american: int) -> None:
        assert decimal_to_american(american_to_decimal(american)) == american

    def test_minus_100_canonicalises_to_plus_100(self) -> None:
        """The one value that cannot round-trip, because -100 and +100 are two
        spellings of the same price and the conversion has to pick one."""
        assert decimal_to_american(american_to_decimal(-100)) == 100

    @pytest.mark.parametrize("decimal", DECIMAL_SAMPLES)
    def test_decimal_survives_a_round_trip_within_american_granularity(
        self, decimal: float
    ) -> None:
        """American odds are integers, so the round trip is lossy.  The loss is
        largest around even money, where one American point is worth 0.005
        decimal — 0.25% — and it shrinks at both extremes."""
        back = american_to_decimal(decimal_to_american(decimal))
        assert back == pytest.approx(decimal, rel=0.0025)

    def test_round_trip_error_is_tiny_for_long_prices(self) -> None:
        """At +2400 a single American point moves decimal by 0.01, i.e. 0.04%.
        A tolerance wide enough for even money is 6x too loose here."""
        assert american_to_decimal(decimal_to_american(25.0)) == pytest.approx(25.0, rel=1e-9)


class TestImpliedProbability:
    def test_even_money(self) -> None:
        assert implied_probability(2.0) == pytest.approx(0.5)

    def test_heavy_favourite(self) -> None:
        assert implied_probability(1.20) == pytest.approx(1 / 1.20)

    @pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -1.0])
    def test_odds_that_would_imply_a_probability_above_one_are_rejected(
        self, bad: float
    ) -> None:
        """A "probability" above 1 would silently corrupt every overround sum
        computed from it, so it is refused at the source."""
        with pytest.raises(ValueError):
            implied_probability(bad)

    @pytest.mark.parametrize("decimal", DECIMAL_SAMPLES)
    def test_probability_is_the_reciprocal(self, decimal: float) -> None:
        assert implied_probability(decimal) == pytest.approx(1 / decimal, rel=1e-12)

    def test_a_fair_two_way_market_sums_to_one(self) -> None:
        assert implied_probability(3.0) + implied_probability(1.5) == pytest.approx(1.0)


class TestPlausibility:
    @pytest.mark.parametrize("odds", DECIMAL_SAMPLES)
    def test_real_prices_are_plausible(self, odds: float) -> None:
        assert is_plausible_decimal_odds(odds)

    @pytest.mark.parametrize("odds", [1.0, 1.001, 1.009, 1001.0, 5000.0])
    def test_absurd_prices_are_not(self, odds: float) -> None:
        """1.001 is "risk 10,000 to win 10" — arithmetically fine, and a units
        error every time it appears in a real feed."""
        assert not is_plausible_decimal_odds(odds)

    def test_the_bounds_themselves_are_plausible(self) -> None:
        assert is_plausible_decimal_odds(MIN_DECIMAL_ODDS)
        assert is_plausible_decimal_odds(MAX_DECIMAL_ODDS)
