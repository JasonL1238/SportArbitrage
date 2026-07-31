"""SQLite persistence for promo collection runs.

Separate database from the odds collector on purpose — different schema, different
cadence, and a promo outage must not block arb history.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.promos.base import PromoSourceHealth
from src.promos.schema import PromoOffer

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    offer_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS promo_health (
    run_id INTEGER NOT NULL REFERENCES promo_runs(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    ok INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    raw_bytes INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    offer_count INTEGER NOT NULL DEFAULT 0,
    rejection_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    error_message TEXT,
    PRIMARY KEY (run_id, source_key)
);

CREATE TABLE IF NOT EXISTS promo_offers (
    run_id INTEGER NOT NULL REFERENCES promo_runs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    terms TEXT NOT NULL DEFAULT '',
    url TEXT,
    starts_at TEXT,
    ends_at TEXT,
    observed_at TEXT NOT NULL,
    raw_ref TEXT NOT NULL DEFAULT '',
    raw_kind TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '',
    requires_login INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, source, offer_id)
);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class PromoStore:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def start_run(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO promo_runs(started_at, ok) VALUES (?, 0)",
            (_iso(datetime.now(UTC)),),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        ok: bool,
        offers: Sequence[PromoOffer],
        health: Sequence[PromoSourceHealth],
        notes: str = "",
    ) -> None:
        self._conn.execute("DELETE FROM promo_offers WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM promo_health WHERE run_id = ?", (run_id,))
        seen: set[tuple[str, str]] = set()
        for offer in offers:
            key = (offer.source, offer.offer_id)
            if key in seen:
                continue
            seen.add(key)
            self._conn.execute(
                """
                INSERT INTO promo_offers(
                    run_id, source, offer_id, kind, title, description, terms,
                    url, starts_at, ends_at, observed_at, raw_ref, raw_kind,
                    product, requires_login, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    offer.source,
                    offer.offer_id,
                    offer.kind.value,
                    offer.title,
                    offer.description[:4000],
                    offer.terms[:4000],
                    offer.url,
                    _iso(offer.starts_at),
                    _iso(offer.ends_at),
                    _iso(offer.observed_at),
                    offer.raw_ref,
                    offer.raw_kind,
                    offer.product,
                    int(offer.requires_login),
                    json.dumps(offer.metadata, sort_keys=True),
                ),
            )
        for item in health:
            self._conn.execute(
                """
                INSERT INTO promo_health(
                    run_id, source_key, ok, checked_at, request_count, raw_bytes,
                    latency_ms, offer_count, rejection_count, skipped_count,
                    error_kind, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item.source_key,
                    int(item.ok),
                    _iso(item.checked_at),
                    item.request_count,
                    item.raw_bytes,
                    item.latency_ms,
                    item.offer_count,
                    item.rejection_count,
                    item.skipped_count,
                    item.error_kind,
                    item.error_message,
                ),
            )
        self._conn.execute(
            """
            UPDATE promo_runs
            SET finished_at = ?, ok = ?, offer_count = ?, source_count = ?, notes = ?
            WHERE id = ?
            """,
            (
                _iso(datetime.now(UTC)),
                int(ok),
                len(seen),
                len(health),
                notes,
                run_id,
            ),
        )
        self._conn.commit()

    def list_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT id, started_at, finished_at, ok, offer_count, source_count, notes
            FROM promo_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def offers_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT source, offer_id, kind, title, description, url, ends_at, product,
                   requires_login, raw_kind
            FROM promo_offers
            WHERE run_id = ?
            ORDER BY source, kind, title
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_run_id(self) -> int | None:
        row = self._conn.execute("SELECT MAX(id) AS id FROM promo_runs").fetchone()
        return int(row["id"]) if row and row["id"] is not None else None


__all__ = ["PromoStore", "SCHEMA_VERSION"]
