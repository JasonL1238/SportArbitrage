from __future__ import annotations


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
