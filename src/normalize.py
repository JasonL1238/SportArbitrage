from __future__ import annotations


def american_to_decimal(odds: int | float) -> float:
    """Convert American odds to decimal odds.

    +150 → 2.50  (risk 100 to win 150, total return 250)
    -110 → 1.909  (risk 110 to win 100, total return 209.09)
    +100 → 2.00  (even money)
    """
    odds = float(odds)
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    if odds > 0:
        return 1 + odds / 100
    return 1 + 100 / abs(odds)


def decimal_to_american(odds: float) -> int:
    """Convert decimal odds to American odds."""
    if odds < 1.0:
        raise ValueError(f"Decimal odds must be >= 1.0, got {odds}")
    if odds == 1.0:
        raise ValueError("Decimal odds of 1.0 imply zero payout")
    if odds >= 2.0:
        return round((odds - 1) * 100)
    return round(-100 / (odds - 1))


def implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability (0–1)."""
    if decimal_odds <= 0:
        raise ValueError(f"Decimal odds must be positive, got {decimal_odds}")
    return 1 / decimal_odds


def ensure_decimal(odds: float, fmt: str = "decimal") -> float:
    """Normalize odds to decimal format.

    If *fmt* is ``"american"``, convert; otherwise pass through.
    """
    if fmt == "american":
        return american_to_decimal(odds)
    return odds
