"""The taxable basis: what a settled position would be taxed *on*.

No rates here, because :mod:`src.tax` deliberately applies none — the dashboard
does that, once, so the operator can change a rate without a new run.  What is
tested here is the part that must be right before any rate is applied: that
winnings and losses are counted separately and that the three settlements which
are easy to get wrong — a push, a half-lose and a bonus-credit stake — land where
they belong.
"""
from __future__ import annotations

import pytest

from src.tax import (
    DEDUCTIBLE_SHARE,
    FEDERAL_PRESETS,
    NO_BASIS,
    STATE_PRESETS,
    TaxBasis,
    basis,
    combine,
    presets_payload,
)


class TestOneLeg:
    def test_a_winner_is_all_winnings_and_no_loss(self) -> None:
        assert basis(1000.0, 2100.0) == TaxBasis(winnings=1100.0, losses=0.0)

    def test_a_loser_is_all_loss_and_no_winnings(self) -> None:
        assert basis(1000.0, 0.0) == TaxBasis(winnings=0.0, losses=1000.0)

    def test_a_push_is_neither(self) -> None:
        """The refunded stake is not a loss.

        A result-based implementation invites treating "did not win" as "lost",
        which would invent a $1,000 deduction out of a bet that never resolved.
        """
        assert basis(1000.0, 1000.0) == NO_BASIS

    def test_a_half_lose_deducts_only_the_half_that_lost(self) -> None:
        assert basis(1000.0, 500.0) == TaxBasis(winnings=0.0, losses=500.0)

    def test_a_half_win_counts_only_the_profit(self) -> None:
        # Half the stake wins at 3.00, the other half is refunded: 1500 + 500.
        assert basis(1000.0, 2000.0) == TaxBasis(winnings=1000.0, losses=0.0)

    def test_a_bonus_stake_is_never_a_deductible_loss(self) -> None:
        """No money at risk, so nothing to deduct — and the whole return is won.

        The same fact the ledger already encodes as ``_cash_stake`` and the promo
        playbook states as "a bonus win's whole return is profit".
        """
        assert basis(0.0, 300.0) == TaxBasis(winnings=300.0, losses=0.0)
        assert basis(0.0, 0.0) == NO_BASIS


class TestCombining:
    def test_legs_sum_on_both_sides_independently(self) -> None:
        total = combine([basis(1000.0, 2100.0), basis(1000.0, 0.0)])
        assert total == TaxBasis(winnings=1100.0, losses=1000.0)

    def test_an_empty_position_has_no_basis(self) -> None:
        assert combine([]) == NO_BASIS

    def test_the_profit_property_recovers_the_ordinary_number(self) -> None:
        """The invariant every caller's tests assert against.

        Winnings minus losses is the profit the rest of the pipeline already
        computes by its own arithmetic.  If a basis disagrees with the profit
        beside it, the basis is wrong, and the disagreement is visible without
        knowing a single thing about tax.
        """
        assert combine([basis(1000.0, 2100.0), basis(1000.0, 0.0)]).profit == pytest.approx(100.0)

    def test_winnings_and_losses_do_not_cancel(self) -> None:
        """The whole reason the pair is carried instead of the profit.

        Both positions below make $100.  They are taxed on very different
        numbers, and a payload holding only the profit could not tell them
        apart.
        """
        big = combine([basis(1000.0, 2100.0), basis(1000.0, 0.0)])
        small = combine([basis(50.0, 200.0), basis(50.0, 0.0)])
        assert big.profit == pytest.approx(small.profit)
        assert big.winnings > small.winnings * 5


class TestTheRatesTheDashboardOffers:
    def test_the_deduction_cap_is_the_2026_share(self) -> None:
        assert DEDUCTIBLE_SHARE == 0.90

    def test_both_preset_lists_start_at_no_tax(self) -> None:
        """The default has to be the page exactly as it was before this existed."""
        assert FEDERAL_PRESETS[0][1] == 0.0
        assert STATE_PRESETS[0][1] == 0.0

    def test_every_preset_is_a_fraction_not_a_percentage(self) -> None:
        for label, rate in (*FEDERAL_PRESETS, *STATE_PRESETS):
            assert 0.0 <= rate < 1.0, label

    def test_the_two_flat_state_rates_this_repository_names_are_present(self) -> None:
        """PA and IL are the two it can stand behind; the rest stay unnamed."""
        rates = {label: rate for label, rate in STATE_PRESETS}
        assert rates["3.07% · PA flat"] == 0.0307
        assert rates["4.95% · IL flat"] == 0.0495

    def test_the_payload_carries_the_rates_and_the_cap(self) -> None:
        payload = presets_payload()
        assert payload["deductible_share"] == DEDUCTIBLE_SHARE
        assert [entry["rate"] for entry in payload["federal"]] == [r for _, r in FEDERAL_PRESETS]
        assert [entry["label"] for entry in payload["state"]] == [s for s, _ in STATE_PRESETS]


class TestTheRuleTheDashboardWillApply:
    """Not implemented here — asserted here so the JS has something to match.

    The rate arithmetic lives in one place, the page, because the after-tax floor
    depends on the rate and has to move when the operator moves the picker.  This
    class states what that one place must compute, in the plainest possible form,
    so the expected numbers are written down somewhere reviewable rather than
    only inside a template string.
    """

    @staticmethod
    def _after_tax(b: TaxBasis, federal: float, state: float, share: float) -> float:
        deductible = min(b.losses * share, b.winnings)
        return b.profit - (max(0.0, b.winnings - deductible) * federal + b.winnings * state)

    def test_the_worked_example_from_a_two_thousand_dollar_arb(self) -> None:
        b = combine([basis(1000.0, 2100.0), basis(1000.0, 0.0)])
        # Federal 24% on 1100 - 900 = 200, plus 3.07% of the gross 1100.
        assert self._after_tax(b, 0.24, 0.0307, 0.90) == pytest.approx(18.23, abs=0.005)

    def test_the_cap_is_what_costs_the_money(self) -> None:
        b = combine([basis(1000.0, 2100.0), basis(1000.0, 0.0)])
        uncapped = self._after_tax(b, 0.24, 0.0, 1.00)
        capped = self._after_tax(b, 0.24, 0.0, 0.90)
        assert uncapped == pytest.approx(76.0)
        assert capped == pytest.approx(52.0)

    def test_a_thin_edge_goes_negative_after_tax(self) -> None:
        """The finding the whole feature exists to surface."""
        b = combine([basis(1000.0, 2040.0), basis(1000.0, 0.0)])
        assert b.profit == pytest.approx(40.0)
        assert self._after_tax(b, 0.24, 0.0307, 0.90) < 0.0

    def test_the_deduction_never_exceeds_the_winnings(self) -> None:
        """A losing position is not a negative tax bill.

        Capping the deduction at winnings is what keeps a bad day from reading
        as a refund the page has no business promising.
        """
        b = combine([basis(1000.0, 0.0), basis(1000.0, 0.0)])
        assert self._after_tax(b, 0.37, 0.0, 0.90) == pytest.approx(-2000.0)

    def test_no_rate_means_no_change(self) -> None:
        b = combine([basis(1000.0, 2100.0), basis(1000.0, 0.0)])
        assert self._after_tax(b, 0.0, 0.0, 0.90) == pytest.approx(b.profit)
