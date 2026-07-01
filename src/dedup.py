"""Alert deduplication logic.

Pure functions — no I/O, fully unit-testable.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from src.models import ArbOpportunity


def make_dedup_key(opp: ArbOpportunity) -> str:
    """Deterministic fingerprint for an arb opportunity.

    Two opportunities with the same event, market, line, and
    outcome/bookmaker combination produce the same key regardless
    of detection order or margin.
    """
    line_norm = abs(opp.point) if opp.point is not None else ""
    legs = sorted(
        (b.outcome_name, b.bookmaker_key) for b in opp.best_outcomes
    )
    raw = f"{opp.event_id}|{opp.market_key}|{line_norm}|{legs}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _books_changed(opp: ArbOpportunity, last_alert: dict[str, Any]) -> bool:
    """True if the set of bookmakers contributing legs has changed."""
    current_books = frozenset(b.bookmaker_key for b in opp.best_outcomes)
    prev_key = last_alert.get("dedup_key", "")
    prev_books_raw = last_alert.get("books")
    if prev_books_raw and isinstance(prev_books_raw, (list, set, frozenset)):
        return current_books != frozenset(prev_books_raw)
    return False


def should_alert(
    opp: ArbOpportunity,
    last_alert: dict[str, Any] | None,
    *,
    cooldown_seconds: int = 600,
    min_margin_improvement: float = 0.005,
) -> tuple[bool, str | None]:
    """Decide whether an alert should be sent.

    Returns (should_send, skip_reason).  ``skip_reason`` is ``None``
    when the alert should fire.
    """
    if last_alert is None:
        return True, None

    sent_at = last_alert["sent_at"]
    if isinstance(sent_at, str):
        sent_at = datetime.fromisoformat(sent_at)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)

    elapsed = (datetime.now(UTC) - sent_at).total_seconds()

    if elapsed >= cooldown_seconds:
        return True, None

    margin_improvement = opp.margin - last_alert["margin"]
    if margin_improvement >= min_margin_improvement:
        return True, None

    if _books_changed(opp, last_alert):
        return True, None

    return False, "cooldown"
