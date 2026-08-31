"""What the government takes out of a settled position, and what that does to it.

:mod:`src.commission` answers "what does the *venue* take out?"  This answers the
same question for the other party that takes a cut, and it is the more damaging
one, because it is not proportional to profit.  Tax is assessed on **gross
winnings**; losses are merely a *deduction* against them, and from tax year 2026
only 90% of losses may be deducted at all.

That asymmetry is exactly load-bearing for this repository.  An arbitrage is
risk-free before tax and is **not** risk-free after it: the winning leg's gross
winnings are taxable in full while the losing leg's stake is only 90% deductible,
so a position whose pre-tax floor is a flat +$100 across every outcome has an
after-tax floor that differs per outcome and can be negative.  A 2% edge does not
survive a rate that is applied to a number eleven times larger than the edge.

**This module deliberately computes no tax.**  It computes the *basis* — the
gross winnings and the deductible losses a position produces in a given outcome —
and stops there.  Applying rates is the dashboard's job, in one place in its
JavaScript, because the operator changes the rate with a picker and the answer has
to move without a new collection run.  Splitting it this way means the rule that
turns a basis into a bill exists exactly once, and the rule that derives a basis
from a settlement exists exactly once here, rather than each existing twice and
drifting.

Nothing in the collection, detection, alerting or ranking path reads this module.
It is a reporting overlay: the numbers a run is judged by stay pre-tax.

Informational, not tax advice — the same stance ``docs/PROMO_CAMPAIGN.md`` already
takes.  The rates below are starting points the operator picks from, not a
determination about anybody's situation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

#: Share of gambling losses that may be deducted against gambling winnings, for
#: tax years beginning in 2026.  Before that the share was 1.0, and setting it
#: back to 1.0 is how the dashboard shows what the change costs — which is the
#: whole reason this is a named constant and not a literal ``0.9`` in a formula.
DEDUCTIBLE_SHARE = 0.90


@dataclass(frozen=True)
class TaxBasis:
    """Gross winnings and deductible losses, before any rate is applied.

    Kept apart rather than netted because netting them is precisely what the law
    no longer permits in full: ``winnings - losses`` is the profit, but the
    taxable amount is ``winnings - min(losses * share, winnings)``, and those two
    stop being equal the moment *share* is below 1.0.  A payload that carried
    only the profit could not express the difference, which is why every surface
    here serialises the pair.
    """

    winnings: float = 0.0
    losses: float = 0.0

    def __add__(self, other: "TaxBasis") -> "TaxBasis":
        if not isinstance(other, TaxBasis):
            return NotImplemented
        return TaxBasis(self.winnings + other.winnings, self.losses + other.losses)

    @property
    def profit(self) -> float:
        """The ordinary profit this basis describes.

        Every caller builds a basis beside a profit it computed by its own
        settlement arithmetic, and this is what those tests assert against: if
        the two disagree the basis is wrong, and the disagreement is visible
        without knowing anything about tax.
        """
        return self.winnings - self.losses

    def rounded(self, digits: int = 2) -> "TaxBasis":
        return TaxBasis(round(self.winnings, digits), round(self.losses, digits))

    def payload(self, digits: int = 2) -> dict[str, float]:
        return {
            "winnings": round(self.winnings, digits),
            "losses": round(self.losses, digits),
        }


#: The basis of a leg that neither won nor lost anything.
NO_BASIS = TaxBasis()


def basis(at_risk: float, cash_back: float) -> TaxBasis:
    """The basis one leg produces, from the operator's cash in and cash out.

    *at_risk* is the operator's **own** money staked and *cash_back* is the cash
    the leg returns, stake included.  Stating it as those two numbers rather than
    as a settlement result is what lets one function serve every caller, and it
    gets the three cases that are easy to get wrong for free:

    * A **push** returns the stake, so ``cash_back == at_risk`` and the leg
      contributes to neither side.  Treating a push as a win of zero would be
      harmless; treating it as a loss of the stake — which a result-based
      implementation invites — would invent a deduction that does not exist.
    * A **half-lose** returns half the stake, and the deductible loss is the half
      that was actually lost, not the whole stake.
    * A **bonus-credit** leg has ``at_risk == 0``, because none of the operator's
      money is on it.  Its entire cash return is therefore winnings and none of
      it is ever a deductible loss — the same fact ``betlog._cash_stake`` already
      encodes for the bankroll, and the same one ``docs/PROMO_CAMPAIGN.md``
      states as "a bonus win's whole return is profit".
    """
    delta = cash_back - at_risk
    if delta >= 0.0:
        return TaxBasis(delta, 0.0)
    return TaxBasis(0.0, -delta)


def basis_cents(at_risk: int, cash_back: int) -> tuple[int, int]:
    """The same rule in whole cents, returned as ``(winnings, losses)``.

    :mod:`src.betlog` holds every amount as an integer number of cents on
    purpose, and converting to float and back to accumulate a basis would
    reintroduce exactly the drift that discipline exists to prevent.  So the
    ledger gets its own arithmetic domain — but not its own *rule*: the
    comparison and its direction are stated once, here, beside the float form.
    """
    delta = cash_back - at_risk
    return (delta, 0) if delta >= 0 else (0, -delta)


def combine(bases: Iterable[TaxBasis]) -> TaxBasis:
    """Sum a position's legs into one basis."""
    total = NO_BASIS
    for item in bases:
        total = total + item
    return total


# ── the rates the picker offers ──────────────────────────────────────────────
#
# Presented as choices, never as a determination.  Two shapes of layer, because
# the two behave differently and blurring them understates the bill:
#
# * The **federal** layer taxes winnings less the capped loss deduction.
# * The **state** layer here taxes **gross winnings with no loss offset at all**.
#   That is the harshest of the arrangements in use — Illinois is explicitly one
#   (a flat 4.95% and no offset, as ``docs/PROMO_CAMPAIGN.md`` already records) —
#   and it is modelled that way on purpose, following the bias
#   :mod:`src.commission` sets for this repository: getting a charge wrong in the
#   generous direction manufactures an edge that is not there, while getting it
#   wrong in the strict direction only costs a position.  An operator whose state
#   lets winnings and losses net simply picks ``none`` here and reads the federal
#   figure.
#
# Only the two flat rates this repository can stand behind are named.  The rest
# are bare rates to dial to, rather than claims about a graduated bracket in a
# state nobody here has verified.

FEDERAL_PRESETS: tuple[tuple[str, float], ...] = (
    ("none", 0.0),
    ("22%", 0.22),
    ("24%", 0.24),
    ("32%", 0.32),
    ("37%", 0.37),
)

STATE_PRESETS: tuple[tuple[str, float], ...] = (
    ("none", 0.0),
    ("3.07% · PA flat", 0.0307),
    ("4.95% · IL flat", 0.0495),
    ("5.00%", 0.05),
    ("7.50%", 0.075),
    ("10.00%", 0.10),
)


def presets_payload() -> dict[str, object]:
    """What the dashboard builds its rate pickers from.

    Serialised rather than written into the page's JavaScript so the rates have
    one home, the same way ``report_copy.BRAND_LABELS`` is the one home for a
    brand's display name.
    """
    return {
        "deductible_share": DEDUCTIBLE_SHARE,
        "federal": [{"label": label, "rate": rate} for label, rate in FEDERAL_PRESETS],
        "state": [{"label": label, "rate": rate} for label, rate in STATE_PRESETS],
    }


__all__ = [
    "DEDUCTIBLE_SHARE",
    "FEDERAL_PRESETS",
    "NO_BASIS",
    "STATE_PRESETS",
    "TaxBasis",
    "basis",
    "basis_cents",
    "combine",
    "presets_payload",
]
