"""Tests for parser replay: snapshot → parse → filter → arbs roundtrip."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.arb import filter_stale_odds, find_arbs
from src.odds_client import OddsClient
from tests.fixtures.api_snapshots import (
    NBA_TWO_WAY_ARB,
    MULTI_EVENT_SNAPSHOT,
    SOCCER_THREE_WAY,
    STALE_ODDS_SNAPSHOT,
)


def _replay(raw: list[dict], min_margin: float = 0.01, max_age: int = 3600):
    events = OddsClient.parse_events(raw)
    if max_age > 0:
        events = filter_stale_odds(events, max_age)
    return find_arbs(events, min_margin=min_margin, total_stake=100.0)


class TestReplayFromFixtures:
    def test_nba_arb_replays(self):
        opps = _replay(NBA_TWO_WAY_ARB)
        assert len(opps) >= 1
        assert opps[0].home_team == "Los Angeles Lakers"

    def test_multi_event_replays(self):
        opps = _replay(MULTI_EVENT_SNAPSHOT)
        assert len(opps) == 1

    def test_soccer_three_way_replays(self):
        opps = _replay(SOCCER_THREE_WAY)
        assert len(opps) == 1
        assert opps[0].outcome_count == 3

    def test_stale_snapshot_filtered(self):
        opps_raw = _replay(STALE_ODDS_SNAPSHOT, min_margin=0.0, max_age=0)
        opps_filtered = _replay(STALE_ODDS_SNAPSHOT, min_margin=0.01, max_age=300)
        assert len(opps_raw) >= 1
        assert len(opps_filtered) == 0


class TestReplayFromFile:
    def test_file_round_trip(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(NBA_TWO_WAY_ARB, f)
            path = f.name

        data = json.loads(Path(path).read_text())
        opps = _replay(data)
        assert len(opps) >= 1
        Path(path).unlink()

    def test_wrapped_format(self):
        """Replay also works when the file wraps the data in a dict."""
        wrapped = {"response_json": NBA_TWO_WAY_ARB, "source_key": "odds_api"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(wrapped, f)
            path = f.name

        data = json.loads(Path(path).read_text())
        raw = data["response_json"] if isinstance(data, dict) and "response_json" in data else data
        opps = _replay(raw)
        assert len(opps) >= 1
        Path(path).unlink()


class TestReplayStakeMath:
    @pytest.mark.parametrize("snapshot", [NBA_TWO_WAY_ARB, SOCCER_THREE_WAY, MULTI_EVENT_SNAPSHOT])
    def test_replayed_profits_positive(self, snapshot):
        opps = _replay(snapshot)
        for opp in opps:
            assert opp.guaranteed_profit > 0
            assert sum(opp.stakes) == pytest.approx(100.0, rel=1e-9)
