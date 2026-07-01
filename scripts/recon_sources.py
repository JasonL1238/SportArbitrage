"""Direct-source reconnaissance for public odds/market-data surfaces.

The tool intentionally avoids login, geoblock/CAPTCHA bypass, and anti-bot
evasion.  It records only public URLs and promising JSON payloads; request
headers, cookies, and auth-bearing query parameters are not persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROMISING_RE = re.compile(
    r"odds?|price|market|event|team|player|book|order|bid|ask|selection|fixture|competitor|liquidity",
    re.I,
)
SENSITIVE_QUERY_RE = re.compile(r"token|key|secret|session|auth|jwt|cookie|signature", re.I)
FIXTURE_DIR = Path("tests/fixtures/source_recon")


@dataclass(frozen=True)
class SourceTarget:
    name: str
    page_url: str
    probe_urls: tuple[str, ...] = ()


@dataclass
class ReconHit:
    method: str
    url: str
    status_code: int
    fixture_path: str | None = None
    fields_found: list[str] = field(default_factory=list)
    bytes_saved: int = 0


@dataclass
class ReconResult:
    source: str
    accessible: bool
    page_url: str
    hits: list[ReconHit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


SOURCES: dict[str, SourceTarget] = {
    "espn_odds": SourceTarget(
        "espn_odds",
        "https://www.espn.com/odds/",
        (
            "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
        ),
    ),
    "polymarket": SourceTarget(
        "polymarket",
        "https://polymarket.com/sports",
        (
            "https://clob.polymarket.com/sampling-markets",
        ),
    ),
    "kalshi": SourceTarget(
        "kalshi",
        "https://kalshi.com/markets/sports",
        (
            "https://api.elections.kalshi.com/trade-api/v2/markets?limit=5&status=open&query=sports",
        ),
    ),
    "draftkings": SourceTarget("draftkings", "https://sportsbook.draftkings.com/"),
    "fanduel": SourceTarget("fanduel", "https://sportsbook.fanduel.com/"),
    "betmgm": SourceTarget("betmgm", "https://sports.betmgm.com/"),
    "caesars": SourceTarget("caesars", "https://www.caesars.com/sportsbook-and-casino"),
    "betrivers": SourceTarget("betrivers", "https://www.betrivers.com/"),
    "fanatics": SourceTarget("fanatics", "https://sportsbook.fanatics.com/"),
    "hard_rock_bet": SourceTarget("hard_rock_bet", "https://app.hardrock.bet/"),
    "thescore_bet": SourceTarget("thescore_bet", "https://thescore.bet/"),
    "bet365": SourceTarget("bet365", "https://www.bet365.com/"),
    "pinnacle": SourceTarget("pinnacle", "https://www.pinnacle.com/en/"),
    "betfair": SourceTarget("betfair", "https://www.betfair.com/exchange/plus/"),
    "matchbook": SourceTarget("matchbook", "https://www.matchbook.com/"),
    "smarkets": SourceTarget("smarkets", "https://smarkets.com/"),
    "novig": SourceTarget("novig", "https://novig.us/"),
    "sporttrade": SourceTarget("sporttrade", "https://sporttrade.com/"),
    "prophetx": SourceTarget("prophetx", "https://www.prophetx.co/"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded direct-source reconnaissance")
    parser.add_argument("--source", action="append", choices=sorted(SOURCES), help="Source to probe")
    parser.add_argument("--fixtures-dir", type=Path, default=FIXTURE_DIR)
    parser.add_argument("--browser", action="store_true", help="Also capture browser network JSON via Playwright")
    parser.add_argument("--max-payloads", type=int, default=3)
    parser.add_argument("--timeout-ms", type=int, default=8000)
    args = parser.parse_args()

    args.fixtures_dir.mkdir(parents=True, exist_ok=True)
    names = args.source or list(SOURCES)
    results: list[ReconResult] = []
    with httpx.Client(timeout=args.timeout_ms / 1000, follow_redirects=True) as client:
        for name in names:
            target = SOURCES[name]
            result = ReconResult(source=name, accessible=False, page_url=target.page_url)
            result.hits.extend(_probe_public_endpoints(client, target, args.fixtures_dir, args.max_payloads))
            if args.browser:
                try:
                    result.hits.extend(_capture_browser_json(target, args.fixtures_dir, args.max_payloads, args.timeout_ms))
                except Exception as exc:
                    result.errors.append(f"browser_capture: {exc}")
            result.accessible = any(hit.status_code < 500 for hit in result.hits)
            results.append(result)

    for result in results:
        print(json.dumps(asdict(result), sort_keys=True))


def _probe_public_endpoints(
    client: httpx.Client,
    target: SourceTarget,
    fixtures_dir: Path,
    max_payloads: int,
) -> list[ReconHit]:
    hits: list[ReconHit] = []
    for url in target.probe_urls:
        try:
            resp = client.get(url)
        except Exception:
            continue
        hit = ReconHit(method="public_endpoint", url=_sanitize_url(str(resp.url)), status_code=resp.status_code)
        payload = _json_payload(resp)
        if payload is not None and _looks_promising(payload):
            hit.fields_found = _fields_found(payload)
            hit.fixture_path, hit.bytes_saved = _save_payload(fixtures_dir, target.name, payload, hit.url)
        hits.append(hit)
        if len([h for h in hits if h.fixture_path]) >= max_payloads:
            break
    return hits


def _capture_browser_json(
    target: SourceTarget,
    fixtures_dir: Path,
    max_payloads: int,
    timeout_ms: int,
) -> list[ReconHit]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is not installed; run `pip install -r requirements.txt` and `playwright install chromium`") from exc

    hits: list[ReconHit] = []
    seen: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(response: Any) -> None:
            if len([h for h in hits if h.fixture_path]) >= max_payloads:
                return
            url = _sanitize_url(response.url)
            if url in seen:
                return
            seen.add(url)
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower() and not PROMISING_RE.search(url):
                return
            try:
                payload = response.json()
            except Exception:
                return
            if not _looks_promising(payload):
                return
            fixture_path, bytes_saved = _save_payload(fixtures_dir, target.name, payload, url)
            hits.append(
                ReconHit(
                    method="browser_network_json",
                    url=url,
                    status_code=response.status,
                    fixture_path=fixture_path,
                    fields_found=_fields_found(payload),
                    bytes_saved=bytes_saved,
                )
            )

        page.on("response", on_response)
        page.goto(target.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(min(timeout_ms, 5000))
        browser.close()
    return hits


def _json_payload(resp: httpx.Response) -> Any | None:
    content_type = resp.headers.get("content-type", "")
    if "json" not in content_type.lower() and not resp.text.lstrip().startswith(("{", "[")):
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _looks_promising(payload: Any) -> bool:
    sample = json.dumps(payload, sort_keys=True)[:200_000]
    return bool(PROMISING_RE.search(sample))


def _fields_found(payload: Any) -> list[str]:
    sample = json.dumps(payload, sort_keys=True)[:200_000].lower()
    checks = {
        "event_id": ("event_id", "eventid", "event_ticker", '"id"'),
        "market_id": ("market_id", "marketid", "market_ticker", "condition_id", "ticker"),
        "teams": ("home", "away", "team", "competitor"),
        "odds_prices": ("odds", "price", "yes_ask", "no_ask", "bid", "ask"),
        "timestamps": ("time", "updated", "last_update", "commence", "timestamp"),
        "status": ("status", "active", "suspended", "closed"),
        "liquidity": ("liquidity", "size", "volume", "open_interest"),
    }
    return [name for name, tokens in checks.items() if any(token in sample for token in tokens)]


def _save_payload(fixtures_dir: Path, source: str, payload: Any, url: str) -> tuple[str, int]:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    path = fixtures_dir / f"{source}_{digest}.json"
    text = json.dumps({"url": url, "payload": payload}, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    return str(path), len(text.encode("utf-8"))


def _sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        safe_query.append((key, "REDACTED" if SENSITIVE_QUERY_RE.search(key) else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


if __name__ == "__main__":
    main()
