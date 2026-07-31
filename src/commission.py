"""What each venue takes out of a winning bet, and what that does to the price.

A sportsbook's charge is already inside the price it publishes: -110 both ways
*is* the vig.  An exchange's is not.  Matchbook and Smarkets quote you a price
and then take a percentage of your net winnings; Kalshi and Polymarket charge a
fee per contract that depends on the price you paid.  On those venues the number
on the screen is not the number you get paid, and the gap is the same size as
the edge being looked for.

That matters more here than anywhere else in the pipeline, because exchanges
quote *tighter* than sportsbooks — a 102% back-side overround against a
sportsbook's 105% — so an exchange leg is exactly what makes a cross-book sum
drop below 1.0.  Ignoring commission would therefore not produce a scattering of
small errors; it would produce a stream of false positives concentrated on the
new sources, all of them looking like the best opportunities in the report.

Every model here answers one question: **given the quoted decimal odds, what
decimal odds do you actually receive?**  That single number is what arbitrage
arithmetic needs, and expressing it that way keeps the rest of the engine unaware
of which venue charges what.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: Below this the maths stops being meaningful — a "price" of 1.0 pays nothing.
_MIN_DECIMAL = 1.0 + 1e-12


@dataclass(frozen=True)
class Commission:
    """Base model: the venue takes nothing.  Sportsbooks are already priced net."""

    label: str = "none"

    @property
    def is_free(self) -> bool:
        return True

    def net_decimal(self, decimal_odds: float) -> float:
        """The decimal odds actually received, after the venue's charge."""
        if decimal_odds <= _MIN_DECIMAL:
            raise ValueError(f"decimal odds must exceed 1.0, got {decimal_odds}")
        return decimal_odds

    def describe(self) -> str:
        return "no commission (the venue's margin is already in the price)"


@dataclass(frozen=True)
class WinningsCommission(Commission):
    """A percentage of *net winnings*, the classic betting-exchange charge.

    Stake is returned in full and the charge applies only to the profit, so a
    quoted 3.00 at 2% pays ``1 + 2.00 × 0.98 = 2.96``.  Applying the rate to the
    whole return instead would overstate the charge by the stake, which on a
    short price is most of it.
    """

    rate: float = 0.0

    @property
    def is_free(self) -> bool:
        return self.rate <= 0.0

    def net_decimal(self, decimal_odds: float) -> float:
        if decimal_odds <= _MIN_DECIMAL:
            raise ValueError(f"decimal odds must exceed 1.0, got {decimal_odds}")
        return 1.0 + (decimal_odds - 1.0) * (1.0 - self.rate)

    def describe(self) -> str:
        return f"{self.rate * 100:.2f}% of net winnings"


@dataclass(frozen=True)
class ContractFeeCommission(Commission):
    """A fee per contract, charged up front, as prediction markets do.

    A binary contract costs its price ``p`` and pays 1.  The venue adds a fee on
    top of ``p``, so the effective cost is ``p + fee(p)`` and the effective
    decimal odds are ``1 / (p + fee(p))``.  Two shapes are in use:

    * ``"p_times_q"`` — ``rate × p × (1 - p)``.  Kalshi's published schedule,
      maximal at even money and vanishing at the extremes.
    * ``"min_p_q"`` — ``rate × min(p, 1 - p)``.  Polymarket's, whose own market
      payloads state ``{"rate": 0.05, "exponent": 1, "takerOnly": true}``.

    Charged on entry rather than on winnings, which is why this cannot be
    expressed as a :class:`WinningsCommission` with some equivalent rate: the fee
    is paid whether or not the contract settles in your favour.
    """

    rate: float = 0.0
    shape: str = "p_times_q"

    @property
    def is_free(self) -> bool:
        return self.rate <= 0.0

    def _fee(self, price: float) -> float:
        if self.shape == "p_times_q":
            return self.rate * price * (1.0 - price)
        if self.shape == "min_p_q":
            return self.rate * min(price, 1.0 - price)
        raise ValueError(f"unknown contract fee shape {self.shape!r}")

    def net_decimal(self, decimal_odds: float) -> float:
        if decimal_odds <= _MIN_DECIMAL:
            raise ValueError(f"decimal odds must exceed 1.0, got {decimal_odds}")
        price = 1.0 / decimal_odds
        cost = price + self._fee(price)
        if cost >= 1.0:
            # The fee has eaten the entire payout.  Reported as a price that
            # cannot win rather than as a negative one, so callers refuse it
            # instead of treating a nonsense number as an edge.
            return _MIN_DECIMAL
        return 1.0 / cost

    def describe(self) -> str:
        shape = "p×(1−p)" if self.shape == "p_times_q" else "min(p, 1−p)"
        return f"{self.rate:.3g} × {shape} per contract, charged on entry"


NO_COMMISSION = Commission()

#: Per-source charges.  Keyed by ``source_key`` so that :mod:`src.arb` can price
#: a position without importing a single adapter — the registry validates that
#: every registered source appears here, so a new venue cannot be added with its
#: commission silently defaulting to zero.
#:
#: The rates are the venues' **published standard** rates, and each is the least
#: favourable reading available:
#:
#: * A Matchbook or Smarkets account can earn a lower rate through volume; using
#:   the standard rate therefore understates the edge rather than inventing one.
#: * SX Bet's charge is the oracle fee its own ``/metadata`` reports as
#:   ``5 × 10^18`` against a ``10^20`` scale.  That reading is not stated in
#:   words anywhere in the payload, so it is taken as 5% of net winnings — the
#:   most expensive plausible interpretation, again biased toward refusing a
#:   marginal position rather than reporting one.
#:
#: Getting a rate wrong in the *generous* direction manufactures arbitrage;
#: getting it wrong in the strict direction only loses one.  Every uncertainty
#: here is resolved in the second direction on purpose.
COMMISSIONS: dict[str, Commission] = {
    # ── sportsbooks: the margin is already in the quoted price ────────────────
    "fanduel": NO_COMMISSION,
    "pinnacle": NO_COMMISSION,
    "betrivers_kambi": NO_COMMISSION,
    "leovegas_kambi": NO_COMMISSION,
    "bovada": NO_COMMISSION,
    "betmgm": NO_COMMISSION,
    "cloudbet": NO_COMMISSION,
    "onexbet": NO_COMMISSION,
    "an_draftkings": NO_COMMISSION,
    "an_caesars": NO_COMMISSION,
    "an_bet365": NO_COMMISSION,
    "an_fanduel": NO_COMMISSION,
    "an_betrivers": NO_COMMISSION,
    "an_betmgm": NO_COMMISSION,
    "an_bovada": NO_COMMISSION,
    "an_onexbet": NO_COMMISSION,
    "unibet_au": NO_COMMISSION,
    # ── betting exchanges: a share of net winnings ────────────────────────────
    # CHECK AGAINST YOUR OWN ACCOUNT before staking a Matchbook leg.  Matchbook
    # has changed its standard commission more than once (it has been both above
    # and below this number in different periods and jurisdictions), and this
    # module cannot read the current schedule.  If the account's real rate is
    # *higher* than what is encoded here, every Matchbook leg's net odds are
    # overstated — the one direction the doctrine above forbids — and Matchbook
    # legs appear in most cross-book candidates on a typical slate.
    "matchbook": WinningsCommission(label="matchbook", rate=0.02),
    "smarkets": WinningsCommission(label="smarkets", rate=0.02),
    "sxbet": WinningsCommission(label="sxbet", rate=0.05),
    # ── prediction markets: a fee per contract, paid on entry ─────────────────
    "kalshi": ContractFeeCommission(label="kalshi", rate=0.07, shape="p_times_q"),
    "polymarket": ContractFeeCommission(label="polymarket", rate=0.05, shape="min_p_q"),
}


def commission_for(source_key: str, table: Mapping[str, Commission] | None = None) -> Commission:
    """The charge for one source, defaulting to none for an unregistered one.

    An unknown source getting :data:`NO_COMMISSION` is the right default *here*
    because this function is also used on historical rows whose source has since
    been removed; the guarantee that a **live** source cannot be added without a
    declared charge is enforced in :mod:`src.sources.registry`, where adding one
    is the thing that happens.
    """
    return (table if table is not None else COMMISSIONS).get(source_key, NO_COMMISSION)


def net_decimal_odds(
    source_key: str, decimal_odds: float, table: Mapping[str, Commission] | None = None
) -> float:
    return commission_for(source_key, table).net_decimal(decimal_odds)
