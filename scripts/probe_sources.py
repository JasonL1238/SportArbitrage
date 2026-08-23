#!/usr/bin/env python3
"""Ask every candidate source whether it will talk to this machine, and report.

A **script**, deliberately not a test.  What it measures is a fact about the
network from wherever it is run — which endpoints answer, which refuse, and how —
and that changes with the host, the day, the jurisdiction, and the transport.

Default transport matches the collector: Chrome TLS impersonation via
``curl_cffi``, plus the selected state's proxy when configured. Use this to
decide which blocked books are ready for an adapter.

    python scripts/probe_sources.py --state IL      # active retail routes
    python scripts/probe_sources.py --state PA --template-only
    python scripts/probe_sources.py --verbose       # with the first bytes of each reply
    python scripts/probe_sources.py --only blocked  # DK / Caesars / Fanatics / bet365
    python scripts/probe_sources.py --plain         # old plain httpx baseline
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

# Runnable as `python scripts/probe_sources.py` from the repository root, which
# is how it will actually be run; without this the script only works under
# `python -m`, and a recon tool that needs its own incantation does not get used.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings
from src.egress import is_recent, load_detection
from src.jurisdictions import RouteStatus, jurisdiction
from src.probe_cache import ProbeCache, ProbeStatus
from src.sources._common import USER_AGENT
from src.sources.transport import proxy_url

TIMEOUT = 25.0


@dataclass(frozen=True)
class Candidate:
    """One endpoint worth asking, and what a useful answer would look like."""

    family: str
    name: str
    url: str
    params: dict[str, str] = field(default_factory=dict)
    note: str = ""
    counts: Callable[[Any], str] | None = None
    """Turns a decoded payload into a one-line summary of what arrived."""
    source_key: str = ""
    """Stable registered identity when this is an active-state probe."""


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
RESEARCH_CANDIDATES: tuple[Candidate, ...] = (
    # ── reachable, and adapted ───────────────────────────────────────────────
    Candidate("kambi", "betrivers-il",
              "https://eu-offering-api.kambicdn.com/offering/v2018/rsiusil/listView/baseball/mlb.json",
              {"lang": "en_US", "market": "US-IL"}, counts=_len("events"),
              note="registered as betrivers_kambi"),
    Candidate("kambi", "leovegas",
              "https://eu-offering-api.kambicdn.com/offering/v2018/leo/listView/baseball/mlb.json",
              {"lang": "en_GB", "market": "GB"}, counts=_len("events"),
              note="registered as leovegas_kambi; prices independently"),
    Candidate("kambi", "betrivers-nj",
              "https://eu-offering-api.kambicdn.com/offering/v2018/rsiusnj/listView/baseball/mlb.json",
              {"lang": "en_US", "market": "US-NJ"}, counts=_len("events"),
              note="MIRROR of betrivers-il — byte-identical listView; must not be registered"),
    Candidate("kambi", "kambi-reference",
              "https://eu-offering-api.kambicdn.com/offering/v2018/kambi/listView/baseball/mlb.json",
              {"lang": "en_GB", "market": "GB"}, counts=_len("events"),
              note="MIRROR of betrivers-il"),
    Candidate("exchange", "matchbook",
              "https://www.matchbook.com/edge/rest/events",
              {"sport-ids": "3", "states": "open", "per-page": "3", "odds-type": "DECIMAL",
               "include-prices": "true", "exchange-type": "back-lay"},
              counts=_len("events"),
              note="use www.matchbook.com; apiclient.matchbook.com answers 530"),
    Candidate("exchange", "smarkets",
              "https://api.smarkets.com/v3/events/",
              {"type": "baseball_match", "state": "upcoming", "limit": "3"},
              counts=_len("events")),
    Candidate("exchange", "sxbet",
              "https://api.sx.bet/markets/active", {"chainVersion": "SXR", "sportIds": "3"},
              counts=_len("data.markets")),
    Candidate("prediction", "kalshi",
              "https://api.elections.kalshi.com/trade-api/v2/markets",
              {"series_ticker": "KXMLBGAME", "limit": "3", "status": "open"},
              counts=_len("markets"),
              note="rate limits an unpaced probe with 429"),
    # Kept as a measurement, not as a route: the offshore venue was deregistered
    # on 2026-08-13 in favour of the US entity below.  A candidate with no record
    # is a candidate somebody tries again.
    Candidate("prediction", "polymarket-offshore",
              "https://gamma-api.polymarket.com/events",
              {"limit": "3", "closed": "false", "active": "true", "tag_slug": "mlb"},
              counts=lambda p: f"{len(p)} event(s)" if isinstance(p, list) else "?",
              note="DEREGISTERED 2026-08-13 — answers, but is not US-executable "
                   "and is no longer collected; polymarket_us replaced it"),
    # Polymarket **US** (QCX), and the host is the whole finding.  The 2026-08-09
    # measurement recorded this venue as credential-gated on the strength of
    # ``api.polymarket.us`` answering 401 "Missing required API key headers" — but
    # that is the vendor's *authenticated* host.  ``gateway.polymarket.us`` is the
    # public keyless one and answers anonymously; re-measured 2026-08-12 from an
    # Illinois egress, 200 on all three below while ``api.`` still 401s in the same
    # session.  Keep the 401 candidate beside them: it is what stops the next agent
    # concluding the venue is walled after asking one host.
    Candidate("prediction", "polymarket-us-leagues",
              "https://gateway.polymarket.us/v2/leagues", {},
              counts=_len("leagues"),
              note="public/keyless; 50 leagues, all isOperational — mlb, nba, nfl, "
                   "nhl, wnba, epl, ucl plus esports (cs2, lol, dota2, valorant)"),
    Candidate("prediction", "polymarket-us-events",
              "https://gateway.polymarket.us/v2/leagues/mlb/events", {"limit": "3"},
              counts=_len("events"),
              note="game-level markets (15/event) with sportradarGameId; the "
                   "surface an adapter would read"),
    Candidate("prediction", "polymarket-us-markets",
              "https://gateway.polymarket.us/v1/markets",
              {"limit": "3", "active": "true", "closed": "false"},
              counts=_len("markets"),
              note="FUTURES only by default — not the pregame game lines; "
                   "sportsMarketTypes=MONEYLINE is refused with 400"),
    Candidate("prediction", "polymarket-us-authenticated-host",
              "https://api.polymarket.us/v1/markets", {"limit": "3"},
              note="401 'Missing required API key headers' — expected and kept: "
                   "this is the host the 2026-08-09 probe mistook for the venue"),
    Candidate("sportsbook", "bovada",
              "https://www.bovada.lv/services/sports/event/coupon/events/A/description/baseball/mlb",
              {"marketFilterId": "def", "lang": "en"},
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
              counts=_len("fixtures"),
              note="registered as betmgm"),
    Candidate("sportsbook", "cloudbet",
              "https://www.cloudbet.com/sports-api/c/v6/sports/events",
              {"limit": "3", "sport": "baseball"},
              counts=lambda p: (
                  f"{sum(len(c.get('events') or []) for s in (p.get('sports') or []) for c in (s.get('competitions') or []))} event(s)"
                  if isinstance(p, dict) else "?"
              ),
              note="registered as cloudbet; detail hop needed for markets"),
    Candidate("sportsbook", "onexbet",
              "https://1xbet.com/service-api/LineFeed/Get1x2_VZip",
              {"sports": "5", "count": "5", "lng": "en", "tf": "2200000",
               "tz": "0", "mode": "4", "country": "1"},
              counts=_len("Value"),
              note="registered as onexbet"),

    # ── reopen candidates (blocked under plain httpx; try impersonation) ────
    Candidate("blocked", "draftkings-sportscontent",
              "https://sportsbook-nash.draftkings.com/sites/US-SB/api/sportscontent/"
              "controldata/client/desktop/visit/locale/en-us/"
              "markets/v3/marketsByEventGroupIds",
              {"isBatchable": "false", "eventGroupIds": "84240"},
              note="path a real browser hits for MLB markets"),
    Candidate("blocked", "caesars",
              "https://api.americanwagering.com/regions/us/locations/nj/brands/czr/sb/v3/sports-menu",
              note="AWS WAF token gate, not egress: real Chrome gets 200 here, "
                   "curl_cffi gets 403 from the same exit IP (2026-08-12)"),
    Candidate("blocked", "fanatics",
              "https://sportsbook.fanatics.com/",
              note="old api.sportsbook.fanatics.com is NXDOMAIN; site is Akamai bot-manager"),
    # The stateless origin, which is what every "403 Cloudflare" reading was
    # taken against.  The **state** host answers: 2026-08-14 from IL egress,
    # ``www.il.bet365.com`` returned 200 with the full 41.8 KB sportsbook shell
    # under this same transport, and its pull-pod XHR served 754 prices with no
    # token at all.  Kept pointed here on purpose — this row is the control that
    # says the refusal belongs to the stateless origin rather than to the book.
    Candidate("blocked", "bet365",
              "https://www.bet365.com/SportsBook.API/web", {"lid": "1", "zid": "0"},
              note="stateless origin; the per-state host answers (state-routing.md 2026-08-14)"),
    Candidate("hardrock", "hardrock-ladder",
              "https://api.hardrocksportsbook.com/sportsbook/v1/api/getRootLadder",
              note="registered as hardrock; ladder reachable from CA"),
    Candidate("hardrock", "hardrock-tree",
              "https://api.hardrocksportsbook.com/sportsbook/api/public/events/tree",
              {"segment": "nj"},
              note="tree reachable; GraphQL events empty from CA without proxy"),
    Candidate("kambi", "ballybet",
              "https://eu-offering-api.kambicdn.com/offering/v2018/ballybet/listView/baseball/mlb.json",
              {"lang": "en_US", "market": "US-NJ"},
              note="429 No access from CA; AN an_bally is the interim path"),
    Candidate("gone", "espnbet",
              "https://api.espnbet.com/v1/sportsbook/events",
              note="product discontinued; parked host presents a CN=espn.com certificate"),
)


def state_candidates(state: str) -> tuple[Candidate, ...]:
    """Registered retail sources whose constructor routes come from the map."""
    configured = jurisdiction(state)
    candidates: list[Candidate] = []
    for key, route in configured.routes.items():
        if route.status is RouteStatus.UNAVAILABLE:
            continue
        config = route.config
        if key == "fanduel":
            url = f"https://sbapi.{config['state']}.sportsbook.fanduel.com/api/content-managed-page"
        elif key in ("betrivers_kambi", "betparx_kambi"):
            url = (
                "https://eu-offering-api.kambicdn.com/offering/v2018/"
                f"{config['operator']}/listView/baseball/mlb.json"
            )
        elif key == "betmgm":
            url = f"{config['base_url']}/cds-api/bettingoffer/fixtures"
        elif key == "draftkings":
            url = f"{config['content_base_url']}/markets"
        elif key == "bet365":
            # The homepage pod, which is the board this adapter actually reads.
            # Taken verbatim from the application's own request rather than
            # composed: this host answered 403 to about a hundred probing
            # requests on 2026-08-14 and the last of those was fuzzing ``pd``.
            url = (
                f"{config['base_url']}/pullpodapi/gethomepagepods"
                "?lid=32&zid=0&pd=%23HO%23COL1%23&cid=198&cstid=1&tcstid=1"
                f"&crid=54&cgid=3&ctid=198&csid={config.get('csid', '')}"
            )
        elif key == "caesars":
            # ``sb/v3/sports-menu`` is the only odds-side path on this host the
            # real page has ever been observed requesting, and it answers 200 to
            # a browser.  It replaces ``/sports``, which was never observed
            # anywhere: a probe against an invented path does not merely fail,
            # it files a verdict.  ``src/probe_cache.py`` keys that verdict on
            # ``(caesars, <state>, <egress fingerprint>)`` and holds it for 60
            # days, so one guess stood as this book's recorded answer for two
            # months.  See docs/evidence/state-routing.md § Caesars (2026-08-15).
            #
            # Expect ``403`` regardless: the gate is an AWS WAF token the
            # probe's plain client does not carry.  That is the honest result
            # for this transport, and it is now a refusal from a real path
            # rather than a refusal from a fictional one.
            url = f"{config['base_url']}/sports-menu"
        elif key == "hardrock":
            url = "https://api.hardrocksportsbook.com/sportsbook/api/public/events/tree"
        elif key == "thescore":
            # The GraphQL endpoint itself.  A bare GET answers without the
            # client headers or the anonymous token the adapter sends, which is
            # what a probe wants: it asks whether the state's edge is reachable
            # at all, not whether a board parses.
            url = f"{config['api_base']}/graphql"
        else:
            # A route with no branch here used to be dropped silently, and
            # nothing downstream would have said so: it never reaches
            # ``candidates``, so it produces no table row, is not counted in
            # the "N of M candidate(s) answered" line, and writes no probe-cache
            # row.  The only other loop that prints route keys filters to
            # ``UNAVAILABLE``, so a new VALIDATED route would simply be invisible
            # — a book the operator believes is being probed and is not.  The
            # chain is a hand-kept duplicate of the retail key set with no test
            # binding the two, which is exactly the shape that goes stale.
            raise SystemExit(
                f"{configured.state} route {key!r} has no probe URL in "
                "state_candidates(); add a branch for it rather than letting "
                "the probe skip it silently"
            )
        candidates.append(
            Candidate(
                "retail",
                key,
                url,
                note=route.warning or f"{configured.state} validated route",
                source_key=key,
            )
        )
    return tuple(candidates)


def probe_registered(candidate: Candidate, *, state: str) -> tuple[str, str]:
    """Fetch and parse a small MLB scope through the real configured adapter."""
    from src.sources._common import Tier
    from src.sources.registry import descriptor_for_state

    # The strict lookup, never ``sources_for_state``: this function opens a
    # socket and writes the result into the probe cache, so a fallback here
    # records another state's route as this state's validated one.
    #
    # A refusal is one diagnostic row like any other failure.  ``state_candidates``
    # already skips ``UNAVAILABLE`` routes so this should be unreachable, but the
    # call sits outside the ``try`` below and the main loop has only a ``finally``
    # — letting it escape would end the whole probe pass in a traceback, which is
    # the least useful way for a defence-in-depth check to fire.
    #
    # Both exception types, not just ``KeyError``: the same lookup raises
    # ``RuntimeError`` for the *other* refusal — a route that exists but is tagged
    # for another state — and that is the one this call was added to catch.
    try:
        descriptor = descriptor_for_state(state, candidate.source_key)
    except (KeyError, RuntimeError) as exc:
        return "UNLICENSED", str(exc).strip('"')
    # Probe a scope the real adapter declares instead of manufacturing a
    # universal league.  Caesars' NFL special case predates 2026-08-09, when
    # its competition table carried only NFL/NBA ids; the Illinois menu
    # capture supplied MLB/NHL/WNBA, so the uniform MLB scope now works for
    # every state-sensitive adapter.
    league = "MLB"
    source = descriptor.build(leagues=(league,), timeout=TIMEOUT)
    try:
        raws = source.fetch_raw(tier=Tier.CORE)
        outcome = source.parse(raws)
    except Exception as exc:  # noqa: BLE001 - one diagnostic row, never a traceback
        detail = f"{type(exc).__name__}: {exc}"
        lowered = detail.lower()
        if "geo" in lowered or "region" in lowered or "location" in lowered:
            return "GEO RESTRICTED", detail
        if any(word in lowered for word in ("403", "blocked", "denied", "captcha", "waf")):
            return "BLOCKED", detail
        return "PARSE FAIL", detail
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - one source's close must not end the sweep
                pass
    if outcome.rejections:
        first = outcome.rejections[0]
        return "PARSE FAIL", f"{len(outcome.rejections)} rejection(s): {first.reason}"
    if not outcome.quotes:
        return "PARSE FAIL", f"adapter produced no {league} quotes"
    return "OK", f"{len(outcome.quotes)} {league} quote(s), {len(raws)} response(s)"


def probe(
    candidate: Candidate,
    *,
    state: str,
    verbose: bool,
    plain: bool = False,
) -> tuple[str, str]:
    """``(verdict, detail)`` for one candidate.  Never raises."""
    from src.sources.browser import browser_enabled, build_browser_client
    from src.sources.transport import build_default_client

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    selected_proxy = proxy_url(state)
    try:
        if plain:
            response = httpx.get(
                candidate.url, params=candidate.params, timeout=TIMEOUT,
                follow_redirects=True, headers=headers, proxy=selected_proxy,
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
                build_browser_client(timeout=TIMEOUT, seed_url=seed, state=state)
                if browser_enabled()
                else build_default_client(timeout=TIMEOUT, state=state)
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
    except Exception as exc:  # noqa: BLE001 - any failure is a reachability result
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


def _cache_status(verdict: str, detail: str) -> ProbeStatus:
    lowered = f"{verdict} {detail}".lower()
    if verdict == "OK":
        return ProbeStatus.OK
    # Checked before the substring markers: a licensing refusal never reached the
    # network, so the marker sweep below can only mis-file it.  It matched none of
    # them and was cached as ``PARSE_FAIL`` — a broken parser, for an adapter that
    # was never built.
    if verdict == "UNLICENSED":
        return ProbeStatus.UNLICENSED
    if "geo" in lowered or "not available in your region" in lowered:
        return ProbeStatus.GEO_RESTRICTED
    if any(word in lowered for word in ("403", "blocked", "denied", "waf", "captcha")):
        return ProbeStatus.BLOCKED
    return ProbeStatus.PARSE_FAIL


def main(argv: list[str] | None = None) -> int:
    refused = settings.refuse_bad_settings()
    if refused is not None:
        return refused
    parser = argparse.ArgumentParser(
        prog="python scripts/probe_sources.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--state",
        default=settings.STATE,
        help="retail jurisdiction (IL, PA, NJ, or DC)",
    )
    parser.add_argument(
        "--only",
        help="one source/name/family (draftkings, blocked, kambi, ...)",
    )
    parser.add_argument("--force", action="store_true", help="ignore fresh successful cache rows")
    parser.add_argument(
        "--template-only",
        action="store_true",
        help="request state-shaped routes without validating or caching them",
    )
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
    try:
        configured = jurisdiction(args.state)
    except (KeyError, RuntimeError) as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    state = configured.state
    if args.browser:
        import os
        os.environ["ODDS_FETCH_MODE"] = "browser"

    active = state_candidates(state)
    pool = (*active, *RESEARCH_CANDIDATES) if args.only else active
    chosen = [
        c
        for c in pool
        if not args.only
        or args.only in {c.family, c.name, c.source_key}
    ]
    if not chosen:
        known = sorted({c.family for c in pool} | {c.name for c in pool})
        print(f"no candidates matching {args.only!r}; known: {known}", file=sys.stderr)
        return 1

    detection = None
    fingerprint = ""
    if not args.template_only:
        detection = load_detection(settings.EGRESS_STATE_PATH)
        if detection is None or not is_recent(detection):
            print(
                "error: no recent egress detection; run "
                "python scripts/detect_state.py first",
                file=sys.stderr,
            )
            return 2
        if detection.state != state:
            print(
                f"error: requested {state} but recent detected egress is "
                f"{detection.state}; refusing validation",
                file=sys.stderr,
            )
            return 2
        fingerprint = detection.egress_fingerprint

    if args.plain:
        mode = "plain httpx"
    elif args.browser:
        mode = "Playwright Chromium"
    else:
        mode = "curl_cffi Chrome impersonation"
    proxy = proxy_url(state)
    print(f"state: {state} ({'template-only' if args.template_only else 'egress verified'})")
    print(f"transport: {mode}" + (" via configured proxy" if proxy else " (no proxy)"))
    print(f"{'family':<11} {'candidate':<28} {'verdict':<12} detail")
    print("-" * 110)
    reachable = 0
    cache = None if args.template_only else ProbeCache(settings.PROBE_CACHE_PATH)
    try:
        for candidate in chosen:
            if cache is not None and candidate.source_key and not args.force:
                cached = cache.fresh_ok(
                    candidate.source_key,
                    state,
                    fingerprint,
                    ttl=timedelta(days=settings.PROBE_TTL_DAYS),
                )
                if cached is not None:
                    print(
                        f"{candidate.family:<11} {candidate.name:<28} "
                        f"{'CACHED OK':<12} {cached.probed_at}"
                    )
                    reachable += 1
                    continue
            if candidate.source_key:
                verdict, detail = probe_registered(candidate, state=state)
            else:
                verdict, detail = probe(
                    candidate,
                    state=state,
                    verbose=args.verbose,
                    plain=args.plain,
                )
            actual_ok = verdict == "OK"
            reachable += actual_ok
            shown = "UNVALIDATED" if args.template_only else verdict
            shown_detail = f"{verdict}: {detail}" if args.template_only else detail
            print(
                f"{candidate.family:<11} {candidate.name:<28} "
                f"{shown:<12} {shown_detail}"
            )
            if candidate.note:
                print(f"{'':<11} {'':<28} {'':<12} note: {candidate.note}")
            if cache is not None and candidate.source_key:
                status = _cache_status(verdict, detail)
                route = configured.routes[candidate.source_key]
                if status is ProbeStatus.OK and route.routed_state != state:
                    status = ProbeStatus.UNTESTED
                    detail = f"legacy {route.routed_state} route answered; not {state} validation"
                cache.record(
                    candidate.source_key,
                    state,
                    fingerprint,
                    status,
                    detail,
                )
    finally:
        if cache is not None:
            cache.close()
    for key, route in configured.routes.items():
        if route.status is RouteStatus.UNAVAILABLE and (
            not args.only or args.only in {"retail", key}
        ):
            print(f"{'retail':<11} {key:<28} {'UNTESTED':<12} {route.detail}")
    print("-" * 110)
    print(f"{reachable} of {len(chosen)} candidate(s) answered with usable JSON")
    print("\nNext lever on a refusal: ODDS_HTTP_PROXY, then browser-cookie bootstrap / Playwright.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
