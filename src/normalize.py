"""Odds format conversion.

American odds mean "risk 100 to win X" (positive) or "risk X to win 100"
(negative), so values strictly between -100 and +100 do not describe a price at
all.  Accepting one silently is the dangerous case: -50 converts to a perfectly
plausible 3.00, indistinguishable downstream from a genuine +200.  The domain is
therefore enforced rather than assumed.
"""
from __future__ import annotations

#: The floor of the domain.  Anything at or below 1.0 is "risk everything to win
#: nothing" and cannot be a price at all; a units error lands far outside this.
#:
#: This was 1.01, on the reasoning that a price worse than -10000 American — risk
#: ten thousand to win one hundred — is not something a book publishes.  A live
#: run falsified that: Pinnacle quoted **-11540** (decimal 1.00867) on a heavy
#: favourite in an ITF tennis match, and FanDuel quoted 1.005 on a suspended
#: runner.  Both are real prices, so rejecting them was rejecting data rather than
#: catching a fault, and it reported a genuine quote as a scaling error.
#:
#: 1.001 is -100000: still impossible to reach by any real scaling mistake
#: (Kambi's undivided thousandths land above :data:`MAX_DECIMAL_ODDS`, not below
#: this), while leaving room for whatever extreme a book actually prints.
MIN_DECIMAL_ODDS = 1.001

#: Longer than any price on a two-sided baseball market.
MAX_DECIMAL_ODDS = 1000.0


def american_to_decimal(odds: int | float) -> float:
    """Convert American odds to decimal odds.

    +150 -> 2.50  (risk 100 to win 150, total return 250)
    -110 -> 1.909 (risk 110 to win 100, total return 209.09)
    +100 -> 2.00  (even money)

    Raises ``ValueError`` for ``-100 < odds < 100``, which is not a price.
    """
    odds = float(odds)
    if -100.0 < odds < 100.0:
        raise ValueError(
            f"American odds must be <= -100 or >= +100, got {odds:+g}: "
            "values in between do not describe a price"
        )
    if odds > 0:
        return 1 + odds / 100
    return 1 + 100 / abs(odds)


def decimal_to_american(odds: float) -> int:
    """Convert decimal odds to American odds.

    Even money comes back as ``+100`` rather than ``-100``; both spellings mean
    the same price, and settling on one keeps the conversion round-trippable.
    """
    if odds <= 1.0:
        raise ValueError(f"Decimal odds must exceed 1.0, got {odds}")
    if odds >= 2.0:
        return round((odds - 1) * 100)
    return round(-100 / (odds - 1))


def fractional_to_decimal(numerator: float, denominator: float) -> float:
    """Convert UK fractional odds to decimal odds.

    ``10/13`` -> 1.7692 (stake 13 to win 10, total return 23 per 13 staked)
    ``11/10`` -> 2.1000
    ``1/1``   -> 2.0000 (even money)

    The fraction states **profit over stake**, so the decimal price — which is
    total return over stake — is ``1 + numerator/denominator``.

    That ``1 +`` is the whole reason this function exists rather than being
    inlined.  :func:`src.sources.thescore._decimal_odds` divides without it, and
    is right to: theScore publishes a decimal price *as a rational*
    (``13387/2000`` is 6.6935), not a fractional price.  bet365's ``OD=`` is a
    true fraction.  Borrowing the wrong one of those two is understating every
    price by exactly 1.0, and the result stays inside
    :func:`is_plausible_decimal_odds`, so nothing downstream would catch it —
    ``10/13`` would publish as 0.769, and the pair of legs it belongs to would
    look like a guaranteed profit.
    """
    numerator = float(numerator)
    denominator = float(denominator)
    if denominator <= 0 or numerator <= 0:
        raise ValueError(
            f"fractional odds must have positive terms, got "
            f"{numerator:g}/{denominator:g}"
        )
    return 1 + numerator / denominator


def implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to the probability they imply (0-1).

    Rejects ``decimal_odds <= 1.0``, which would yield a "probability" of 1 or
    more — a nonsense value that would then propagate into every overround sum
    computed from it.
    """
    if decimal_odds <= 1.0:
        raise ValueError(
            f"Decimal odds must exceed 1.0 to imply a probability below 1, got {decimal_odds}"
        )
    return 1 / decimal_odds


def is_plausible_decimal_odds(odds: float) -> bool:
    """Is this a price a sportsbook would actually publish?"""
    return MIN_DECIMAL_ODDS <= odds <= MAX_DECIMAL_ODDS
