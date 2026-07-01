"""Postgres persistence layer.

All database access goes through this module. Tables are created via
``init_db`` on first connection — no external migration tool required.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from src import config
from src.models import ArbOpportunity, PaperTrade

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    sports_scanned JSONB NOT NULL,
    credits_used INT DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error_message TEXT,
    opportunities_found INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id BIGSERIAL PRIMARY KEY,
    scan_run_id BIGINT REFERENCES scan_runs(id),
    sport_key TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    response_json JSONB NOT NULL,
    credits_used INT,
    credits_remaining INT,
    source_key TEXT NOT NULL DEFAULT 'odds_api',
    fetch_url TEXT,
    status_code INT,
    headers_json JSONB,
    content_type TEXT,
    parser_version TEXT DEFAULT '1',
    parse_error TEXT
);

CREATE TABLE IF NOT EXISTS normalized_odds (
    id BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT REFERENCES raw_snapshots(id),
    scan_run_id BIGINT REFERENCES scan_runs(id),
    source_key TEXT NOT NULL DEFAULT 'odds_api',
    sport_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    canonical_event_id TEXT,
    bookmaker_key TEXT NOT NULL,
    bookmaker_title TEXT NOT NULL,
    market_key TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    line DOUBLE PRECISION,
    decimal_odds DOUBLE PRECISION NOT NULL,
    last_update TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    is_live BOOLEAN NOT NULL DEFAULT FALSE,
    confidence DOUBLE PRECISION DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS arb_opportunities (
    id BIGSERIAL PRIMARY KEY,
    scan_run_id BIGINT REFERENCES scan_runs(id),
    sport_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    commence_time TIMESTAMPTZ NOT NULL,
    market_key TEXT NOT NULL,
    line DOUBLE PRECISION,
    outcome_count INT NOT NULL,
    margin DOUBLE PRECISION NOT NULL,
    implied_prob_sum DOUBLE PRECISION NOT NULL,
    total_stake DOUBLE PRECISION NOT NULL,
    guaranteed_profit DOUBLE PRECISION NOT NULL,
    best_odds_json JSONB NOT NULL,
    stake_split_json JSONB NOT NULL,
    dedup_key TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_logs (
    id BIGSERIAL PRIMARY KEY,
    opportunity_id BIGINT REFERENCES arb_opportunities(id),
    dedup_key TEXT NOT NULL,
    margin DOUBLE PRECISION NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL,
    channel TEXT NOT NULL,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT,
    skipped BOOLEAN NOT NULL DEFAULT FALSE,
    skip_reason TEXT
);

CREATE TABLE IF NOT EXISTS paper_trade_checks (
    id BIGSERIAL PRIMARY KEY,
    opportunity_id BIGINT NOT NULL REFERENCES arb_opportunities(id),
    checked_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'unchecked',
    still_available BOOLEAN,
    odds_at_check_json JSONB,
    would_have_profit DOUBLE PRECISION,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_health (
    id BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    is_healthy BOOLEAN NOT NULL,
    fetch_count INT DEFAULT 0,
    failure_count INT DEFAULT 0,
    last_success TIMESTAMPTZ,
    last_failure TIMESTAMPTZ,
    error_message TEXT,
    latency_ms DOUBLE PRECISION
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_normalized_odds_event
    ON normalized_odds (event_id, market_key, line, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_arb_opportunities_detected
    ON arb_opportunities (detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_arb_opportunities_dedup
    ON arb_opportunities (dedup_key);
CREATE INDEX IF NOT EXISTS idx_alert_logs_dedup
    ON alert_logs (dedup_key, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_health_key
    ON source_health (source_key, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_snapshots_source
    ON raw_snapshots (source_key, fetched_at DESC);
"""

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_connection() -> psycopg.Connection:
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
    return conn


def init_db(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLES)
        cur.execute(_CREATE_INDEXES)
    conn.commit()


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def save_snapshot(
    conn: psycopg.Connection,
    sport_key: str,
    response_json: str,
    *,
    scan_run_id: int | None = None,
    credits_used: int | None = None,
    credits_remaining: int | None = None,
    source_key: str = "odds_api",
    fetch_url: str | None = None,
    status_code: int | None = None,
    headers_json: str | None = None,
    content_type: str | None = None,
    parser_version: str = "1",
    parse_error: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_snapshots "
            "(scan_run_id, sport_key, fetched_at, response_json, credits_used, "
            " credits_remaining, source_key, fetch_url, status_code, headers_json, "
            " content_type, parser_version, parse_error) "
            "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s) RETURNING id",
            (
                scan_run_id, sport_key, datetime.now(UTC), response_json,
                credits_used, credits_remaining, source_key, fetch_url,
                status_code, headers_json, content_type, parser_version,
                parse_error,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# Normalized odds
# ---------------------------------------------------------------------------


def save_normalized_odds(
    conn: psycopg.Connection,
    snapshot_id: int,
    scan_run_id: int,
    events: list,
    fetched_at: datetime | None = None,
    source_key: str = "odds_api",
) -> int:
    """Flatten parsed Event objects into one row per bookmaker-outcome."""
    ts = fetched_at or datetime.now(UTC)
    count = 0
    with conn.cursor() as cur:
        for event in events:
            for bm in event.bookmakers:
                for mkt in bm.markets:
                    for outcome in mkt.outcomes:
                        cur.execute(
                            "INSERT INTO normalized_odds "
                            "(snapshot_id, scan_run_id, source_key, sport_key, event_id, "
                            " bookmaker_key, bookmaker_title, market_key, outcome_name, "
                            " line, decimal_odds, last_update, fetched_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                snapshot_id, scan_run_id, source_key,
                                event.sport_key, event.id,
                                bm.key, bm.title, mkt.key, outcome.name,
                                outcome.point, outcome.price, mkt.last_update, ts,
                            ),
                        )
                        count += 1
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Scan runs
# ---------------------------------------------------------------------------


def start_scan_run(conn: psycopg.Connection, sports: list[str]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scan_runs (started_at, sports_scanned) VALUES (%s, %s::jsonb) RETURNING id",
            (datetime.now(UTC), json.dumps(sports)),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]  # type: ignore[index]


def finish_scan_run(
    conn: psycopg.Connection,
    run_id: int,
    credits_used: int,
    *,
    status: str = "completed",
    error_message: str | None = None,
    opportunities_found: int = 0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_runs SET finished_at=%s, credits_used=%s, status=%s, "
            "error_message=%s, opportunities_found=%s WHERE id=%s",
            (datetime.now(UTC), credits_used, status, error_message, opportunities_found, run_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


def save_opportunity(
    conn: psycopg.Connection,
    opp: ArbOpportunity,
    dedup_key: str,
    scan_run_id: int | None = None,
) -> int:
    best_odds = [
        {
            "outcome": b.outcome_name,
            "bookmaker": b.bookmaker_title,
            "bookmaker_key": b.bookmaker_key,
            "decimal_odds": b.decimal_odds,
            "point": b.point,
        }
        for b in opp.best_outcomes
    ]
    stake_data = [
        {"outcome": b.outcome_name, "bookmaker": b.bookmaker_title, "stake": s}
        for b, s in zip(opp.best_outcomes, opp.stakes)
    ]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO arb_opportunities "
            "(scan_run_id, sport_key, event_id, home_team, away_team, commence_time, "
            " market_key, line, outcome_count, margin, implied_prob_sum, total_stake, "
            " guaranteed_profit, best_odds_json, stake_split_json, dedup_key, detected_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s) RETURNING id",
            (
                scan_run_id,
                opp.sport_key,
                opp.event_id,
                opp.home_team,
                opp.away_team,
                opp.commence_time,
                opp.market_key,
                opp.point,
                opp.outcome_count,
                opp.margin,
                opp.implied_prob_sum,
                opp.total_stake,
                opp.guaranteed_profit,
                json.dumps(best_odds),
                json.dumps(stake_data),
                dedup_key,
                opp.detected_at,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]  # type: ignore[index]


def get_opportunities(
    conn: psycopg.Connection,
    sport_key: str | None = None,
    market_key: str | None = None,
    min_margin: float | None = None,
    bookmaker: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM arb_opportunities WHERE TRUE"
    params: list[Any] = []

    if sport_key:
        query += " AND sport_key = %s"
        params.append(sport_key)
    if market_key:
        query += " AND market_key = %s"
        params.append(market_key)
    if min_margin is not None:
        query += " AND margin >= %s"
        params.append(min_margin)
    if bookmaker:
        query += " AND best_odds_json::text ILIKE %s"
        params.append(f"%{bookmaker}%")

    query += " ORDER BY detected_at DESC LIMIT %s"
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [_jsonify_row(r) for r in rows]


def _jsonify_row(row: dict[str, Any]) -> dict[str, Any]:
    """Ensure JSONB columns are returned as Python objects (not strings)."""
    out = dict(row)
    for key in ("best_odds_json", "stake_split_json", "response_json", "odds_at_check_json", "sports_scanned"):
        if key in out and isinstance(out[key], str):
            out[key] = json.loads(out[key])
    return out


# ---------------------------------------------------------------------------
# Alert logs
# ---------------------------------------------------------------------------


def get_last_alert_for_dedup_key(conn: psycopg.Connection, dedup_key: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM alert_logs "
            "WHERE dedup_key = %s AND skipped = FALSE "
            "ORDER BY sent_at DESC LIMIT 1",
            (dedup_key,),
        )
        return cur.fetchone()


def save_alert_log(
    conn: psycopg.Connection,
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
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alert_logs "
            "(opportunity_id, dedup_key, margin, sent_at, channel, success, "
            " error_message, skipped, skip_reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                opportunity_id, dedup_key, margin, datetime.now(UTC), channel,
                success, error_message, skipped, skip_reason,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]  # type: ignore[index]


def get_alerted_opportunities(conn: psycopg.Connection, limit: int = 200) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.*, al.sent_at AS alert_sent_at, al.channel AS alert_channel "
            "FROM arb_opportunities o "
            "JOIN alert_logs al ON al.opportunity_id = o.id "
            "WHERE al.success = TRUE AND al.skipped = FALSE "
            "ORDER BY al.sent_at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [_jsonify_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Paper trade checks
# ---------------------------------------------------------------------------


def upsert_paper_trade(conn: psycopg.Connection, trade: PaperTrade) -> int:
    if trade.id is not None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE paper_trade_checks SET checked_at=%s, status=%s, still_available=%s, "
                "odds_at_check_json=%s::jsonb, would_have_profit=%s, notes=%s WHERE id=%s",
                (
                    trade.checked_at,
                    trade.status,
                    trade.still_available,
                    trade.odds_at_check_json,
                    trade.would_have_profit,
                    trade.notes,
                    trade.id,
                ),
            )
        conn.commit()
        return trade.id

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO paper_trade_checks "
            "(opportunity_id, checked_at, status, still_available, "
            " odds_at_check_json, would_have_profit, notes) "
            "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id",
            (
                trade.opportunity_id,
                trade.checked_at,
                trade.status,
                trade.still_available,
                trade.odds_at_check_json,
                trade.would_have_profit,
                trade.notes,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]  # type: ignore[index]


def get_paper_trades(conn: psycopg.Connection, opportunity_id: int | None = None) -> list[dict[str, Any]]:
    if opportunity_id is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM paper_trade_checks WHERE opportunity_id = %s ORDER BY checked_at DESC",
                (opportunity_id,),
            )
            rows = cur.fetchall()
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM paper_trade_checks ORDER BY checked_at DESC")
            rows = cur.fetchall()
    return [_jsonify_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def get_metrics_summary(conn: psycopg.Connection, days: int = 28) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt, AVG(margin) AS avg_margin "
            "FROM arb_opportunities WHERE detected_at >= NOW() - make_interval(days => %s)",
            (days,),
        )
        row = cur.fetchone()
        total_opps = row["cnt"] if row else 0
        avg_margin = row["avg_margin"] if row else 0

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM scan_runs "
            "WHERE started_at >= NOW() - make_interval(days => %s)",
            (days,),
        )
        scan_row = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM alert_logs "
            "WHERE success = TRUE AND skipped = FALSE "
            "AND sent_at >= NOW() - make_interval(days => %s)",
            (days,),
        )
        alert_row = cur.fetchone()
        alerts_sent = alert_row["cnt"] if alert_row else 0

        cur.execute(
            "SELECT status, COUNT(*) AS cnt FROM paper_trade_checks "
            "WHERE checked_at >= NOW() - make_interval(days => %s) GROUP BY status",
            (days,),
        )
        paper_rows = cur.fetchall()
        paper_counts: dict[str, int] = {r["status"]: r["cnt"] for r in paper_rows}

    checked = sum(v for k, v in paper_counts.items() if k != "unchecked")
    verified = paper_counts.get("real", 0) + paper_counts.get("placed_manually", 0)

    avg_verified_profit = 0.0
    if verified > 0:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT AVG(would_have_profit) AS avg_p, AVG(o.total_stake) AS avg_s "
                "FROM paper_trade_checks p "
                "JOIN arb_opportunities o ON o.id = p.opportunity_id "
                "WHERE p.status IN ('real', 'placed_manually') "
                "AND p.checked_at >= NOW() - make_interval(days => %s) "
                "AND p.would_have_profit IS NOT NULL",
                (days,),
            )
            vrow = cur.fetchone()
            if vrow and vrow["avg_p"] is not None and vrow["avg_s"]:
                avg_verified_profit = (vrow["avg_p"] / vrow["avg_s"]) * 100

    verified_pct = verified / checked if checked > 0 else 0.0
    projected_ev = (avg_verified_profit / 100) * alerts_sent * (30 / max(days, 1)) if alerts_sent else 0.0

    return {
        "days": days,
        "total_scans": scan_row["cnt"] if scan_row else 0,
        "theoretical_arbs_found": total_opps,
        "total_opportunities": total_opps,
        "opportunities_per_week": total_opps / max(days / 7, 1),
        "avg_margin": avg_margin or 0,
        "alerts_sent": alerts_sent,
        "checked_opportunities": checked,
        "verified_executable_pct": verified_pct,
        "avg_verified_profit_per_100": avg_verified_profit,
        "projected_monthly_ev": projected_ev,
        "paper_trade_counts": paper_counts,
        "checked": checked,
        "placed": paper_counts.get("placed_manually", 0),
        "success_rate": verified_pct,
    }


# ---------------------------------------------------------------------------
# Source health
# ---------------------------------------------------------------------------


def save_source_health(
    conn: psycopg.Connection,
    source_key: str,
    is_healthy: bool,
    *,
    fetch_count: int = 0,
    failure_count: int = 0,
    last_success: datetime | None = None,
    last_failure: datetime | None = None,
    error_message: str | None = None,
    latency_ms: float | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_health "
            "(source_key, recorded_at, is_healthy, fetch_count, failure_count, "
            " last_success, last_failure, error_message, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                source_key, datetime.now(UTC), is_healthy, fetch_count,
                failure_count, last_success, last_failure, error_message,
                latency_ms,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row["id"]  # type: ignore[index]


def get_source_health_latest(
    conn: psycopg.Connection,
) -> list[dict[str, Any]]:
    """Most recent health record per source."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (source_key) * FROM source_health "
            "ORDER BY source_key, recorded_at DESC"
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_source_health_history(
    conn: psycopg.Connection,
    source_key: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM source_health "
            "WHERE source_key = %s ORDER BY recorded_at DESC LIMIT %s",
            (source_key, limit),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_snapshot_by_id(
    conn: psycopg.Connection,
    snapshot_id: int,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM raw_snapshots WHERE id = %s", (snapshot_id,))
        row = cur.fetchone()
    return _jsonify_row(row) if row else None
