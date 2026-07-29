#!/usr/bin/env python3
"""Ask every candidate source whether it will talk to this machine, and report.

A **script**, deliberately not a test.  What it measures is a fact about the
network from wherever it is run — which endpoints answer, which refuse, and how —
and that changes with the host, the day and the jurisdiction.  A test that
asserted any of it would fail for reasons that have nothing to do with the code.

What it is for is the opposite of a test: producing the evidence behind
``docs/SOURCE_FEASIBILITY.md``, so that a wall is documented once rather than
rediscovered every few months by someone writing an adapter for a host that has
never answered.

    python scripts/probe_sources.py                 # reachability, one line each
    python scripts/probe_sources.py --verbose       # with the first bytes of each reply
    python scripts/probe_sources.py --only kambi    # one family

Nothing here bypasses anything.  A refusal is recorded as a refusal; there is no
retry with different headers, no proxy, no browser.  The whole point of the
output is to say honestly what this machine cannot reach.
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

    # ── refused: recorded so nobody writes an adapter for them again ─────────
    Candidate("blocked", "draftkings",
              "https://sportsbook-nash-usnj.draftkings.com/sites/US-NJ-SB/api/v5/eventgroups/84240",
              {"format": "json"}, note="Akamai; host has also gone NXDOMAIN"),
    Candidate("blocked", "betmgm",
              "https://sports.nj.betmgm.com/cds-api/bettingoffer/fixtures",
              {"x-bwin-accessid": "public", "lang": "en-us", "country": "US"},
              note="Cloudflare Bot Management (cf-ray, __cf_bm)"),
    Candidate("blocked", "caesars",
              "https://api.americanwagering.com/regions/us/locations/nj/brands/czr/sb/v3/sports",
              note="CloudFront WAF"),
    Candidate("blocked", "fanatics",
              "https://api.sportsbook.fanatics.com/api/sportsbook/v1/events",
              note="Akamai, same edge as DraftKings"),
    Candidate("blocked", "bet365",
              "https://www.bet365.com/SportsBook.API/web", {"lid": "1", "zid": "0"},
              note="Cloudflare"),
    Candidate("gone", "espnbet",
              "https://api.espnbet.com/v1/sportsbook/events",
              note="product discontinued; parked host presents a CN=espn.com certificate"),
)


def probe(candidate: Candidate, *, verbose: bool) -> tuple[str, str]:
    """``(verdict, detail)`` for one candidate.  Never raises."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        response = httpx.get(
            candidate.url, params=candidate.params, timeout=TIMEOUT,
            follow_redirects=True, headers=headers,
        )
    except httpx.HTTPError as exc:
        return "UNREACHABLE", f"{type(exc).__name__}: {exc}"

    body = response.text
    if response.status_code != 200:
        marker = _refusal_marker(body)
        return f"HTTP {response.status_code}", marker or body.strip()[:120]

    try:
        payload = json.loads(body)
    except ValueError:
        marker = _refusal_marker(body)
        return "NOT JSON", marker or f"{len(body)} bytes of {response.headers.get('content-type')}"

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
    parser.add_argument("--only", help="probe one family (kambi, exchange, prediction, ...)")
    parser.add_argument("--verbose", action="store_true", help="show the first bytes of each reply")
    args = parser.parse_args(argv)

    chosen = [c for c in CANDIDATES if not args.only or c.family == args.only]
    if not chosen:
        families = sorted({c.family for c in CANDIDATES})
        print(f"no candidates in family {args.only!r}; known: {families}", file=sys.stderr)
        return 1

    print(f"{'family':<11} {'candidate':<18} {'verdict':<12} detail")
    print("-" * 100)
    reachable = 0
    for candidate in chosen:
        verdict, detail = probe(candidate, verbose=args.verbose)
        reachable += verdict == "OK"
        print(f"{candidate.family:<11} {candidate.name:<18} {verdict:<12} {detail}")
        if candidate.note:
            print(f"{'':<11} {'':<18} {'':<12} note: {candidate.note}")
    print("-" * 100)
    print(f"{reachable} of {len(chosen)} candidate(s) answered with usable JSON")
    print(
        "\nA refusal here is recorded, never worked around: no proxy, no browser, no "
        "retry with a different identity.  See docs/SOURCE_FEASIBILITY.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
