"""Tests for the ESPN public odds adapter using replay fixtures."""
from __future__ import annotations

import pytest

from src.arb import find_arbs
from src.sources.base import SourceAdapter
from src.sources.espn_odds import EspnOddsAdapter, _parse_espn_odds
from tests.fixtures.espn_snapshots import (
    ESPN_MLB_ODDS_SNAPSHOT,
    ESPN_MULTI_PROVIDER_SNAPSHOT,
    ESPN_NBA_ODDS_SNAPSHOT,
    ESPN_NO_ODDS_SNAPSHOT,
)


class TestEspnProtocolConformance:
    def test_satisfies_source_adapter(self):
        adapter = EspnOddsAdapter()
        assert isinstance(adapter, SourceAdapter)
        adapter.close()

    def test_source_key(self):
        adapter = EspnOddsAdapter()
        assert adapter.source_key == "espn_odds"
        adapter.close()

    def test_supports_delta_false(self):
        adapter = EspnOddsAdapter()
        assert adapter.supports_delta() is False
        adapter.close()

    def test_confidence_score(self):
        adapter = EspnOddsAdapter()
        assert adapter.confidence_score() == 0.70
        adapter.close()

    def test_healthcheck_initially_healthy(self):
        adapter = EspnOddsAdapter()
        assert adapter.healthcheck().is_healthy is True
        adapter.close()


class TestEspnNbaParser:
    def test_parses_single_event(self):
        events = _parse_espn_odds(ESPN_NBA_ODDS_SNAPSHOT)
        assert len(events) == 1
        event = events[0]
        assert event.home_team == "Los Angeles Lakers"
        assert event.away_team == "Boston Celtics"
        assert event.sport_key == "basketball_nba"

    def test_has_three_markets(self):
        events = _parse_espn_odds(ESPN_NBA_ODDS_SNAPSHOT)
        bookmaker = events[0].bookmakers[0]
        market_keys = {m.key for m in bookmaker.markets}
        assert market_keys == {"h2h", "spreads", "totals"}

    def test_moneyline_outcomes(self):
        events = _parse_espn_odds(ESPN_NBA_ODDS_SNAPSHOT)
        h2h = next(m for m in events[0].bookmakers[0].markets if m.key == "h2h")
        assert len(h2h.outcomes) == 2
        home = next(o for o in h2h.outcomes if o.name == "Los Angeles Lakers")
        away = next(o for o in h2h.outcomes if o.name == "Boston Celtics")
        assert home.price == pytest.approx(1 + 100 / 150, rel=1e-4)
        assert away.price == pytest.approx(1 + 130 / 100, rel=1e-4)

    def test_spread_outcomes(self):
        events = _parse_espn_odds(ESPN_NBA_ODDS_SNAPSHOT)
        spread = next(m for m in events[0].bookmakers[0].markets if m.key == "spreads")
        assert len(spread.outcomes) == 2
        home = next(o for o in spread.outcomes if o.name == "Los Angeles Lakers")
        assert home.point == -3.5

    def test_totals_outcomes(self):
        events = _parse_espn_odds(ESPN_NBA_ODDS_SNAPSHOT)
        totals = next(m for m in events[0].bookmakers[0].markets if m.key == "totals")
        assert len(totals.outcomes) == 2
        over = next(o for o in totals.outcomes if o.name == "Over")
        assert over.point == 215.5

    def test_bookmaker_key(self):
        events = _parse_espn_odds(ESPN_NBA_ODDS_SNAPSHOT)
        assert events[0].bookmakers[0].key == "draftkings"
        assert events[0].bookmakers[0].title == "DraftKings"


class TestEspnMlbParser:
    def test_parses_mlb_event(self):
        events = _parse_espn_odds(ESPN_MLB_ODDS_SNAPSHOT)
        assert len(events) == 1
        assert events[0].home_team == "New York Yankees"
        assert events[0].away_team == "Boston Red Sox"
        assert events[0].sport_key == "baseball_mlb"

    def test_run_line(self):
        events = _parse_espn_odds(ESPN_MLB_ODDS_SNAPSHOT)
        spread = next(m for m in events[0].bookmakers[0].markets if m.key == "spreads")
        home = next(o for o in spread.outcomes if o.name == "New York Yankees")
        assert home.point == -1.5


class TestEspnEdgeCases:
    def test_no_odds_produces_no_bookmakers(self):
        events = _parse_espn_odds(ESPN_NO_ODDS_SNAPSHOT)
        assert len(events) == 1
        assert len(events[0].bookmakers) == 0

    def test_empty_input(self):
        events = _parse_espn_odds([])
        assert events == []

    def test_adapter_supplied_sport_key_is_preserved(self):
        raw = [dict(ESPN_MLB_ODDS_SNAPSHOT[0], _sport_key="baseball_mlb")]
        events = _parse_espn_odds(raw)
        assert events[0].sport_key == "baseball_mlb"


class TestEspnMultiProvider:
    def test_parses_two_providers(self):
        events = _parse_espn_odds(ESPN_MULTI_PROVIDER_SNAPSHOT)
        assert len(events) == 1
        assert len(events[0].bookmakers) == 2
        keys = {b.key for b in events[0].bookmakers}
        assert "draftkings" in keys
        assert "espn_bet" in keys

    def test_arb_detection_on_multi_provider(self):
        """Cross-book arbs should be detectable from ESPN multi-provider data."""
        events = _parse_espn_odds(ESPN_MULTI_PROVIDER_SNAPSHOT)
        arbs = find_arbs(events, min_margin=-1.0)
        assert len(arbs) >= 1
        for opp in arbs:
            assert opp.outcome_count >= 2


class TestEspnAdapterViaInterface:
    def test_parse_events_method(self):
        adapter = EspnOddsAdapter()
        events = adapter.parse_events(ESPN_NBA_ODDS_SNAPSHOT)
        assert len(events) == 1
        assert events[0].home_team == "Los Angeles Lakers"
        adapter.close()

    def test_context_manager(self):
        with EspnOddsAdapter() as adapter:
            events = adapter.parse_events(ESPN_NBA_ODDS_SNAPSHOT)
            assert len(events) == 1
