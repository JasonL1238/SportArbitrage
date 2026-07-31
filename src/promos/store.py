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
from typing import Any, Sequence

from src.promos.base import PromoSourceHealth
from src.promos.schema import PromoOffer

SCHEMA_VERSION = 2

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
    summary TEXT NOT NULL DEFAULT '',
    eligible_regions_json TEXT NOT NULL DEFAULT '[]',
    ineligible_regions_json TEXT NOT NULL DEFAULT '[]',
    eligibility_notes TEXT NOT NULL DEFAULT '',
    bonus_amount REAL,
    min_deposit REAL,
    min_odds TEXT,
    wagering_requirement TEXT,
    reward_type TEXT NOT NULL DEFAULT '',
    usage_guidance TEXT NOT NULL DEFAULT '',
    is_specific INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, source, offer_id)
);
"""

_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("summary", "TEXT NOT NULL DEFAULT ''"),
    ("eligible_regions_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("ineligible_regions_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("eligibility_notes", "TEXT NOT NULL DEFAULT ''"),
    ("bonus_amount", "REAL"),
    ("min_deposit", "REAL"),
    ("min_odds", "TEXT"),
    ("wagering_requirement", "TEXT"),
    ("reward_type", "TEXT NOT NULL DEFAULT ''"),
    ("usage_guidance", "TEXT NOT NULL DEFAULT ''"),
    ("is_specific", "INTEGER NOT NULL DEFAULT 0"),
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_list(value: Sequence[str] | None) -> str:
    return json.dumps(list(value or []), sort_keys=False)


def _load_str_list(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        code = str(item or "").strip().upper()
        if code:
            out.append(code)
    return out


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
        self._migrate()

    def _migrate(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row["value"]) if row is not None else 0
        if current < 2:
            cols = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(promo_offers)").fetchall()
            }
            for name, decl in _V2_COLUMNS:
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE promo_offers ADD COLUMN {name} {decl}")
            current = 2
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) != SCHEMA_VERSION:
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
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
                    product, requires_login, summary, eligible_regions_json,
                    ineligible_regions_json, eligibility_notes, bonus_amount,
                    min_deposit, min_odds, wagering_requirement, reward_type,
                    usage_guidance, is_specific, metadata_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    offer.source,
                    offer.offer_id,
                    offer.kind.value,
                    offer.title,
                    offer.description[:4000],
                    offer.terms[:8000],
                    offer.url,
                    _iso(offer.starts_at),
                    _iso(offer.ends_at),
                    _iso(offer.observed_at),
                    offer.raw_ref,
                    offer.raw_kind,
                    offer.product,
                    int(offer.requires_login),
                    (offer.summary or "")[:1000],
                    _json_list(offer.eligible_regions),
                    _json_list(offer.ineligible_regions),
                    (offer.eligibility_notes or "")[:2000],
                    offer.bonus_amount,
                    offer.min_deposit,
                    offer.min_odds,
                    offer.wagering_requirement,
                    offer.reward_type or "",
                    (offer.usage_guidance or "")[:8000],
                    int(offer.is_specific),
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
            SELECT source, offer_id, kind, title, description, terms, url,
                   starts_at, ends_at, product, requires_login, raw_kind,
                   summary, eligible_regions_json, ineligible_regions_json,
                   eligibility_notes, bonus_amount, min_deposit, min_odds,
                   wagering_requirement, reward_type, usage_guidance,
                   is_specific, metadata_json
            FROM promo_offers
            WHERE run_id = ?
            ORDER BY source, kind, title
            """,
            (run_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except ValueError:
                item["metadata"] = {}
                item.pop("metadata_json", None)
            if not isinstance(item.get("metadata"), dict):
                item["metadata"] = {}
            item["eligible_regions"] = _load_str_list(item.pop("eligible_regions_json", None))
            item["ineligible_regions"] = _load_str_list(
                item.pop("ineligible_regions_json", None)
            )
            item["requires_login"] = bool(item.get("requires_login"))
            item["is_specific"] = bool(item.get("is_specific"))
            out.append(item)
        return out

    def health_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT source_key, ok, checked_at, request_count, raw_bytes, latency_ms,
                   offer_count, rejection_count, skipped_count, error_kind, error_message
            FROM promo_health
            WHERE run_id = ?
            ORDER BY source_key
            """,
            (run_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["ok"] = bool(item["ok"])
            out.append(item)
        return out

    def latest_run_id(self) -> int | None:
        row = self._conn.execute("SELECT MAX(id) AS id FROM promo_runs").fetchone()
        return int(row["id"]) if row and row["id"] is not None else None


__all__ = ["PromoStore", "SCHEMA_VERSION"]
