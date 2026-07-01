from __future__ import annotations


def apply_costs_to_decimal_odds(
    decimal_odds: float,
    *,
    fee_rate: float = 0.0,
    slippage_bps: float = 0.0,
) -> float:
    """Return effective decimal odds after commission and slippage.

    ``fee_rate`` is applied to net winnings only, which is how many exchanges
    express commission. ``slippage_bps`` conservatively reduces the net odds.
    """
    if decimal_odds <= 1.0:
        raise ValueError("Decimal odds must be greater than 1.0")
    if fee_rate < 0 or slippage_bps < 0:
        raise ValueError("Fee and slippage inputs cannot be negative")

    net = decimal_odds - 1.0
    net_after_fee = net * (1.0 - fee_rate)
    net_after_slippage = net_after_fee * (1.0 - slippage_bps / 10_000)
    return 1.0 + net_after_slippage


def stake_split(decimal_odds: list[float], total_stake: float) -> list[float]:
    """Calculate how much to wager on each outcome so that every outcome
    returns the same payout (the hallmark of a risk-free arb).

    Returns a list of stake amounts summing to *total_stake*.
    """
    if not decimal_odds or any(o <= 0 for o in decimal_odds):
        raise ValueError("All decimal odds must be positive")
    inverse_sum = sum(1 / o for o in decimal_odds)
    return [(total_stake / o) / inverse_sum for o in decimal_odds]


def guaranteed_profit(
    stakes: list[float], decimal_odds: list[float], total_stake: float
) -> float:
    """The minimum payout across outcomes minus the total stake.

    Positive means guaranteed profit (true arb).
    """
    payouts = [s * o for s, o in zip(stakes, decimal_odds)]
    return min(payouts) - total_stake


def max_stake_from_liquidity(stakes: list[float], liquidities: list[float | None]) -> float | None:
    """Scale the total stake to the tightest leg liquidity, if known."""
    caps = [
        liquidity / stake
        for stake, liquidity in zip(stakes, liquidities)
        if liquidity is not None and stake > 0
    ]
    if not caps:
        return None
    scale = min(caps)
    return sum(stakes) * min(scale, 1.0)
