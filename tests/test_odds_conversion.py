from __future__ import annotations

import pytest

from src.normalize import american_to_decimal, decimal_to_american, ensure_decimal, implied_probability


class TestAmericanToDecimal:
    def test_positive_odds(self):
        assert american_to_decimal(150) == pytest.approx(2.50)

    def test_negative_odds(self):
        assert american_to_decimal(-110) == pytest.approx(1 + 100 / 110)

    def test_even_money(self):
        assert american_to_decimal(100) == pytest.approx(2.0)

    def test_heavy_favorite(self):
        assert american_to_decimal(-500) == pytest.approx(1.20)

    def test_big_underdog(self):
        assert american_to_decimal(1000) == pytest.approx(11.0)

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="cannot be zero"):
            american_to_decimal(0)


class TestDecimalToAmerican:
    def test_positive_american(self):
        assert decimal_to_american(2.50) == 150

    def test_negative_american(self):
        assert decimal_to_american(1.50) == -200

    def test_even_money(self):
        assert decimal_to_american(2.0) == 100

    def test_below_one_raises(self):
        with pytest.raises(ValueError):
            decimal_to_american(0.5)

    def test_exactly_one_raises(self):
        with pytest.raises(ValueError):
            decimal_to_american(1.0)


class TestImpliedProbability:
    def test_even_money(self):
        assert implied_probability(2.0) == pytest.approx(0.5)

    def test_heavy_favorite(self):
        assert implied_probability(1.20) == pytest.approx(1 / 1.20)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            implied_probability(-1.0)


class TestEnsureDecimal:
    def test_decimal_passthrough(self):
        assert ensure_decimal(2.50, "decimal") == 2.50

    def test_american_conversion(self):
        assert ensure_decimal(150, "american") == pytest.approx(2.50)
