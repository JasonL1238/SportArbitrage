"""Sports arbitrage scanner CLI.

Usage:
    python -m src.scanner [--sport basketball_nba] [--dry-run] [--min-margin 0.01]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from dataclasses import dataclass

from src import config
from src.alerts import send_alerts
from src.arb import filter_stale_odds, filter_started_events, find_arbs
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
from src.local_store import LocalStore
from src.sources.base import SourceAdapter
from src.sources.espn_odds import EspnOddsAdapter
from src.sources.odds_api import OddsApiAdapter
from src.sources.registry import SourceRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@dataclass
class ScanSummary:
    sources: int
    sports: int
    opportunities: int
    credits_used: int
    status: str


class DbStore:
    """Adapter around the Postgres functions used by the scanner."""

    def __init__(self) -> None:
        self.conn = get_connection()

    def init(self) -> None:
        init_db(self.conn)

    def start_scan_run(self, sports: list[str]) -> int:
        return start_scan_run(self.conn, sports)

    def finish_scan_run(
        self,
        run_id: int,
        credits_used: int,
        *,
        status: str,
        error_message: str | None,
        opportunities_found: int,
    ) -> None:
        finish_scan_run(
            self.conn,
            run_id,
            credits_used,
            status=status,
            error_message=error_message,
            opportunities_found=opportunities_found,
        )

    def save_snapshot(self, *args, **kwargs) -> int:
        return save_snapshot(self.conn, *args, **kwargs)

    def save_normalized_odds(self, snapshot_id: int, run_id: int, events: list, *, source_key: str) -> int:
        return save_normalized_odds(self.conn, snapshot_id, run_id, events, source_key=source_key)

    def save_opportunity(self, opp, dedup_key: str, *, scan_run_id: int | None) -> int:
        return save_opportunity(self.conn, opp, dedup_key, scan_run_id=scan_run_id)

    def get_last_alert_for_dedup_key(self, dedup_key: str):
        return get_last_alert_for_dedup_key(self.conn, dedup_key)

    def save_alert_log(self, *args, **kwargs) -> int:
        return save_alert_log(self.conn, *args, **kwargs)

    def close(self) -> None:
        self.conn.close()


def build_default_registry() -> SourceRegistry:
    """Create a registry with the configured source adapters."""
    registry = SourceRegistry()
    source_keys = set(config.ODDS_SOURCES)

    if "odds_api" in source_keys and config.ODDS_API_KEY:
        registry.register(OddsApiAdapter())
    elif "odds_api" in source_keys:
        log.warning("ODDS_SOURCES includes odds_api but ODDS_API_KEY is not set")

    if "espn_odds" in source_keys:
        registry.register(EspnOddsAdapter())

    return registry


def _process_snapshot(
    store,
    adapter: SourceAdapter,
    sport_key: str,
    run_id: int,
    min_margin: float,
    total_stake: float,
) -> tuple[int, int]:
    """Fetch, parse, detect arbs, and persist for one source + sport.

    Returns ``(opportunity_count, credits_used)``.
    """
    snapshot = adapter.fetch_odds(sport_key)
    raw_data = json.loads(snapshot.raw_payload)

    if not raw_data:
        return 0, snapshot.credits_used or 0

    snapshot_id = store.save_snapshot(
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
    store.save_normalized_odds(snapshot_id, run_id, events, source_key=adapter.source_key)
    events = filter_started_events(events)
    events = filter_stale_odds(events, config.MAX_ODDS_AGE_SECONDS)
    opportunities = find_arbs(
        events,
        min_margin=min_margin,
        total_stake=total_stake,
        require_distinct_books=config.REQUIRE_DISTINCT_BOOKS,
        require_complete_outcomes=config.REQUIRE_COMPLETE_OUTCOMES,
        fee_rate=config.DEFAULT_FEE_RATE,
        slippage_bps=config.DEFAULT_SLIPPAGE_BPS,
    )

    for opp in opportunities:
        dedup_key = make_dedup_key(opp)
        opp_id = store.save_opportunity(opp, dedup_key, scan_run_id=run_id)

        last = store.get_last_alert_for_dedup_key(dedup_key)
        send, skip_reason = should_alert(opp, last)

        if send and config.ENABLE_ALERTS:
            success = send_alerts(opp)
            store.save_alert_log(
                opp_id, dedup_key, opp.margin,
                channel="discord" if config.DISCORD_WEBHOOK_URL else "terminal",
                success=success,
            )
        elif send:
            store.save_alert_log(
                opp_id, dedup_key, opp.margin,
                channel="none", skipped=True, skip_reason="alerts_disabled",
            )
        else:
            store.save_alert_log(
                opp_id, dedup_key, opp.margin,
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

    return len(opportunities), snapshot.credits_used or 0


def run_scan(
    sports: list[str] | None = None,
    min_margin: float = config.MIN_ARB_MARGIN,
    total_stake: float = config.DEFAULT_STAKE,
    dry_run: bool = False,
    registry: SourceRegistry | None = None,
) -> ScanSummary:
    sports = sports or config.TARGET_SPORTS

    registry = registry or build_default_registry()
    if len(registry) == 0:
        log.error("No source adapters configured. Set ODDS_API_KEY, ODDS_SOURCES, or register a source.")
        sys.exit(1)

    source_count = len(registry)
    store = DbStore() if config.DATABASE_URL else LocalStore(config.LOCAL_DATA_DIR)
    if not config.DATABASE_URL:
        log.info("DATABASE_URL not set; writing local scan data to %s", config.LOCAL_DATA_DIR)
    store.init()

    run_id = store.start_scan_run(sports)
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
                    opp_count, snapshot_credits = _process_snapshot(
                        store, adapter, sport_key, run_id, min_margin, total_stake,
                    )
                    total_opps += opp_count
                    credits_used += max(0, snapshot_credits)
                except Exception:
                    log.exception("[%s] Failed to fetch/process odds for %s", adapter.source_key, sport_key)
                    errors.append(f"odds:{adapter.source_key}:{sport_key}")
                    continue

                log.info(
                    "[%s] %s: scan complete, %d arbs found",
                    adapter.source_key, sport_key, opp_count,
                )

        error_msg = "; ".join(errors) if errors else None
        store.finish_scan_run(
            run_id, credits_used,
            status="completed",
            error_message=error_msg,
            opportunities_found=total_opps,
        )
    except Exception:
        log.exception("Scan failed")
        try:
            store.finish_scan_run(
                run_id, credits_used,
                status="failed",
                error_message=traceback.format_exc()[-500:],
                opportunities_found=total_opps,
            )
        except Exception:
            log.exception("Failed to record scan failure")
        raise
    finally:
        registry.close_all()
        store.close()

    log.info(
        "Scan complete. Sources: %d, Sports: %d, Opportunities: %d",
        source_count, len(sports), total_opps,
    )
    return ScanSummary(
        sources=source_count,
        sports=len(sports),
        opportunities=total_opps,
        credits_used=credits_used,
        status="completed",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sports arbitrage scanner")
    parser.add_argument("--sport", type=str, help="Scan a single sport key (e.g. basketball_nba)")
    parser.add_argument("--dry-run", action="store_true", help="Check events only, don't fetch odds")
    parser.add_argument("--min-margin", type=float, default=config.MIN_ARB_MARGIN, help="Minimum arb margin (default 0.01)")
    parser.add_argument("--stake", type=float, default=config.DEFAULT_STAKE, help="Total stake for split calculation")
    parser.add_argument("--source", action="append", help="Source key to use. Repeat for multiple sources.")
    parser.add_argument("--watch", action="store_true", help="Continuously scan until interrupted")
    parser.add_argument("--interval", type=int, default=config.SCAN_INTERVAL_SECONDS, help="Seconds between scans in --watch mode")
    args = parser.parse_args()

    sports = [args.sport] if args.sport else None
    if args.source:
        config.ODDS_SOURCES = args.source

    while True:
        run_scan(sports=sports, min_margin=args.min_margin, total_stake=args.stake, dry_run=args.dry_run)
        if not args.watch:
            break
        log.info("Sleeping %ds before next scan", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
