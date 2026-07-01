"""Cross-source event matching.

Given events from different sources, the matcher scores them for identity
and decides whether they refer to the same real-world contest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.canonical import canonical_team, token_similarity

log = logging.getLogger(__name__)

AUTO_MERGE_THRESHOLD = 0.95
TENTATIVE_MERGE_THRESHOLD = 0.85


@dataclass(frozen=True)
class EventIdentity:
    """Minimal event descriptor for matching purposes."""

    source_key: str
    source_event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    market_keys: frozenset[str] = frozenset()
    participant_count: int = 2


@dataclass
class MatchScore:
    """Detailed scoring breakdown for one candidate match."""

    event_a: EventIdentity
    event_b: EventIdentity
    score: float
    sport_match: bool
    time_delta_seconds: float
    home_similarity: float
    away_similarity: float
    market_compatible: bool
    participant_compatible: bool

    @property
    def auto_merge(self) -> bool:
        return self.score >= AUTO_MERGE_THRESHOLD

    @property
    def tentative_merge(self) -> bool:
        return TENTATIVE_MERGE_THRESHOLD <= self.score < AUTO_MERGE_THRESHOLD


def score_match(a: EventIdentity, b: EventIdentity) -> MatchScore:
    """Score how likely two event identities refer to the same contest.

    Returns a MatchScore with an overall score in [0, 1].
    """
    sport_match = a.sport_key == b.sport_key
    if not sport_match:
        return MatchScore(
            event_a=a, event_b=b, score=0.0, sport_match=False,
            time_delta_seconds=0, home_similarity=0, away_similarity=0,
            market_compatible=False, participant_compatible=False,
        )

    time_delta = abs((a.commence_time - b.commence_time).total_seconds())

    home_a = canonical_team(a.home_team, a.sport_key)
    home_b = canonical_team(b.home_team, b.sport_key)
    away_a = canonical_team(a.away_team, a.sport_key)
    away_b = canonical_team(b.away_team, b.sport_key)

    home_sim = 1.0 if home_a == home_b else token_similarity(a.home_team, b.home_team)
    away_sim = 1.0 if away_a == away_b else token_similarity(a.away_team, b.away_team)

    home_cross = 1.0 if home_a == away_b else token_similarity(a.home_team, b.away_team)
    away_cross = 1.0 if away_a == home_b else token_similarity(a.away_team, b.home_team)
    if (home_cross + away_cross) > (home_sim + away_sim):
        home_sim, away_sim = home_cross, away_cross

    market_compat = bool(a.market_keys & b.market_keys) if a.market_keys and b.market_keys else True
    participant_compat = a.participant_count == b.participant_count

    time_score = max(0.0, 1.0 - time_delta / 7200)

    score = (
        0.35 * home_sim
        + 0.35 * away_sim
        + 0.20 * time_score
        + 0.05 * (1.0 if market_compat else 0.0)
        + 0.05 * (1.0 if participant_compat else 0.0)
    )

    return MatchScore(
        event_a=a,
        event_b=b,
        score=score,
        sport_match=sport_match,
        time_delta_seconds=time_delta,
        home_similarity=home_sim,
        away_similarity=away_sim,
        market_compatible=market_compat,
        participant_compatible=participant_compat,
    )


def find_best_match(
    target: EventIdentity,
    candidates: list[EventIdentity],
) -> MatchScore | None:
    """Find the highest-scoring candidate for *target*, or None if none qualify."""
    best: MatchScore | None = None
    for candidate in candidates:
        if candidate.source_key == target.source_key and candidate.source_event_id == target.source_event_id:
            continue
        ms = score_match(target, candidate)
        if best is None or ms.score > best.score:
            best = ms
    return best
