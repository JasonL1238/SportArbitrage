"""Append-only local persistence for development scans.

This store intentionally mirrors the scanner's small persistence surface
without pretending to be the production database.  It lets local runs keep
snapshots, opportunities, and alert dedup state when Postgres is not set up.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.models import ArbOpportunity


@dataclass
class LocalStore:
    data_dir: Path

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.data_dir / "scan_records.jsonl"
        self.state_path = self.data_dir / "state.json"

    def init(self) -> None:
        self.records_path.touch(exist_ok=True)
        if not self.state_path.exists():
            self.state_path.write_text("{}", encoding="utf-8")

    def start_scan_run(self, sports: list[str]) -> int:
        run_id = self._next_id("scan_run")
        self._append({
            "type": "scan_run_started",
            "id": run_id,
            "started_at": datetime.now(UTC).isoformat(),
            "sports_scanned": sports,
        })
        return run_id

    def finish_scan_run(
        self,
        run_id: int,
        credits_used: int,
        *,
        status: str,
        error_message: str | None,
        opportunities_found: int,
    ) -> None:
        self._append({
            "type": "scan_run_finished",
            "id": run_id,
            "finished_at": datetime.now(UTC).isoformat(),
            "credits_used": credits_used,
            "status": status,
            "error_message": error_message,
            "opportunities_found": opportunities_found,
        })

    def save_snapshot(
        self,
        sport_key: str,
        response_json: str,
        *,
        scan_run_id: int | None,
        credits_used: int | None,
        credits_remaining: int | None,
        source_key: str,
        fetch_url: str | None,
        status_code: int | None,
        content_type: str | None,
        parser_version: str,
    ) -> int:
        snapshot_id = self._next_id("snapshot")
        self._append({
            "type": "raw_snapshot",
            "id": snapshot_id,
            "scan_run_id": scan_run_id,
            "sport_key": sport_key,
            "fetched_at": datetime.now(UTC).isoformat(),
            "response_json": json.loads(response_json),
            "credits_used": credits_used,
            "credits_remaining": credits_remaining,
            "source_key": source_key,
            "fetch_url": fetch_url,
            "status_code": status_code,
            "content_type": content_type,
            "parser_version": parser_version,
        })
        return snapshot_id

    def save_normalized_odds(
        self,
        snapshot_id: int,
        scan_run_id: int,
        events: list,
        *,
        source_key: str,
    ) -> int:
        count = 0
        fetched_at = datetime.now(UTC).isoformat()
        for event in events:
            for bookmaker in event.bookmakers:
                for market in bookmaker.markets:
                    for outcome in market.outcomes:
                        self._append({
                            "type": "normalized_odd",
                            "snapshot_id": snapshot_id,
                            "scan_run_id": scan_run_id,
                            "source_key": source_key,
                            "sport_key": event.sport_key,
                            "event_id": event.id,
                            "bookmaker_key": bookmaker.key,
                            "bookmaker_title": bookmaker.title,
                            "market_key": market.key,
                            "outcome_name": outcome.name,
                            "line": outcome.point,
                            "decimal_odds": outcome.price,
                            "last_update": market.last_update.isoformat(),
                            "fetched_at": fetched_at,
                        })
                        count += 1
        return count

    def save_opportunity(
        self,
        opp: ArbOpportunity,
        dedup_key: str,
        *,
        scan_run_id: int | None,
    ) -> int:
        opp_id = self._next_id("opportunity")
        self._append({
            "type": "arb_opportunity",
            "id": opp_id,
            "scan_run_id": scan_run_id,
            "dedup_key": dedup_key,
            **opp.model_dump(mode="json"),
        })
        return opp_id

    def get_last_alert_for_dedup_key(self, dedup_key: str) -> dict[str, Any] | None:
        last: dict[str, Any] | None = None
        for record in self._records():
            if (
                record.get("type") == "alert_log"
                and record.get("dedup_key") == dedup_key
                and not record.get("skipped", False)
            ):
                last = record
        return last

    def save_alert_log(
        self,
        opportunity_id: int,
        dedup_key: str,
        margin: float,
        channel: str,
        *,
        success: bool = True,
        error_message: str | None = None,
        skipped: bool = False,
        skip_reason: str | None = None,
    ) -> int:
        alert_id = self._next_id("alert")
        self._append({
            "type": "alert_log",
            "id": alert_id,
            "opportunity_id": opportunity_id,
            "dedup_key": dedup_key,
            "margin": margin,
            "sent_at": datetime.now(UTC).isoformat(),
            "channel": channel,
            "success": success,
            "error_message": error_message,
            "skipped": skipped,
            "skip_reason": skip_reason,
        })
        return alert_id

    def close(self) -> None:
        return None

    def _records(self) -> list[dict[str, Any]]:
        if not self.records_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.records_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def _append(self, record: dict[str, Any]) -> None:
        with self.records_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def _next_id(self, key: str) -> int:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        value = int(state.get(key, 0)) + 1
        state[key] = value
        self.state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        return value
