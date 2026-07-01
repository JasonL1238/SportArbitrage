"""Short source smoke tests.

This intentionally fetches only 1-3 events/markets per implemented source and
writes raw fixtures for parser regression tests. It is not a long live run.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config
from src.odds_client import OddsClient
from src.quotes import quotes_from_events
from src.sources.espn_odds import EspnOddsAdapter
from src.sources.kalshi import KalshiQuoteAdapter, parse_kalshi_quotes
from src.sources.polymarket import (
    PolymarketQuoteAdapter,
    parse_polymarket_gamma_quotes,
    parse_polymarket_quotes,
)


FIXTURE_DIR = Path("tests/fixtures/source_smoke")


@dataclass
class SmokeResult:
    source: str
    status: str
    raw_payload_path: str | None
    quote_count: int
    message: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Run short source smoke tests")
    parser.add_argument(
        "--source",
        action="append",
        choices=["espn_odds", "polymarket", "kalshi", "odds_api"],
        help="Source to smoke. Repeatable. Defaults to implemented sources.",
    )
    parser.add_argument("--sport", default="baseball_mlb", help="Sport key for sportsbook-style sources")
    parser.add_argument("--max-items", type=int, default=2, help="Maximum events/markets per source")
    parser.add_argument("--fixtures-dir", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args()

    args.fixtures_dir.mkdir(parents=True, exist_ok=True)
    sources = args.source or ["espn_odds", "polymarket", "kalshi", "odds_api"]
    results: list[SmokeResult] = []

    for source in sources:
        if source == "espn_odds":
            results.append(smoke_espn(args.sport, args.max_items, args.fixtures_dir))
        elif source == "polymarket":
            results.append(smoke_polymarket(args.max_items, args.fixtures_dir))
        elif source == "kalshi":
            results.append(smoke_kalshi(args.max_items, args.fixtures_dir))
        elif source == "odds_api":
            results.append(smoke_odds_api(args.sport, args.max_items, args.fixtures_dir))

    for result in results:
        print(json.dumps(asdict(result), sort_keys=True))

    failed = [r for r in results if r.status == "failed"]
    if failed:
        raise SystemExit(1)


def smoke_espn(sport_key: str, max_items: int, fixtures_dir: Path) -> SmokeResult:
    adapter = EspnOddsAdapter()
    try:
        events = adapter.discover_events(sport_key)[:max_items]
        raw_events = _fetch_espn_summaries(sport_key, events)
        path = _write_fixture(fixtures_dir, "espn_odds", raw_events)
        parsed = adapter.parse_events(raw_events)
        quotes = quotes_from_events(parsed, source="espn_odds", raw_payload_ref=str(path))
        _validate_quotes("espn_odds", quotes)
        _validate_espn_quote_values(raw_events, quotes)
        return SmokeResult("espn_odds", "passed", str(path), len(quotes), f"{len(raw_events)} events")
    except Exception as exc:
        return SmokeResult("espn_odds", "failed", None, 0, str(exc))
    finally:
        adapter.close()


def smoke_polymarket(max_items: int, fixtures_dir: Path) -> SmokeResult:
    adapter = PolymarketQuoteAdapter()
    try:
        events = adapter.fetch_sports_events(max_events=max_items)
        payload = {"gamma_events": events}
        path = _write_fixture(fixtures_dir, "polymarket", payload)
        quotes = parse_polymarket_gamma_quotes(events)
        _validate_quotes("polymarket", quotes)
        _validate_polymarket_gamma_quote_values(events, quotes)
        return SmokeResult("polymarket", "passed", str(path), len(quotes), f"{len(events)} sports events")
    except Exception as exc:
        return SmokeResult("polymarket", "failed", None, 0, str(exc))
    finally:
        adapter.close()


def smoke_kalshi(max_items: int, fixtures_dir: Path) -> SmokeResult:
    adapter = KalshiQuoteAdapter()
    try:
        markets = adapter.fetch_markets(max_markets=max_items, query="sports")
        payload = {"markets": markets}
        path = _write_fixture(fixtures_dir, "kalshi", payload)
        quotes = parse_kalshi_quotes(markets)
        _validate_quotes("kalshi", quotes)
        _validate_kalshi_quote_values(markets, quotes)
        return SmokeResult("kalshi", "passed", str(path), len(quotes), f"{len(markets)} markets")
    except Exception as exc:
        return SmokeResult("kalshi", "failed", None, 0, str(exc))
    finally:
        adapter.close()


def smoke_odds_api(sport_key: str, max_items: int, fixtures_dir: Path) -> SmokeResult:
    if not config.ODDS_API_KEY:
        return SmokeResult("odds_api", "skipped", None, 0, "ODDS_API_KEY not set")

    client = OddsClient()
    try:
        raw, _remaining = client.get_odds(sport_key, markets="h2h", regions=config.REGIONS)
        raw = raw[:max_items]
        path = _write_fixture(fixtures_dir, "odds_api", raw)
        parsed = OddsClient.parse_events(raw)
        quotes = quotes_from_events(parsed, source="odds_api", raw_payload_ref=str(path))
        _validate_quotes("odds_api", quotes)
        _validate_odds_api_quote_values(raw, quotes)
        return SmokeResult("odds_api", "passed", str(path), len(quotes), f"{len(raw)} events")
    except Exception as exc:
        return SmokeResult("odds_api", "failed", None, 0, str(exc))
    finally:
        client.close()


def _fetch_espn_summaries(sport_key: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paths = {
        "basketball_nba": ("basketball", "nba"),
        "americanfootball_nfl": ("football", "nfl"),
        "baseball_mlb": ("baseball", "mlb"),
        "icehockey_nhl": ("hockey", "nhl"),
    }
    if sport_key not in paths:
        return events
    sport, league = paths[sport_key]
    summary_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary"
    out: list[dict[str, Any]] = []
    with httpx.Client(timeout=15.0) as client:
        for event in events:
            event = dict(event)
            event["_sport_key"] = sport_key
            event_id = event.get("id")
            if event_id:
                resp = client.get(summary_url, params={"event": event_id})
                resp.raise_for_status()
                summary = resp.json()
                odds_payload = summary.get("pickcenter") or summary.get("odds") or []
                comps = event.get("competitions") or []
                if comps and odds_payload:
                    comps[0]["odds"] = odds_payload if isinstance(odds_payload, list) else [odds_payload]
            out.append(event)
    return out


def _write_fixture(fixtures_dir: Path, source: str, payload: Any) -> Path:
    path = fixtures_dir / f"{source}_latest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _validate_quotes(source: str, quotes: list) -> None:
    if not quotes:
        raise ValueError(f"{source}: no normalized quotes produced")
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(days=2)
    for quote in quotes:
        required = [
            quote.source,
            quote.sport,
            quote.event_name,
            quote.market_type,
            quote.selection,
            quote.timestamp,
            quote.source_event_id,
        ]
        if any(v in (None, "") for v in required):
            raise ValueError(f"{source}: quote missing required fields: {quote}")
        if quote.decimal_odds is None and quote.price is None:
            raise ValueError(f"{source}: quote missing both decimal_odds and price")
        if quote.timestamp < stale_cutoff or quote.timestamp > now + timedelta(minutes=5):
            raise ValueError(f"{source}: unreasonable quote timestamp {quote.timestamp.isoformat()}")


def _validate_espn_quote_values(raw_events: list[dict[str, Any]], quotes: list) -> None:
    raw_prices = set()
    for event in raw_events:
        for comp in event.get("competitions", []):
            for odds in comp.get("odds", []):
                for side in ("homeTeamOdds", "awayTeamOdds"):
                    ml = odds.get(side, {}).get("moneyLine")
                    if ml is not None:
                        raw_prices.add(round(_american_to_decimal(float(ml)), 6))
                for key in ("overOdds", "underOdds"):
                    if odds.get(key) is not None:
                        raw_prices.add(round(_american_to_decimal(float(odds[key])), 6))
    quote_prices = {round(q.decimal_odds, 6) for q in quotes if q.decimal_odds is not None}
    if raw_prices and not raw_prices.intersection(quote_prices):
        raise ValueError("espn_odds: normalized odds do not match raw payload prices")


def _validate_polymarket_quote_values(books: list[dict[str, Any]], quotes: list) -> None:
    raw_asks = set()
    for book in books:
        for ask in book.get("asks", []):
            raw_asks.add(round(float(ask["price"]), 6))
    quote_prices = {round(q.price, 6) for q in quotes if q.price is not None}
    if raw_asks and not raw_asks.intersection(quote_prices):
        raise ValueError("polymarket: normalized prices do not match raw order book asks")


def _validate_polymarket_gamma_quote_values(events: list[dict[str, Any]], quotes: list) -> None:
    raw_prices = set()
    for event in events:
        for market in event.get("markets", []) or []:
            raw = market.get("outcomePrices")
            if isinstance(raw, str):
                try:
                    prices = json.loads(raw)
                except ValueError:
                    prices = []
            else:
                prices = raw if isinstance(raw, list) else []
            for price in prices:
                value = float(price)
                if 0 < value <= 1:
                    raw_prices.add(round(value, 6))
    quote_prices = {round(q.price, 6) for q in quotes if q.price is not None}
    if raw_prices and not raw_prices.intersection(quote_prices):
        raise ValueError("polymarket: normalized prices do not match raw Gamma prices")


def _validate_kalshi_quote_values(markets: list[dict[str, Any]], quotes: list) -> None:
    raw_prices = set()
    for market in markets:
        for key in ("yes_ask_dollars", "no_ask_dollars"):
            value = market.get(key)
            if value is not None and 0 < float(value) <= 1:
                raw_prices.add(round(float(value), 6))
    quote_prices = {round(q.price, 6) for q in quotes if q.price is not None}
    if raw_prices and not raw_prices.intersection(quote_prices):
        raise ValueError("kalshi: normalized prices do not match raw ask prices")


def _validate_odds_api_quote_values(raw_events: list[dict[str, Any]], quotes: list) -> None:
    raw_prices = {
        round(float(outcome["price"]), 6)
        for event in raw_events
        for bookmaker in event.get("bookmakers", [])
        for market in bookmaker.get("markets", [])
        for outcome in market.get("outcomes", [])
        if outcome.get("price") is not None
    }
    quote_prices = {round(q.decimal_odds, 6) for q in quotes if q.decimal_odds is not None}
    if raw_prices and not raw_prices.intersection(quote_prices):
        raise ValueError("odds_api: normalized odds do not match raw payload prices")


def _american_to_decimal(odds: float) -> float:
    if odds > 0:
        return 1 + odds / 100
    return 1 + 100 / abs(odds)


if __name__ == "__main__":
    main()
