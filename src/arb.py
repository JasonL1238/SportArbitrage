from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from src.models import ArbOpportunity, BestOutcome, BookmakerMarket, Event
from src.stake import guaranteed_profit, stake_split

log = logging.getLogger(__name__)

EXPECTED_OUTCOME_COUNTS: dict[str, dict[str, int]] = {
    "h2h": {"default": 2, "soccer": 3},
    "spreads": {"default": 2},
    "totals": {"default": 2},
}


def filter_started_events(events: list[Event]) -> list[Event]:
    """Remove events whose commence_time is in the past."""
    now = datetime.now(UTC)
    filtered = [e for e in events if e.commence_time > now]
    dropped = len(events) - len(filtered)
    if dropped:
        log.info("Filtered %d started/live events", dropped)
    return filtered


def filter_stale_odds(events: list[Event], max_age_seconds: int) -> list[Event]:
    """Remove bookmaker markets whose last_update is older than *max_age_seconds*."""
    cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
    filtered: list[Event] = []
    dropped = 0
    for event in events:
        fresh_bookmakers = []
        for bm in event.bookmakers:
            fresh_markets = [m for m in bm.markets if m.last_update >= cutoff]
            dropped += len(bm.markets) - len(fresh_markets)
            if fresh_markets:
                fresh_bookmakers.append(bm.model_copy(update={"markets": fresh_markets}))
        filtered.append(event.model_copy(update={"bookmakers": fresh_bookmakers}))
    if dropped:
        log.info("Filtered %d stale market entries (older than %ds)", dropped, max_age_seconds)
    return filtered


def _expected_outcomes(market_key: str, sport_key: str) -> int | None:
    """How many outcomes a complete market should have, or None if unknown."""
    counts = EXPECTED_OUTCOME_COUNTS.get(market_key)
    if counts is None:
        return None
    if sport_key.startswith("soccer") and "soccer" in counts:
        return counts["soccer"]
    return counts["default"]


def _has_distinct_books(best_outcomes: list[BestOutcome]) -> bool:
    """True if at least two distinct bookmakers contribute legs."""
    return len({b.bookmaker_key for b in best_outcomes}) >= 2


def arb_margin(decimal_odds: list[float]) -> float:
    """Return the arbitrage margin for a set of decimal odds.

    Positive value means an arb exists.  For example, if two outcomes have
    best decimal odds [2.20, 2.10], the inverse sum is 1/2.20 + 1/2.10 ≈ 0.931,
    and the margin is 1 − 0.931 ≈ 0.069 (6.9%).
    """
    if not decimal_odds or any(o <= 0 for o in decimal_odds):
        return -1.0
    inverse_sum = sum(1 / o for o in decimal_odds)
    return 1 - inverse_sum


GroupKey = tuple[str, str, str, Optional[float]]  # (sport_key, event_id, market_key, point)


def _group_key_for_outcome(
    sport_key: str, event_id: str, market_key: str, point: Optional[float]
) -> GroupKey:
    # Spreads have opposite points per side (e.g. -3.5 / +3.5).
    # Normalize to abs so both sides land in the same group.
    if market_key == "spreads" and point is not None:
        point = abs(point)
    return (sport_key, event_id, market_key, point)


def find_arbs(
    events: list[Event],
    min_margin: float = 0.01,
    total_stake: float = 100.0,
    *,
    require_distinct_books: bool = False,
    require_complete_outcomes: bool = False,
) -> list[ArbOpportunity]:
    """Scan a list of events and return all arbitrage opportunities above *min_margin*.

    When *require_distinct_books* is True, opportunities where every leg comes
    from the same bookmaker are discarded (same-book theoretical arbs are
    rarely executable).

    When *require_complete_outcomes* is True, market groups that don't have
    the expected number of outcomes for their market type are skipped.
    """
    opportunities: list[ArbOpportunity] = []

    for event in events:
        market_groups: dict[GroupKey, dict[str, BestOutcome]] = {}

        for bookmaker in event.bookmakers:
            for market in bookmaker.markets:
                for outcome in market.outcomes:
                    gk = _group_key_for_outcome(
                        event.sport_key, event.id, market.key, outcome.point
                    )
                    if gk not in market_groups:
                        market_groups[gk] = {}

                    current_best = market_groups[gk].get(outcome.name)
                    if current_best is None or outcome.price > current_best.decimal_odds:
                        market_groups[gk][outcome.name] = BestOutcome(
                            outcome_name=outcome.name,
                            bookmaker_key=bookmaker.key,
                            bookmaker_title=bookmaker.title,
                            decimal_odds=outcome.price,
                            point=outcome.point,
                        )

        for gk, best_by_outcome in market_groups.items():
            best_list = list(best_by_outcome.values())

            if require_complete_outcomes:
                expected = _expected_outcomes(gk[2], gk[0])
                if expected is not None and len(best_list) != expected:
                    continue

            odds = [b.decimal_odds for b in best_list]
            margin = arb_margin(odds)

            if margin >= min_margin:
                if require_distinct_books and not _has_distinct_books(best_list):
                    continue

                stakes = stake_split(odds, total_stake)
                profit = guaranteed_profit(stakes, odds, total_stake)
                inverse_sum = sum(1 / o for o in odds)

                opportunities.append(
                    ArbOpportunity(
                        sport_key=gk[0],
                        event_id=gk[1],
                        home_team=event.home_team,
                        away_team=event.away_team,
                        commence_time=event.commence_time,
                        market_key=gk[2],
                        point=gk[3],
                        outcome_count=len(best_list),
                        margin=margin,
                        implied_prob_sum=inverse_sum,
                        best_outcomes=best_list,
                        stakes=stakes,
                        guaranteed_profit=profit,
                        total_stake=total_stake,
                        detected_at=datetime.now(UTC),
                    )
                )

    return opportunities
