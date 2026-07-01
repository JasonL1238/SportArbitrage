from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from src.models import Event
from src.scanner import run_scan
from src.sources.base import SourceHealth, SourceSnapshot
from src.sources.registry import SourceRegistry
from tests.fixtures.api_snapshots import NBA_TWO_WAY_ARB


class _SnapshotAdapter:
    def __init__(self, payload: list[dict[str, Any]], *, credits_used: int = 3) -> None:
        self.payload = payload
        self.credits_used = credits_used
        self.closed = False

    @property
    def source_key(self) -> str:
        return "snapshot"

    def discover_events(self, sport_key: str) -> list[dict[str, Any]]:
        return [{"id": "event_1"}]

    def fetch_odds(self, sport_key: str) -> SourceSnapshot:
        return SourceSnapshot(
            source_key=self.source_key,
            sport_key=sport_key,
            raw_payload=json.dumps(self.payload),
            fetched_at=datetime.now(UTC),
            credits_used=self.credits_used,
            credits_remaining=100,
        )

    def parse_events(self, raw: list[dict[str, Any]]) -> list[Event]:
        from src.odds_client import OddsClient

        return OddsClient.parse_events(raw)

    def healthcheck(self) -> SourceHealth:
        return SourceHealth(source_key=self.source_key, is_healthy=True)

    def supports_delta(self) -> bool:
        return False

    def confidence_score(self) -> float:
        return 1.0

    def close(self) -> None:
        self.closed = True


def _registry(adapter: _SnapshotAdapter) -> SourceRegistry:
    registry = SourceRegistry()
    registry.register(adapter)
    return registry


def test_run_scan_without_database_writes_local_records(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.DATABASE_URL", "")
    monkeypatch.setattr("src.config.LOCAL_DATA_DIR", tmp_path)
    monkeypatch.setattr("src.config.ENABLE_ALERTS", False)
    monkeypatch.setattr("src.config.REQUIRE_DISTINCT_BOOKS", True)
    monkeypatch.setattr("src.config.REQUIRE_COMPLETE_OUTCOMES", True)

    adapter = _SnapshotAdapter(NBA_TWO_WAY_ARB, credits_used=3)
    summary = run_scan(
        sports=["basketball_nba"],
        registry=_registry(adapter),
        min_margin=0.01,
        total_stake=100,
    )

    assert summary.opportunities == 1
    assert summary.credits_used == 3
    assert adapter.closed is True

    records = [
        json.loads(line)
        for line in (tmp_path / "scan_records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(r["type"] == "raw_snapshot" for r in records)
    assert any(r["type"] == "arb_opportunity" for r in records)
    assert any(r["type"] == "alert_log" and r["skipped"] is True for r in records)


def test_run_scan_dry_run_does_not_fetch_paid_odds(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.DATABASE_URL", "")
    monkeypatch.setattr("src.config.LOCAL_DATA_DIR", tmp_path)

    adapter = _SnapshotAdapter(NBA_TWO_WAY_ARB)
    summary = run_scan(
        sports=["basketball_nba"],
        registry=_registry(adapter),
        dry_run=True,
    )

    assert summary.opportunities == 0
    records = [
        json.loads(line)
        for line in (tmp_path / "scan_records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert not any(r["type"] == "raw_snapshot" for r in records)
