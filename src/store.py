"""SQLite persistence for collected quotes and run metadata.

SQLite because the requirement is a pipeline that runs locally and unattended:
it is in the standard library, needs no server to be up, survives restarts, and
is queryable with tools already on the machine.

The ``quote`` table is UNIQUE on ``(run_id, dedup_key)``, so a parser that emits
the same priced selection twice in one run fails at the storage boundary as well
as in validation.  The key is a single text column rather than a constraint over
the individual columns because SQLite treats NULLs as distinct in a UNIQUE
constraint — a moneyline has no line and no side, so a column-wise constraint
would never have fired for exactly the rows most at risk of duplication.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.raw_store import RawResponse
from src.schema import BaseballQuote, Market, Period, QuoteStatus, Selection, Side
from src.sources.base import Rejection, SourceHealth
from src.validation import Finding, ValidationReport

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    ok            INTEGER,
    quote_count   INTEGER NOT NULL DEFAULT 0,
    event_count   INTEGER NOT NULL DEFAULT 0,
    error_count   INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS raw_response (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES collection_run(id),
    source       TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    url          TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    content_type TEXT,
    fetched_at   TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    byte_size    INTEGER NOT NULL,
    path         TEXT NOT NULL,
    raw_ref      TEXT NOT NULL,
    headers      TEXT,
    -- 1 when this fetch returned bytes identical to the previous run's fetch of
    -- the same endpoint, so an unchanging feed is visible rather than implied.
    unchanged    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_raw_run ON raw_response(run_id, source);

CREATE TABLE IF NOT EXISTS quote (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES collection_run(id),
    source              TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    raw_ref             TEXT NOT NULL,
    sport               TEXT NOT NULL,
    league              TEXT NOT NULL,
    event_key           TEXT NOT NULL,
    source_event_id     TEXT NOT NULL,
    home_team           TEXT NOT NULL,
    away_team           TEXT NOT NULL,
    commence_time       TEXT NOT NULL,
    market              TEXT NOT NULL,
    period              TEXT NOT NULL,
    selection           TEXT NOT NULL,
    side                TEXT,
    line                REAL,
    is_alternate        INTEGER NOT NULL DEFAULT 0,
    decimal_odds        REAL NOT NULL,
    american_odds       INTEGER NOT NULL,
    implied_probability REAL NOT NULL,
    source_market_id    TEXT,
    source_selection_id TEXT,
    limit_amount        REAL,
    status              TEXT NOT NULL,
    last_change_at      TEXT,
    dedup_key           TEXT NOT NULL,
    -- Uniqueness is enforced on the explicit dedup key rather than on the
    -- individual columns: SQLite treats NULLs as distinct in a UNIQUE
    -- constraint, so a moneyline (side and line both NULL) would never have
    -- collided and duplicate moneylines would have stored silently.
    UNIQUE (run_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_quote_event ON quote(event_key, market, period);
CREATE INDEX IF NOT EXISTS idx_quote_run ON quote(run_id, source);

CREATE TABLE IF NOT EXISTS source_health (
    run_id          INTEGER NOT NULL REFERENCES collection_run(id),
    source_key      TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    checked_at      TEXT NOT NULL,
    request_count   INTEGER NOT NULL DEFAULT 0,
    raw_bytes       INTEGER NOT NULL DEFAULT 0,
    latency_ms      REAL,
    quote_count     INTEGER NOT NULL DEFAULT 0,
    event_count     INTEGER NOT NULL DEFAULT 0,
    rejection_count INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    unchanged_payloads INTEGER NOT NULL DEFAULT 0,
    error_kind      TEXT,
    error_message   TEXT,
    PRIMARY KEY (run_id, source_key)
);

CREATE TABLE IF NOT EXISTS rejection (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL REFERENCES collection_run(id),
    source   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    detail   TEXT NOT NULL,
    context  TEXT
);

CREATE TABLE IF NOT EXISTS skipped (
    run_id   INTEGER NOT NULL REFERENCES collection_run(id),
    source   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    count    INTEGER NOT NULL,
    PRIMARY KEY (run_id, source, reason)
);

CREATE TABLE IF NOT EXISTS finding (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES collection_run(id),
    severity  TEXT NOT NULL,
    code      TEXT NOT NULL,
    message   TEXT NOT NULL,
    source    TEXT,
    event_key TEXT
);
"""


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


class Store:
    """Durable record of what was collected, from where, and whether it held up."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"database at {self.path} has schema version {row['version']}, "
                    f"this build expects {SCHEMA_VERSION}"
                )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── runs ─────────────────────────────────────────────────────────────────

    def start_run(self, started_at: datetime) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO collection_run (started_at) VALUES (?)", (_iso(started_at),)
            )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        report: ValidationReport,
        note: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE collection_run
                      SET finished_at = ?, ok = ?, quote_count = ?, event_count = ?,
                          error_count = ?, warning_count = ?, note = ?
                    WHERE id = ?""",
                (
                    _iso(finished_at),
                    int(report.ok),
                    report.quote_count,
                    report.event_count,
                    len(report.errors),
                    len(report.warnings),
                    note,
                    run_id,
                ),
            )

    # ── writes ───────────────────────────────────────────────────────────────

    def previous_sha(self, source: str, endpoint: str, *, before_run_id: int) -> str | None:
        """The sha256 this endpoint last returned, for change detection."""
        row = self._conn.execute(
            """SELECT sha256 FROM raw_response
                WHERE source = ? AND endpoint = ? AND run_id < ?
                ORDER BY run_id DESC, id DESC LIMIT 1""",
            (source, endpoint, before_run_id),
        ).fetchone()
        return row["sha256"] if row else None

    def record_raw(
        self, run_id: int, raw: RawResponse, path: Path, *, unchanged: bool = False
    ) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO raw_response
                   (run_id, source, endpoint, url, status_code, content_type,
                    fetched_at, sha256, byte_size, path, raw_ref, headers, unchanged)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    raw.source,
                    raw.endpoint,
                    raw.url,
                    raw.status_code,
                    raw.content_type,
                    _iso(raw.fetched_at),
                    raw.sha256,
                    raw.byte_size,
                    str(path),
                    raw.ref,
                    json.dumps(raw.headers),
                    int(unchanged),
                ),
            )

    def save_quotes(self, run_id: int, quotes: Iterable[BaseballQuote]) -> int:
        rows = [
            (
                run_id,
                q.source,
                _iso(q.observed_at),
                q.raw_ref,
                q.sport,
                q.league,
                q.event_key,
                q.source_event_id,
                q.home_team,
                q.away_team,
                _iso(q.commence_time),
                q.market.value,
                q.period.value,
                q.selection.value,
                q.side.value if q.side else None,
                q.line,
                int(q.is_alternate),
                q.decimal_odds,
                q.american_odds,
                q.implied_probability,
                q.source_market_id,
                q.source_selection_id,
                q.limit_amount,
                q.status.value,
                _iso(q.last_change_at),
                "|".join(q.dedup_key),
            )
            for q in quotes
        ]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(
                """INSERT INTO quote
                   (run_id, source, observed_at, raw_ref, sport, league, event_key,
                    source_event_id, home_team, away_team, commence_time, market, period,
                    selection, side, line, is_alternate, decimal_odds, american_odds,
                    implied_probability, source_market_id, source_selection_id,
                    limit_amount, status, last_change_at, dedup_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    def save_health(self, run_id: int, health: SourceHealth) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO source_health
                   (run_id, source_key, ok, checked_at, request_count, raw_bytes, latency_ms,
                    quote_count, event_count, rejection_count, skipped_count,
                    unchanged_payloads, error_kind, error_message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    health.source_key,
                    int(health.ok),
                    _iso(health.checked_at),
                    health.request_count,
                    health.raw_bytes,
                    health.latency_ms,
                    health.quote_count,
                    health.event_count,
                    health.rejection_count,
                    health.skipped_count,
                    health.unchanged_payloads,
                    health.error_kind,
                    health.error_message,
                ),
            )

    def save_rejections(self, run_id: int, rejections: Sequence[Rejection]) -> None:
        if not rejections:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT INTO rejection (run_id, source, reason, detail, context) VALUES (?,?,?,?,?)",
                [
                    (run_id, r.source, r.reason, r.detail, json.dumps(r.context, default=str))
                    for r in rejections
                ],
            )

    def save_skipped(self, run_id: int, source: str, skipped: dict[str, int]) -> None:
        if not skipped:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO skipped (run_id, source, reason, count) VALUES (?,?,?,?)",
                [(run_id, source, reason, count) for reason, count in skipped.items()],
            )

    def save_findings(self, run_id: int, findings: Sequence[Finding]) -> None:
        if not findings:
            return
        with self._conn:
            self._conn.executemany(
                """INSERT INTO finding (run_id, severity, code, message, source, event_key)
                   VALUES (?,?,?,?,?,?)""",
                [
                    (run_id, f.severity.value, f.code, f.message, f.source, f.event_key)
                    for f in findings
                ],
            )

    # ── reads ────────────────────────────────────────────────────────────────

    def load_quotes(self, run_id: int) -> list[BaseballQuote]:
        rows = self._conn.execute(
            "SELECT * FROM quote WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [_quote_from_row(row) for row in rows]

    def raw_paths(self, run_id: int, source: str | None = None) -> list[tuple[str, Path]]:
        sql = "SELECT source, path FROM raw_response WHERE run_id = ?"
        params: list[Any] = [run_id]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id"
        return [(row["source"], Path(row["path"])) for row in self._conn.execute(sql, params)]

    def latest_run_id(self, *, only_ok: bool = False) -> int | None:
        sql = "SELECT id FROM collection_run WHERE finished_at IS NOT NULL"
        if only_ok:
            sql += " AND ok = 1"
        sql += " ORDER BY id DESC LIMIT 1"
        row = self._conn.execute(sql).fetchone()
        return int(row["id"]) if row else None

    def run_summaries(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT id, started_at, finished_at, ok, quote_count, event_count,
                      error_count, warning_count
                 FROM collection_run
                ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def health_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM source_health WHERE run_id = ? ORDER BY source_key", (run_id,)
        ).fetchall()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()


def _quote_from_row(row: sqlite3.Row) -> BaseballQuote:
    return BaseballQuote(
        source=row["source"],
        observed_at=datetime.fromisoformat(row["observed_at"]),
        raw_ref=row["raw_ref"],
        sport=row["sport"],
        league=row["league"],
        event_key=row["event_key"],
        source_event_id=row["source_event_id"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        commence_time=datetime.fromisoformat(row["commence_time"]),
        market=Market(row["market"]),
        period=Period(row["period"]),
        selection=Selection(row["selection"]),
        side=Side(row["side"]) if row["side"] else None,
        line=row["line"],
        is_alternate=bool(row["is_alternate"]),
        decimal_odds=row["decimal_odds"],
        american_odds=row["american_odds"],
        implied_probability=row["implied_probability"],
        source_market_id=row["source_market_id"],
        source_selection_id=row["source_selection_id"],
        limit_amount=row["limit_amount"],
        status=QuoteStatus(row["status"]),
        last_change_at=(
            datetime.fromisoformat(row["last_change_at"]) if row["last_change_at"] else None
        ),
    )
