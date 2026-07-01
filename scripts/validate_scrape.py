#!/usr/bin/env python3
"""Validate The Odds API scraping: fetch → parse → structure checks.

Usage:
    python scripts/validate_scrape.py              # live scrape (needs ODDS_API_KEY)
    python scripts/validate_scrape.py --mock     # offline validation with fake API
    python scripts/validate_scrape.py --sport basketball_nba --fetch-odds
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow running as `python scripts/validate_scrape.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from src import config
from src.arb import filter_stale_odds, find_arbs
from src.odds_client import OddsClient

_NOW = datetime.now(UTC).isoformat().replace("+00:00", "Z")

MOCK_SPORTS = [
    {"key": "basketball_nba", "group": "Basketball", "title": "NBA", "active": True, "has_outrights": False},
]

MOCK_EVENTS = [
    {
        "id": "mock_evt_1",
        "sport_key": "basketball_nba",
        "commence_time": _NOW,
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
    }
]

MOCK_ODDS = [
    {
        "id": "mock_evt_1",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": _NOW,
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _NOW,
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 2.10},
                            {"name": "Boston Celtics", "price": 1.75},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": _NOW,
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 1.91, "point": -3.5},
                            {"name": "Boston Celtics", "price": 1.91, "point": 3.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _NOW,
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 1.80},
                            {"name": "Boston Celtics", "price": 2.20},
                        ],
                    },
                ],
            },
        ],
    }
]


class ScrapeReport:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append((name, True, detail))

    def fail(self, name: str, detail: str) -> None:
        self.checks.append((name, False, detail))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("SCRAPE VALIDATION REPORT")
        print("=" * 60)
        for name, ok, detail in self.checks:
            status = "PASS" if ok else "FAIL"
            line = f"  [{status}] {name}"
            if detail:
                line += f" — {detail}"
            print(line)
        print("=" * 60)
        total = len(self.checks)
        passed = sum(1 for _, ok, _ in self.checks if ok)
        print(f"Result: {passed}/{total} checks passed")
        print("=" * 60 + "\n")


def _mock_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    headers = {"x-requests-remaining": "499", "x-requests-used": "1"}

    if path.endswith("/sports/"):
        return httpx.Response(200, json=MOCK_SPORTS, headers=headers)
    if path.endswith("/events/"):
        return httpx.Response(200, json=MOCK_EVENTS, headers=headers)
    if path.endswith("/odds/"):
        return httpx.Response(200, json=MOCK_ODDS, headers=headers)

    return httpx.Response(404, json={"message": f"unmocked path: {path}"})


def _validate_parsed_events(report: ScrapeReport, events, *, label: str) -> int:
    """Return total outcome rows parsed."""
    if not events:
        report.fail(f"{label}: events parsed", "zero events returned")
        return 0

    report.ok(f"{label}: events parsed", f"{len(events)} event(s)")

    total_outcomes = 0
    total_bookmakers = 0
    market_keys: set[str] = set()

    for evt in events:
        if not evt.id:
            report.fail(f"{label}: event id", "missing event id")
        if not evt.home_team or not evt.away_team:
            report.fail(f"{label}: team names", f"incomplete teams for {evt.id}")
        else:
            report.ok(f"{label}: teams ({evt.home_team} vs {evt.away_team})", evt.id)

        for bm in evt.bookmakers:
            total_bookmakers += 1
            if not bm.key or not bm.title:
                report.fail(f"{label}: bookmaker metadata", f"bad book on {evt.id}")
                continue
            for mkt in bm.markets:
                market_keys.add(mkt.key)
                if not mkt.last_update:
                    report.fail(f"{label}: last_update", f"missing on {bm.key}/{mkt.key}")
                for outcome in mkt.outcomes:
                    total_outcomes += 1
                    if outcome.price <= 0:
                        report.fail(
                            f"{label}: odds price",
                            f"invalid price {outcome.price} for {outcome.name}",
                        )

    report.ok(f"{label}: bookmakers", str(total_bookmakers))
    report.ok(f"{label}: outcomes", str(total_outcomes))
    report.ok(f"{label}: markets seen", ", ".join(sorted(market_keys)) or "none")

    fresh = filter_stale_odds(events, config.MAX_ODDS_AGE_SECONDS)
    fresh_books = sum(len(e.bookmakers) for e in fresh)
    report.ok(f"{label}: stale filter", f"{fresh_books} bookmaker(s) still fresh")

    arbs = find_arbs(fresh, min_margin=config.MIN_ARB_MARGIN)
    report.ok(f"{label}: arb scan", f"{len(arbs)} opportunit(ies) above {config.MIN_ARB_MARGIN:.0%} margin")

    return total_outcomes


def run_mock_validation(sport: str, fetch_odds: bool) -> ScrapeReport:
    report = ScrapeReport()
    report.ok("mode", "mock (offline)")

    transport = httpx.MockTransport(_mock_handler)
    client = OddsClient(api_key="mock-key", base_url="https://api.the-odds-api.com/v4")
    client._client = httpx.Client(transport=transport, base_url=client.base_url)

    try:
        sports = client.get_sports()
        report.ok("GET /sports/", f"{len(sports)} sport(s)")

        events_raw = client.get_events(sport)
        report.ok(f"GET /sports/{sport}/events/", f"{len(events_raw)} event(s)")

        if fetch_odds:
            raw_odds, remaining = client.get_odds(sport, markets="h2h,spreads", regions="us")
            report.ok(f"GET /sports/{sport}/odds/", f"{len(raw_odds)} event(s), credits left: {remaining}")
            parsed = OddsClient.parse_events(raw_odds)
            _validate_parsed_events(report, parsed, label="mock odds")
        else:
            report.ok("odds fetch", "skipped (use --fetch-odds to test paid endpoint)")
    except Exception as exc:
        report.fail("mock scrape", str(exc))
    finally:
        client.close()

    return report


def run_live_validation(sport: str, fetch_odds: bool) -> ScrapeReport:
    report = ScrapeReport()

    if not config.ODDS_API_KEY:
        report.fail("ODDS_API_KEY", "not set — copy .env.example to .env and add your key")
        return report

    report.ok("mode", "live")
    report.ok("ODDS_API_KEY", f"set ({config.ODDS_API_KEY[:6]}…)")
    report.ok("API base", config.ODDS_API_BASE_URL)

    try:
        with OddsClient() as client:
            sports = client.get_sports()
            active = [s for s in sports if s.get("active")]
            report.ok("GET /sports/", f"{len(sports)} total, {len(active)} active")
            if client.credits.remaining is not None:
                report.ok("credits after /sports/", str(client.credits.remaining))

            events_raw = client.get_events(sport)
            report.ok(f"GET /sports/{sport}/events/", f"{len(events_raw)} upcoming event(s)")

            if events_raw:
                sample = events_raw[0]
                report.ok(
                    "sample event",
                    f"{sample.get('home_team')} vs {sample.get('away_team')} @ {sample.get('commence_time')}",
                )

            if not fetch_odds:
                report.ok("odds fetch", "skipped (use --fetch-odds; costs API credits)")
                return report

            raw_odds, remaining = client.get_odds(sport)
            report.ok(
                f"GET /sports/{sport}/odds/",
                f"{len(raw_odds)} event(s) with odds, credits remaining: {remaining}",
            )

            if not raw_odds:
                report.fail("odds data", "empty response — sport may be off-season or no lines posted")
                return report

            # Spot-check raw JSON structure
            first = raw_odds[0]
            required = ["id", "sport_key", "home_team", "away_team", "bookmakers"]
            missing = [k for k in required if k not in first]
            if missing:
                report.fail("raw JSON schema", f"missing fields: {missing}")
            else:
                report.ok("raw JSON schema", "required fields present")

            bm_count = len(first.get("bookmakers", []))
            if bm_count == 0:
                report.fail("bookmakers in response", "first event has zero bookmakers")
            else:
                report.ok("bookmakers in response", f"{bm_count} on first event")

            parsed = OddsClient.parse_events(raw_odds)
            outcomes = _validate_parsed_events(report, parsed, label="live odds")

            if outcomes == 0:
                report.fail("parse pipeline", "no outcomes extracted from response")

            # Save sample for inspection
            sample_path = Path("scrape_sample.json")
            sample_path.write_text(json.dumps(raw_odds[:2], indent=2, default=str))
            report.ok("sample saved", str(sample_path.resolve()))

    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200]
        report.fail("HTTP error", f"{exc.response.status_code} {body}")
    except httpx.RequestError as exc:
        report.fail("network error", str(exc))
    except Exception as exc:
        report.fail("scrape error", str(exc))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate odds scraping pipeline")
    parser.add_argument("--sport", default="basketball_nba", help="Sport key to test")
    parser.add_argument("--mock", action="store_true", help="Run offline with mocked API responses")
    parser.add_argument("--fetch-odds", action="store_true", help="Also fetch odds (costs API credits)")
    args = parser.parse_args()

    print(f"Validating scrape for sport={args.sport!r}, fetch_odds={args.fetch_odds}")

    if args.mock:
        report = run_mock_validation(args.sport, args.fetch_odds)
    else:
        report = run_live_validation(args.sport, args.fetch_odds)

    report.print_summary()
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
