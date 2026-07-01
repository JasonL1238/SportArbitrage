#!/usr/bin/env python3
"""Replay a stored raw snapshot through the parser pipeline.

Reads a snapshot from the database (by id) or from a JSON file, parses it
through the configured source adapter, runs stale-odds filtering and arb
detection, and prints the results.  No network requests are made.

Usage:
    python scripts/replay_snapshot.py --id 42
    python scripts/replay_snapshot.py --file snapshots/sample.json --source odds_api
    python scripts/replay_snapshot.py --id 42 --min-margin 0.005
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.arb import filter_stale_odds, find_arbs
from src.odds_client import OddsClient


def replay_from_db(snapshot_id: int, min_margin: float, max_age: int, total_stake: float) -> None:
    from src.db import get_connection, get_snapshot_by_id, init_db

    conn = get_connection()
    init_db(conn)
    snap = get_snapshot_by_id(conn, snapshot_id)
    conn.close()

    if not snap:
        print(f"Snapshot {snapshot_id} not found.", file=sys.stderr)
        sys.exit(1)

    raw = snap["response_json"]
    if isinstance(raw, str):
        raw = json.loads(raw)

    source_key = snap.get("source_key", "odds_api")
    sport_key = snap.get("sport_key", "unknown")
    parser_version = snap.get("parser_version", "?")

    print(f"Replaying snapshot #{snapshot_id} (source={source_key}, sport={sport_key}, parser_v={parser_version})")
    print(f"  Fetched at: {snap.get('fetched_at')}")
    print(f"  Events in payload: {len(raw)}")
    _run_pipeline(raw, min_margin, max_age, total_stake)


def replay_from_file(path: str, min_margin: float, max_age: int, total_stake: float) -> None:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "response_json" in data:
        data = data["response_json"]
    print(f"Replaying from file: {path}")
    print(f"  Events in payload: {len(data)}")
    _run_pipeline(data, min_margin, max_age, total_stake)


def _run_pipeline(raw: list[dict], min_margin: float, max_age: int, total_stake: float) -> None:
    events = OddsClient.parse_events(raw)
    print(f"  Parsed events: {len(events)}")

    total_books = sum(len(e.bookmakers) for e in events)
    print(f"  Total bookmakers: {total_books}")

    if max_age > 0:
        events = filter_stale_odds(events, max_age)
        fresh_books = sum(len(e.bookmakers) for e in events)
        print(f"  After stale filter ({max_age}s): {fresh_books} bookmakers")

    opps = find_arbs(events, min_margin=min_margin, total_stake=total_stake)
    print(f"\n  Arb opportunities found: {len(opps)}")

    for i, opp in enumerate(opps, 1):
        books = ", ".join(f"{b.outcome_name}@{b.bookmaker_title}({b.decimal_odds})" for b in opp.best_outcomes)
        print(f"\n  [{i}] {opp.home_team} vs {opp.away_team}")
        print(f"      Market: {opp.market_key}" + (f" ({opp.point})" if opp.point else ""))
        print(f"      Margin: {opp.margin_pct}")
        print(f"      Profit on ${opp.total_stake:.0f}: ${opp.guaranteed_profit:.2f}")
        print(f"      Legs: {books}")
        for b, s in zip(opp.best_outcomes, opp.stakes):
            print(f"        {b.outcome_name} @ {b.bookmaker_title}: ${s:.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a stored snapshot through the arb pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", type=int, help="Snapshot ID from the database")
    group.add_argument("--file", type=str, help="Path to a JSON snapshot file")
    parser.add_argument("--min-margin", type=float, default=0.01)
    parser.add_argument("--max-age", type=int, default=0, help="Max odds age in seconds (0=no filter)")
    parser.add_argument("--stake", type=float, default=100.0)
    args = parser.parse_args()

    if args.id is not None:
        replay_from_db(args.id, args.min_margin, args.max_age, args.stake)
    else:
        replay_from_file(args.file, args.min_margin, args.max_age, args.stake)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
