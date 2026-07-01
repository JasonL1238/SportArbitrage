"""Tests for source adapter protocol, OddsApiAdapter, and SourceRegistry."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.models import Event
from src.sources.base import SourceAdapter, SourceHealth, SourceSnapshot
from src.sources.odds_api import OddsApiAdapter
from src.sources.registry import SourceRegistry
from tests.fixtures.api_snapshots import NBA_TWO_WAY_ARB


class _StubAdapter:
    """Minimal adapter for protocol conformance tests."""

    def __init__(self, key: str = "stub", healthy: bool = True) -> None:
        self._key = key
        self._healthy = healthy

    @property
    def source_key(self) -> str:
        return self._key

    def discover_events(self, sport_key: str) -> list[dict[str, Any]]:
        return []

    def fetch_odds(self, sport_key: str) -> SourceSnapshot:
        return SourceSnapshot(
            source_key=self._key,
            sport_key=sport_key,
            raw_payload="[]",
            fetched_at=datetime.now(UTC),
        )

    def parse_events(self, raw: list[dict[str, Any]]) -> list[Event]:
        return []

    def healthcheck(self) -> SourceHealth:
        return SourceHealth(source_key=self._key, is_healthy=self._healthy)

    def supports_delta(self) -> bool:
        return False

    def confidence_score(self) -> float:
        return 0.5

    def close(self) -> None:
        pass


class TestSourceAdapterProtocol:
    def test_stub_satisfies_protocol(self):
        adapter = _StubAdapter()
        assert isinstance(adapter, SourceAdapter)

    def test_odds_api_adapter_satisfies_protocol(self):
        mock_client = MagicMock()
        adapter = OddsApiAdapter(client=mock_client)
        assert isinstance(adapter, SourceAdapter)
        adapter.close()


class TestOddsApiAdapter:
    def test_source_key(self):
        adapter = OddsApiAdapter(client=MagicMock())
        assert adapter.source_key == "odds_api"
        adapter.close()

    def test_discover_events_delegates(self):
        mock_client = MagicMock()
        mock_client.get_events.return_value = [{"id": "e1"}]
        adapter = OddsApiAdapter(client=mock_client)
        result = adapter.discover_events("basketball_nba")
        assert result == [{"id": "e1"}]
        mock_client.get_events.assert_called_once_with("basketball_nba")
        adapter.close()

    @patch("src.config.MARKETS", "h2h,spreads")
    @patch("src.config.REGIONS", "us")
    def test_fetch_odds_returns_snapshot(self):
        mock_client = MagicMock()
        mock_client.get_odds.return_value = (NBA_TWO_WAY_ARB, 497)
        mock_client.base_url = "https://api.the-odds-api.com/v4"
        adapter = OddsApiAdapter(client=mock_client)

        snap = adapter.fetch_odds("basketball_nba")
        assert isinstance(snap, SourceSnapshot)
        assert snap.source_key == "odds_api"
        assert snap.sport_key == "basketball_nba"
        assert snap.credits_remaining == 497
        assert snap.status_code == 200
        parsed = json.loads(snap.raw_payload)
        assert len(parsed) == len(NBA_TWO_WAY_ARB)
        adapter.close()

    def test_parse_events_delegates(self):
        adapter = OddsApiAdapter(client=MagicMock())
        events = adapter.parse_events(NBA_TWO_WAY_ARB)
        assert len(events) == 1
        assert events[0].home_team == "Los Angeles Lakers"
        adapter.close()

    def test_healthcheck_healthy_initially(self):
        adapter = OddsApiAdapter(client=MagicMock())
        health = adapter.healthcheck()
        assert health.is_healthy is True
        assert health.failure_count == 0
        adapter.close()

    def test_healthcheck_after_failure(self):
        mock_client = MagicMock()
        mock_client.get_events.side_effect = RuntimeError("timeout")
        adapter = OddsApiAdapter(client=mock_client)
        with pytest.raises(RuntimeError):
            adapter.discover_events("basketball_nba")
        health = adapter.healthcheck()
        assert health.is_healthy is False
        assert health.failure_count == 1
        assert "timeout" in (health.error_message or "")
        adapter.close()

    def test_healthcheck_recovers_after_success(self):
        mock_client = MagicMock()
        mock_client.get_events.side_effect = [RuntimeError("err"), [{"id": "e1"}]]
        adapter = OddsApiAdapter(client=mock_client)
        with pytest.raises(RuntimeError):
            adapter.discover_events("nba")
        assert adapter.healthcheck().is_healthy is False
        adapter.discover_events("nba")
        assert adapter.healthcheck().is_healthy is True
        adapter.close()

    def test_supports_delta_false(self):
        adapter = OddsApiAdapter(client=MagicMock())
        assert adapter.supports_delta() is False
        adapter.close()

    def test_confidence_score(self):
        adapter = OddsApiAdapter(client=MagicMock())
        assert adapter.confidence_score() == 0.95
        adapter.close()

    def test_context_manager(self):
        mock_client = MagicMock()
        with OddsApiAdapter(client=mock_client) as adapter:
            assert adapter.source_key == "odds_api"


class TestSourceRegistry:
    def test_register_and_get(self):
        reg = SourceRegistry()
        stub = _StubAdapter("test_src")
        reg.register(stub)
        assert reg.get("test_src") is stub

    def test_get_missing_raises(self):
        reg = SourceRegistry()
        with pytest.raises(KeyError, match="no_such"):
            reg.get("no_such")

    def test_all_returns_all(self):
        reg = SourceRegistry()
        reg.register(_StubAdapter("a"))
        reg.register(_StubAdapter("b"))
        assert len(reg.all()) == 2

    def test_healthy_filters(self):
        reg = SourceRegistry()
        reg.register(_StubAdapter("ok", healthy=True))
        reg.register(_StubAdapter("bad", healthy=False))
        healthy = reg.healthy()
        assert len(healthy) == 1
        assert healthy[0].source_key == "ok"

    def test_keys(self):
        reg = SourceRegistry()
        reg.register(_StubAdapter("x"))
        reg.register(_StubAdapter("y"))
        assert set(reg.keys()) == {"x", "y"}

    def test_len_and_contains(self):
        reg = SourceRegistry()
        reg.register(_StubAdapter("z"))
        assert len(reg) == 1
        assert "z" in reg
        assert "nope" not in reg

    def test_close_all(self):
        reg = SourceRegistry()
        s1 = _StubAdapter("a")
        s2 = _StubAdapter("b")
        reg.register(s1)
        reg.register(s2)
        reg.close_all()
        assert len(reg) == 0

    def test_iter(self):
        reg = SourceRegistry()
        reg.register(_StubAdapter("p"))
        reg.register(_StubAdapter("q"))
        keys = [a.source_key for a in reg]
        assert set(keys) == {"p", "q"}

    def test_replace_warns(self):
        reg = SourceRegistry()
        reg.register(_StubAdapter("dup"))
        reg.register(_StubAdapter("dup"))
        assert len(reg) == 1
