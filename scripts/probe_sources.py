#!/usr/bin/env python3
"""Ask every candidate source whether it will talk to this machine, and report.

A **script**, deliberately not a test.  What it measures is a fact about the
network from wherever it is run — which endpoints answer, which refuse, and how —
and that changes with the host, the day, the jurisdiction, and the transport.

Default transport matches the collector: Chrome TLS impersonation via
``curl_cffi``, plus ``ODDS_HTTP_PROXY`` when set.  Use this to decide which
blocked books are ready for an adapter.

    python scripts/probe_sources.py                 # reachability, one line each
    python scripts/probe_sources.py --verbose       # with the first bytes of each reply
    python scripts/probe_sources.py --only blocked  # DK / Caesars / Fanatics / bet365
    python scripts/probe_sources.py --plain         # old plain httpx baseline
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

# Runnable as `python scripts/probe_sources.py` from the repository root, which
# is how it will actually be run; without this the script only works under
# `python -m`, and a recon tool that needs its own incantation does not get used.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sources._common import USER_AGENT  # noqa: E402
from src.sources.transport import proxy_url  # noqa: E402

TIMEOUT = 25.0


@dataclass(frozen=True)
class Candidate:
    """One endpoint worth asking, and what a useful answer would look like."""

    family: str
    name: str
    url: str
    params: dict[str, str] = field(default_factory=dict)
    expect: str = ""
    """What the payload should contain if this source is usable at all."""

    note: str = ""
    counts: Callable[[Any], str] | None = None
    """Turns a decoded payload into a one-line summary of what arrived."""


def _len(path: str) -> Callable[[Any], str]:
    """Report the length of a nested list, given a dotted path."""

    def count(payload: Any) -> str:
        node = payload
        for step in path.split("."):
            if step == "":
                continue
            if isinstance(node, dict):
                node = node.get(step)
            else:
                return "unexpected shape"
        return f"{len(node)} item(s)" if isinstance(node, list) else "no list at that path"

    return count


#: Every candidate probed while planning the expansion, kept whether or not it
#: worked.  The ones that fail are the point: a candidate with no record is a
#: candidate somebody will try again.
CANDIDATES: tuple[Candidate, ...] = (
    # ── reachable, and adapted ───────────────────────────────────────────────
    Candidate("kambi", "betrivers-il",
              "https://eu-offering-api.kambicdn.com/offering/v2018/rsiusil/listView/baseball/mlb.json",
              {"lang": "en_US", "market": "US-IL"}, expect="events", counts=_len("events"),
              note="registered as betrivers_kambi"),
    Candidate("kambi", "leovegas",
              "https://eu-offering-api.kambicdn.com/offering/v2018/leo/listView/baseball/mlb.json",
              {"lang": "en_GB", "market": "GB"}, expect="events", counts=_len("events"),
              note="registered as leovegas_kambi; prices independently"),
    Candidate("kambi", "betrivers-nj",
              "https://eu-offering-api.kambicdn.com/offering/v2018/rsiusnj/listView/baseball/mlb.json",
              {"lang": "en_US", "market": "US-NJ"}, expect="events", counts=_len("events"),
              note="MIRROR of betrivers-il — byte-identical listView; must not be registered"),
    Candidate("kambi", "kambi-reference",
              "https://eu-offering-api.kambicdn.com/offering/v2018/kambi/listView/baseball/mlb.json",
              {"lang": "en_GB", "market": "GB"}, expect="events", counts=_len("events"),
              note="MIRROR of betrivers-il"),
    Candidate("exchange", "matchbook",
              "https://www.matchbook.com/edge/rest/events",
              {"sport-ids": "3", "states": "open", "per-page": "3", "odds-type": "DECIMAL",
               "include-prices": "true", "exchange-type": "back-lay"},
              expect="events", counts=_len("events"),
              note="use www.matchbook.com; apiclient.matchbook.com answers 530"),
    Candidate("exchange", "smarkets",
              "https://api.smarkets.com/v3/events/",
              {"type": "baseball_match", "state": "upcoming", "limit": "3"},
              expect="events", counts=_len("events")),
    Candidate("exchange", "sxbet",
              "https://api.sx.bet/markets/active", {"chainVersion": "SXR", "sportIds": "3"},
              expect="markets", counts=_len("data.markets")),
    Candidate("prediction", "kalshi",
              "https://api.elections.kalshi.com/trade-api/v2/markets",
              {"series_ticker": "KXMLBGAME", "limit": "3", "status": "open"},
              expect="markets", counts=_len("markets"),
              note="rate limits an unpaced probe with 429"),
    Candidate("prediction", "polymarket",
              "https://gamma-api.polymarket.com/events",
              {"limit": "3", "closed": "false", "active": "true", "tag_slug": "mlb"},
              expect="markets", counts=lambda p: f"{len(p)} event(s)" if isinstance(p, list) else "?"),
    Candidate("sportsbook", "bovada",
              "https://www.bovada.lv/services/sports/event/coupon/events/A/description/baseball/mlb",
              {"marketFilterId": "def", "lang": "en"}, expect="events",
              counts=lambda p: f"{sum(len(g.get('events') or []) for g in p)} event(s)"
              if isinstance(p, list) else "?"),
    Candidate("sportsbook", "betmgm",
              "https://www.il.betmgm.com/cds-api/bettingoffer/fixtures",
              {"x-bwin-accessid": "ZTg4YWEwMTgtZTlhYy00MWRkLWIzYWYtZjMzODI5ZDE0Mjc5",
               "lang": "en-us", "country": "US", "userCountry": "US",
               "subdivision": "US-Illinois", "fixtureTypes": "Standard",
               "state": "Latest", "offerMapping": "Filtered",
               "offerCategories": "Gridable", "fixtureCategories": "Gridable",
               "sortBy": "Tags", "take": "3", "sportIds": "23"},
              expect="fixtures", counts=_len("fixtures"),
              note="registered as betmgm"),
    Candidate("sportsbook", "cloudbet",
              "https://www.cloudbet.com/sports-api/c/v6/sports/events",
              {"limit": "3", "sport": "baseball"}, expect="sports",
              counts=lambda p: (
                  f"{sum(len(c.get('events') or []) for s in (p.get('sports') or []) for c in (s.get('competitions') or []))} event(s)"
                  if isinstance(p, dict) else "?"
              ),
              note="registered as cloudbet; detail hop needed for markets"),
    Candidate("sportsbook", "onexbet",
              "https://1xbet.com/service-api/LineFeed/Get1x2_VZip",
              {"sports": "5", "count": "5", "lng": "en", "tf": "2200000",
               "tz": "0", "mode": "4", "country": "1"},
              expect="Value", counts=_len("Value"),
              note="registered as onexbet"),

    # ── reopen candidates (blocked under plain httpx; try impersonation) ────
    Candidate("blocked", "draftkings",
              "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/v5/eventgroups/84240",
              {"format": "json"},
              note="registered as draftkings; Akamai 403 from CA without ODDS_HTTP_PROXY"),
    Candidate("blocked", "draftkings-sportscontent",
              "https://sportsbook-nash.draftkings.com/sites/US-SB/api/sportscontent/"
              "controldata/client/desktop/visit/locale/en-us/"
              "markets/v3/marketsByEventGroupIds",
              {"isBatchable": "false", "eventGroupIds": "84240"},
              note="path a real browser hits for MLB markets"),
    Candidate("blocked", "caesars",
              "https://api.americanwagering.com/regions/us/locations/nj/brands/czr/sb/v3/sports",
              note="CloudFront WAF; first-party adapter still pending proxy open"),
    Candidate("blocked", "fanatics",
              "https://sportsbook.fanatics.com/",
              note="old api.sportsbook.fanatics.com is NXDOMAIN; site is Akamai bot-manager"),
    Candidate("blocked", "bet365",
              "https://www.bet365.com/SportsBook.API/web", {"lid": "1", "zid": "0"},
              note="Cloudflare under plain httpx"),
    Candidate("hardrock", "hardrock-ladder",
              "https://api.hardrocksportsbook.com/sportsbook/v1/api/getRootLadder",
              expect="PriceAdjustmentDetailsResponse",
              note="registered as hardrock; ladder reachable from CA"),
    Candidate("hardrock", "hardrock-tree",
              "https://api.hardrocksportsbook.com/sportsbook/api/public/events/tree",
              {"segment": "nj"}, expect="betSync",
              note="tree reachable; GraphQL events empty from CA without proxy"),
    Candidate("kambi", "ballybet",
              "https://eu-offering-api.kambicdn.com/offering/v2018/ballybet/listView/baseball/mlb.json",
              {"lang": "en_US", "market": "US-NJ"}, expect="events",
              note="429 No access from CA; AN an_bally is the interim path"),
    Candidate("gone", "espnbet",
              "https://api.espnbet.com/v1/sportsbook/events",
              note="product discontinued; parked host presents a CN=espn.com certificate"),
)


def probe(candidate: Candidate, *, verbose: bool, plain: bool = False) -> tuple[str, str]:
    """``(verdict, detail)`` for one candidate.  Never raises."""
    from src.sources.browser import browser_enabled, build_browser_client
    from src.sources.transport import build_default_client

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    try:
        if plain:
            response = httpx.get(
                candidate.url, params=candidate.params, timeout=TIMEOUT,
                follow_redirects=True, headers=headers,
            )
            status = response.status_code
            body = response.text
            content_type = response.headers.get("content-type")
        else:
            seed = None
            # Seed DK/Caesars with their sportsbook origin so the edge can mint cookies.
            if "draftkings.com" in candidate.url:
                seed = "https://sportsbook.draftkings.com/"
            elif "americanwagering.com" in candidate.url:
                seed = "https://www.caesars.com/sportsbook-and-casino"
            session = (
                build_browser_client(timeout=TIMEOUT, seed_url=seed)
                if browser_enabled()
                else build_default_client(timeout=TIMEOUT)
            )
            try:
                response = session.get(
                    candidate.url, params=candidate.params or None, headers=headers,
                )
            finally:
                session.close()
            status = response.status_code
            body = response.text
            content_type = response.headers.get("content-type")
    except Exception as exc:
        return "UNREACHABLE", f"{type(exc).__name__}: {exc}"

    if status != 200:
        marker = _refusal_marker(body)
        return f"HTTP {status}", marker or body.strip()[:120]

    try:
        payload = json.loads(body)
    except ValueError:
        marker = _refusal_marker(body)
        return "NOT JSON", marker or f"{len(body)} bytes of {content_type}"

    detail = candidate.counts(payload) if candidate.counts else f"{len(body)} bytes"
    if verbose:
        detail += f" | {body[:160]!r}"
    return "OK", detail


def _refusal_marker(body: str) -> str | None:
    """Name the wall, when the body says which one it is."""
    lowered = body[:20_000].lower()
    for marker, label in (
        ("access denied", "Akamai/edge 'Access Denied'"),
        ("reference #", "Akamai reference id"),
        ("cloudflare", "Cloudflare"),
        ("cf-ray", "Cloudflare"),
        ("request blocked", "CloudFront WAF"),
        ("not available in your region", "geo-restricted"),
        ("perimeterx", "PerimeterX"),
        ("datadome", "DataDome"),
        ("captcha", "CAPTCHA challenge"),
    ):
        if marker in lowered:
            return label
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/probe_sources.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--only", help="probe one family (kambi, exchange, prediction, blocked, ...)")
    parser.add_argument("--verbose", action="store_true", help="show the first bytes of each reply")
    parser.add_argument(
        "--plain", action="store_true",
        help="use plain httpx (old baseline) instead of Chrome impersonation",
    )
    parser.add_argument(
        "--browser", action="store_true",
        help="use Playwright Chromium (sets ODDS_FETCH_MODE=browser for this run)",
    )
    args = parser.parse_args(argv)
    if args.browser:
        import os
        os.environ["ODDS_FETCH_MODE"] = "browser"

    chosen = [c for c in CANDIDATES if not args.only or c.family == args.only]
    if not chosen:
        families = sorted({c.family for c in CANDIDATES})
        print(f"no candidates in family {args.only!r}; known: {families}", file=sys.stderr)
        return 1

    if args.plain:
        mode = "plain httpx"
    elif args.browser:
        mode = "Playwright Chromium"
    else:
        mode = "curl_cffi Chrome impersonation"
    proxy = proxy_url()
    print(f"transport: {mode}" + (f" via {proxy}" if proxy else " (no proxy)"))
    print(f"{'family':<11} {'candidate':<28} {'verdict':<12} detail")
    print("-" * 110)
    reachable = 0
    for candidate in chosen:
        verdict, detail = probe(candidate, verbose=args.verbose, plain=args.plain)
        reachable += verdict == "OK"
        print(f"{candidate.family:<11} {candidate.name:<28} {verdict:<12} {detail}")
        if candidate.note:
            print(f"{'':<11} {'':<28} {'':<12} note: {candidate.note}")
    print("-" * 110)
    print(f"{reachable} of {len(chosen)} candidate(s) answered with usable JSON")
    print("\nNext lever on a refusal: ODDS_HTTP_PROXY, then browser-cookie bootstrap / Playwright.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
