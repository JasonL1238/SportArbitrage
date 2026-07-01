"""Sports arbitrage scanner CLI.

Usage:
    python -m src.scanner [--sport basketball_nba] [--dry-run] [--min-margin 0.01]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import UTC, datetime

from src import config
from src.alerts import send_alerts
from src.arb import filter_stale_odds, find_arbs
from src.db import (
    finish_scan_run,
    get_connection,
    get_last_alert_for_dedup_key,
    init_db,
    save_alert_log,
    save_normalized_odds,
    save_opportunity,
    save_snapshot,
    start_scan_run,
)
from src.dedup import make_dedup_key, should_alert
from src.sources.base import SourceAdapter
from src.sources.odds_api import OddsApiAdapter
from src.sources.registry import SourceRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def build_default_registry() -> SourceRegistry:
    """Create a registry with the configured source adapters."""
    registry = SourceRegistry()
    if config.ODDS_API_KEY:
        registry.register(OddsApiAdapter())
    return registry


def _process_snapshot(
    conn,
    adapter: SourceAdapter,
    sport_key: str,
    run_id: int,
    min_margin: float,
    total_stake: float,
) -> int:
    """Fetch, parse, detect arbs, and persist for one source + sport. Returns opp count."""
    snapshot = adapter.fetch_odds(sport_key)
    raw_data = json.loads(snapshot.raw_payload)

    if not raw_data:
        return 0

    snapshot_id = save_snapshot(
        conn,
        sport_key,
        snapshot.raw_payload,
        scan_run_id=run_id,
        credits_used=snapshot.credits_used,
        credits_remaining=snapshot.credits_remaining,
        source_key=adapter.source_key,
        fetch_url=snapshot.url,
        status_code=snapshot.status_code,
        content_type=snapshot.content_type,
        parser_version=snapshot.parser_version,
    )

    events = adapter.parse_events(raw_data)
    save_normalized_odds(conn, snapshot_id, run_id, events, source_key=adapter.source_key)
    events = filter_stale_odds(events, config.MAX_ODDS_AGE_SECONDS)
    opportunities = find_arbs(events, min_margin=min_margin, total_stake=total_stake)

    for opp in opportunities:
        dedup_key = make_dedup_key(opp)
        opp_id = save_opportunity(conn, opp, dedup_key, scan_run_id=run_id)

        last = get_last_alert_for_dedup_key(conn, dedup_key)
        send, skip_reason = should_alert(opp, last)

        if send and config.ENABLE_ALERTS:
            success = send_alerts(opp)
            save_alert_log(
                conn, opp_id, dedup_key, opp.margin,
                channel="discord" if config.DISCORD_WEBHOOK_URL else "terminal",
                success=success,
            )
        elif send:
            save_alert_log(
                conn, opp_id, dedup_key, opp.margin,
                channel="none", skipped=True, skip_reason="alerts_disabled",
            )
        else:
            save_alert_log(
                conn, opp_id, dedup_key, opp.margin,
                channel="none", skipped=True, skip_reason=skip_reason,
            )

        log.info(
            "ARB FOUND [%s]: %s vs %s | %s %s | margin=%s | books=%s | alerted=%s",
            adapter.source_key,
            opp.home_team,
            opp.away_team,
            opp.market_key,
            f"({opp.point})" if opp.point else "",
            opp.margin_pct,
            ", ".join(b.bookmaker_title for b in opp.best_outcomes),
            send and config.ENABLE_ALERTS,
        )

    return len(opportunities)


def run_scan(
    sports: list[str] | None = None,
    min_margin: float = config.MIN_ARB_MARGIN,
    total_stake: float = config.DEFAULT_STAKE,
    dry_run: bool = False,
    registry: SourceRegistry | None = None,
) -> None:
    sports = sports or config.TARGET_SPORTS

    if not config.DATABASE_URL:
        log.error("DATABASE_URL not set. See .env.example for Postgres connection string.")
        sys.exit(1)

    registry = registry or build_default_registry()
    if len(registry) == 0:
        log.error("No source adapters configured. Set ODDS_API_KEY or register a source.")
        sys.exit(1)

    conn = get_connection()
    init_db(conn)

    run_id = start_scan_run(conn, sports)
    credits_used = 0
    total_opps = 0
    errors: list[str] = []

    try:
        for adapter in registry:
            for sport_key in sports:
                try:
                    raw_events = adapter.discover_events(sport_key)
                except Exception:
                    log.exception("[%s] Failed to fetch events for %s", adapter.source_key, sport_key)
                    errors.append(f"events:{adapter.source_key}:{sport_key}")
                    continue

                if not raw_events:
                    log.info("[%s] No upcoming events for %s — skipping", adapter.source_key, sport_key)
                    continue

                log.info("[%s] %s: %d upcoming events", adapter.source_key, sport_key, len(raw_events))

                if dry_run:
                    log.info("[dry-run] Would fetch odds from %s for %s", adapter.source_key, sport_key)
                    continue

                try:
                    opp_count = _process_snapshot(
                        conn, adapter, sport_key, run_id, min_margin, total_stake,
                    )
                    total_opps += opp_count
                except Exception:
                    log.exception("[%s] Failed to fetch/process odds for %s", adapter.source_key, sport_key)
                    errors.append(f"odds:{adapter.source_key}:{sport_key}")
                    continue

                log.info(
                    "[%s] %s: scan complete, %d arbs found",
                    adapter.source_key, sport_key, opp_count,
                )

        error_msg = "; ".join(errors) if errors else None
        finish_scan_run(
            conn, run_id, credits_used,
            status="completed",
            error_message=error_msg,
            opportunities_found=total_opps,
        )
    except Exception:
        log.exception("Scan failed")
        try:
            finish_scan_run(
                conn, run_id, credits_used,
                status="failed",
                error_message=traceback.format_exc()[-500:],
                opportunities_found=total_opps,
            )
        except Exception:
            log.exception("Failed to record scan failure")
        raise
    finally:
        registry.close_all()
        conn.close()

    log.info(
        "Scan complete. Sources: %d, Sports: %d, Opportunities: %d",
        len(list(registry)), len(sports), total_opps,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sports arbitrage scanner")
    parser.add_argument("--sport", type=str, help="Scan a single sport key (e.g. basketball_nba)")
    parser.add_argument("--dry-run", action="store_true", help="Check events only, don't fetch odds")
    parser.add_argument("--min-margin", type=float, default=config.MIN_ARB_MARGIN, help="Minimum arb margin (default 0.01)")
    parser.add_argument("--stake", type=float, default=config.DEFAULT_STAKE, help="Total stake for split calculation")
    args = parser.parse_args()

    sports = [args.sport] if args.sport else None
    run_scan(sports=sports, min_margin=args.min_margin, total_stake=args.stake, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
