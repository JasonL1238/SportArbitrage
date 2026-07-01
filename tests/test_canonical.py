"""Tests for canonical team-name, market, and event-matching logic."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.canonical import canonical_market, canonical_team, normalize_name, token_similarity
from src.matcher import (
    AUTO_MERGE_THRESHOLD,
    TENTATIVE_MERGE_THRESHOLD,
    EventIdentity,
    MatchScore,
    find_best_match,
    score_match,
)


class TestNormalizeName:
    def test_lowercase_and_strip(self):
        assert normalize_name("  Los Angeles Lakers  ") == "los angeles lakers"

    def test_removes_diacritics(self):
        assert normalize_name("José") == "jose"

    def test_collapses_whitespace(self):
        assert normalize_name("New   York   Knicks") == "new york knicks"

    def test_removes_punctuation(self):
        assert normalize_name("St. Louis") == "st louis"

    def test_empty_string(self):
        assert normalize_name("") == ""


class TestCanonicalTeam:
    def test_full_name_nba(self):
        assert canonical_team("Los Angeles Lakers", "basketball_nba") == "los_angeles_lakers"

    def test_short_name_nba(self):
        assert canonical_team("Lakers", "basketball_nba") == "los_angeles_lakers"

    def test_mixed_case(self):
        assert canonical_team("CELTICS", "basketball_nba") == "boston_celtics"

    def test_mlb_alias(self):
        assert canonical_team("NY Yankees", "baseball_mlb") == "new_york_yankees"

    def test_nfl_alias(self):
        assert canonical_team("Chiefs", "americanfootball_nfl") == "kansas_city_chiefs"

    def test_nhl_alias(self):
        assert canonical_team("Rangers", "icehockey_nhl") == "new_york_rangers"

    def test_cross_sport_lookup(self):
        assert canonical_team("Lakers") == "los_angeles_lakers"

    def test_unknown_team_falls_back(self):
        result = canonical_team("Unknown Team XYZ", "basketball_nba")
        assert result == "unknown_team_xyz"

    def test_76ers_alias(self):
        assert canonical_team("76ers", "basketball_nba") == "philadelphia_76ers"

    def test_49ers_alias(self):
        assert canonical_team("49ers", "americanfootball_nfl") == "san_francisco_49ers"


class TestCanonicalMarket:
    def test_h2h(self):
        assert canonical_market("h2h") == "moneyline"

    def test_moneyline(self):
        assert canonical_market("moneyline") == "moneyline"

    def test_spreads(self):
        assert canonical_market("spreads") == "spread"

    def test_run_line(self):
        assert canonical_market("run_line") == "spread"

    def test_totals(self):
        assert canonical_market("totals") == "totals"

    def test_over_under(self):
        assert canonical_market("over_under") == "totals"

    def test_unknown_passthrough(self):
        assert canonical_market("player_props") == "player_props"

    def test_case_insensitive(self):
        assert canonical_market("H2H") == "moneyline"


class TestTokenSimilarity:
    def test_identical(self):
        assert token_similarity("Los Angeles Lakers", "Los Angeles Lakers") == 1.0

    def test_subset(self):
        sim = token_similarity("Lakers", "Los Angeles Lakers")
        assert 0.0 < sim < 1.0

    def test_completely_different(self):
        assert token_similarity("Yankees", "Lakers") == 0.0

    def test_empty(self):
        assert token_similarity("", "Lakers") == 0.0


class TestScoreMatch:
    def _identity(self, source: str, eid: str, home: str, away: str,
                  commence: datetime | None = None, sport: str = "basketball_nba"):
        return EventIdentity(
            source_key=source,
            source_event_id=eid,
            sport_key=sport,
            home_team=home,
            away_team=away,
            commence_time=commence or datetime.now(UTC) + timedelta(hours=2),
            market_keys=frozenset(["h2h"]),
        )

    def test_identical_events_auto_merge(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        a = self._identity("src_a", "e1", "Los Angeles Lakers", "Boston Celtics", now)
        b = self._identity("src_b", "e2", "Los Angeles Lakers", "Boston Celtics", now)
        ms = score_match(a, b)
        assert ms.auto_merge is True
        assert ms.score >= AUTO_MERGE_THRESHOLD

    def test_alias_match_auto_merges(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        a = self._identity("src_a", "e1", "Lakers", "Celtics", now)
        b = self._identity("src_b", "e2", "Los Angeles Lakers", "Boston Celtics", now)
        ms = score_match(a, b)
        assert ms.score >= AUTO_MERGE_THRESHOLD

    def test_different_sports_zero(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        a = self._identity("src_a", "e1", "Lakers", "Celtics", now, sport="basketball_nba")
        b = self._identity("src_b", "e2", "Lakers", "Celtics", now, sport="baseball_mlb")
        ms = score_match(a, b)
        assert ms.score == 0.0
        assert ms.sport_match is False

    def test_large_time_gap_reduces_score(self):
        now = datetime.now(UTC)
        a = self._identity("src_a", "e1", "Lakers", "Celtics", now)
        b = self._identity("src_b", "e2", "Lakers", "Celtics", now + timedelta(hours=3))
        ms = score_match(a, b)
        assert ms.score < AUTO_MERGE_THRESHOLD

    def test_completely_different_teams_low_score(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        a = self._identity("src_a", "e1", "Lakers", "Celtics", now)
        b = self._identity("src_b", "e2", "Yankees", "Red Sox", now)
        ms = score_match(a, b)
        assert ms.score < TENTATIVE_MERGE_THRESHOLD

    def test_swapped_home_away_still_matches(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        a = self._identity("src_a", "e1", "Lakers", "Celtics", now)
        b = self._identity("src_b", "e2", "Celtics", "Lakers", now)
        ms = score_match(a, b)
        assert ms.score >= AUTO_MERGE_THRESHOLD


class TestFindBestMatch:
    def _identity(self, source: str, eid: str, home: str, away: str,
                  commence: datetime | None = None):
        return EventIdentity(
            source_key=source,
            source_event_id=eid,
            sport_key="basketball_nba",
            home_team=home,
            away_team=away,
            commence_time=commence or datetime.now(UTC) + timedelta(hours=2),
        )

    def test_finds_best_among_candidates(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        target = self._identity("src_a", "e1", "Lakers", "Celtics", now)
        candidates = [
            self._identity("src_b", "e10", "Yankees", "Red Sox", now),
            self._identity("src_b", "e11", "Los Angeles Lakers", "Boston Celtics", now),
            self._identity("src_b", "e12", "Knicks", "Heat", now),
        ]
        best = find_best_match(target, candidates)
        assert best is not None
        assert best.event_b.source_event_id == "e11"
        assert best.auto_merge is True

    def test_no_good_match_returns_low_score(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        target = self._identity("src_a", "e1", "Lakers", "Celtics", now)
        candidates = [
            self._identity("src_b", "e10", "Yankees", "Red Sox", now),
            self._identity("src_b", "e11", "Cubs", "Mets", now),
        ]
        best = find_best_match(target, candidates)
        assert best is not None
        assert best.auto_merge is False
        assert best.tentative_merge is False

    def test_skips_same_source_same_id(self):
        now = datetime.now(UTC) + timedelta(hours=2)
        target = self._identity("src_a", "e1", "Lakers", "Celtics", now)
        candidates = [
            self._identity("src_a", "e1", "Lakers", "Celtics", now),
        ]
        best = find_best_match(target, candidates)
        assert best is None

    def test_empty_candidates(self):
        target = self._identity("src_a", "e1", "Lakers", "Celtics")
        assert find_best_match(target, []) is None
