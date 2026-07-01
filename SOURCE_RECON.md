# Direct Source Recon

Last updated: 2026-07-01

Scope: public pages, official/public market-data endpoints, and browser-observable structured data only. This work does not bypass login, geoblocks, CAPTCHAs, or technical protections, and it does not implement anti-bot evasion. Aggregator APIs are intentionally excluded from this recon.

## Commands

```bash
python scripts/recon_sources.py --source espn_odds --source polymarket --source kalshi --max-payloads 2
python scripts/recon_sources.py --browser --source draftkings --source betrivers --max-payloads 3
python scripts/smoke_sources.py --source espn_odds --sport baseball_mlb --max-items 2
python scripts/smoke_sources.py --source polymarket --max-items 1
python scripts/smoke_sources.py --source kalshi --max-items 2
python -m pytest tests/test_source_smoke_fixtures.py tests/test_polymarket_adapter.py tests/test_kalshi_adapter.py -q
```

Browser recon requires:

```bash
pip install -r requirements.txt
playwright install chromium
```

## Verified Fixtures

| Source | Fixture | Notes |
|---|---|---|
| ESPN odds | `tests/fixtures/source_smoke/espn_odds_latest.json` | 2 MLB events, normalized to `PriceQuote`. |
| Polymarket | `tests/fixtures/source_smoke/polymarket_latest.json` | 1 sports-tagged Gamma event, normalized to 50 `PriceQuote` rows. |
| Kalshi | `tests/fixtures/source_smoke/kalshi_latest.json` | 2 public sports-query markets, normalized to `PriceQuote`. |
| ESPN recon | `tests/fixtures/source_recon/espn_odds_5910c0b757.json` | Public scoreboard JSON. |
| Polymarket recon | `tests/fixtures/source_recon/polymarket_a265f81e16.json` | Public CLOB sampling JSON. |
| Kalshi recon | `tests/fixtures/source_recon/kalshi_d6e23d5d82.json` | Public Kalshi markets JSON. |

## Source Status

| Source | Accessible now | Best acquisition method | Auth required | Live odds/prices visible | Sports markets | Structured JSON | Sample fixture path | Fields found | Parsing difficulty | Reliability risk | Recommendation |
|---|---:|---|---|---:|---:|---:|---|---|---|---|---|
| ESPN odds | Yes | Public ESPN scoreboard + summary JSON | No | Yes | Yes | Yes | `tests/fixtures/source_smoke/espn_odds_latest.json` | event ID, teams, odds, timestamps, status | Easy | Medium: media endpoint, limited book coverage, no liquidity | Implemented now |
| Polymarket | Yes | Public Gamma REST for sports events; public CLOB REST/WS for books | No for market data; wallet/API auth for trading | Yes | Yes | Yes | `tests/fixtures/source_smoke/polymarket_latest.json` | event ID, market ID, token IDs, outcomes, prices, liquidity, timestamps, status | Medium | Medium: prediction taxonomy, restricted flags, event matching differs from sportsbooks | Implemented now |
| Kalshi public market data | Yes | Official public REST `/trade-api/v2/markets`; WS/auth later | No for market list; auth for full order book/trading | Yes, top-level bid/ask | Yes | Yes | `tests/fixtures/source_smoke/kalshi_latest.json` | ticker, event ticker, market type, yes/no ask, volume/liquidity, timestamps, status | Medium | Medium: sports availability changes; full depth requires signed auth | Implemented now |
| DraftKings | Partial | Public/browser JSON endpoint pattern | No for public viewing | Yes | Yes | Yes | None yet | event IDs, outcome/market IDs, teams, odds, status partial | Medium | Medium-high: undocumented, region/layout changes | Defer; browser-recon next |
| FanDuel | Partial | Browser JSON endpoint pattern | No for public viewing, but state params vary | Yes | Yes | Yes | None yet | event ID, market ID, runners, odds, status | Medium | High: state hostnames, changing params | Defer |
| BetMGM | Partial | Browser JSON endpoint pattern | No for public viewing, but CDN/location params vary | Yes | Yes | Yes | None yet | fixture/event IDs, market IDs, teams, odds, status | Medium | High: undocumented CDN/API shape | Defer |
| Caesars | Partial | Browser JSON endpoint pattern | No for public viewing, but region/location params vary | Yes | Yes | Yes | None yet | event IDs, market IDs, teams, odds, status | Medium-high | High: region and brand parameters | Defer |
| BetRivers | Partial | Public Kambi JSON | No for public viewing | Yes | Yes | Yes | None yet | event ID, criterion/market IDs, teams, odds, timestamps, status | Medium | Medium: Kambi stable, operator paths vary | Best direct sportsbook follow-up |
| Fanatics | No/limited | Browser JSON or blocked | Public viewing may be gated | Unverified | Yes | Unknown | None | unknown | Hard | High: app-first, protected/geo-dependent | Defer |
| Hard Rock Bet | No/limited | Browser JSON or blocked | Public viewing may be gated | Unverified | Yes | Unknown | None | unknown | Hard | High: app/location gating | Defer |
| ESPN BET / theScore Bet | No/limited | App/browser JSON or blocked | Likely app/location required | Unverified | Yes | Unknown | None | unknown | Hard | High: ESPN BET brand transition; app-first | Defer |
| bet365 | No/limited | Blocked/protected browser flows | Public page only; live data not reliably accessible | Unverified | Yes | Not reliably public | None | unknown | Very hard | Very high: geofencing/protected flows | Avoid direct |
| Pinnacle | No from public US web | Official API only | Yes, approved API account | Not public | Yes | Yes with auth | None | event IDs, line IDs, odds, timestamps | Low with API, hard without | Medium: API/account terms, US access | Defer until authorized API |
| Betfair | No from current US access | Official API-NG + Stream API | Yes, app key/session | No public prices | Yes | Yes with auth | None | market/book schemas in docs | Hard | High: account/geo restrictions | Defer until valid account |
| Matchbook | Yes | Official REST public event/price endpoints | No for public read observed; auth for account/betting | Yes | Yes | Yes | None yet | event ID, market ID, runner ID, decimal/US odds, liquidity, timestamps, status | Low-medium | Medium: jurisdiction/account terms, sparse liquidity | Strong next exchange candidate |
| Smarkets | Partial | Official HTTP API/OpenAPI | Session/API-user for latest prices | Not unrestricted | Yes | Yes | None | event, market, contract, price/quantity schemas | Medium | Medium-high: API-user approval | Defer |
| Novig | No for live data without credentials | Official REST/GraphQL/WS | Yes | No | Yes | Yes docs/schema | None | market/outcome/orderbook schemas | Medium | Credentials/partner access required | Defer until credentials |
| Sporttrade | Reference only | Public SSE reference-data stream | No for refdata; prices unclear | No prices proven | Yes metadata | Yes | None | leagues, contests, participants, markets, outcomes, trading state | Medium | Prices/order books not publicly proven | Metadata only; defer prices |
| ProphetX | No without credentials | REST + Pusher WebSocket | Yes | No | Yes | Yes docs/schema | None | events, markets, liquidity via authenticated endpoints | Hard | Commercial onboarding/auth | Defer |

## Implemented Direct Adapters

Implemented now, capped at 3 direct sources:

| Adapter | Source file | Parser tests | Smoke command |
|---|---|---|---|
| ESPN odds | `src/sources/espn_odds.py` | `tests/test_espn_adapter.py`, `tests/test_source_smoke_fixtures.py` | `python scripts/smoke_sources.py --source espn_odds --sport baseball_mlb --max-items 2` |
| Polymarket | `src/sources/polymarket.py` | `tests/test_polymarket_adapter.py`, `tests/test_source_smoke_fixtures.py` | `python scripts/smoke_sources.py --source polymarket --max-items 1` |
| Kalshi | `src/sources/kalshi.py` | `tests/test_kalshi_adapter.py`, `tests/test_source_smoke_fixtures.py` | `python scripts/smoke_sources.py --source kalshi --max-items 2` |

## Normalized Output Samples

Fields are normalized into `PriceQuote`:

| Source | Example |
|---|---|
| ESPN odds | `source=espn_odds`, `sport=baseball_mlb`, `market_type=moneyline`, `selection=Toronto Blue Jays`, `decimal_odds=1.869565`, `source_event_id=401815981` |
| Polymarket | `source=polymarket`, `sport=soccer`, `market_type=prediction_binary`, `selection=Yes`, `price=0.1005`, `source_market_id=<conditionId>` |
| Kalshi | `source=kalshi`, `market_type=prediction_binary`, `selection=NO`, `price=1.0`, `source_market_id=<ticker>`, `timestamp=<updated_time>` |

## Best Next Direct Sources

1. **Matchbook** - strongest next exchange candidate because public REST event/price reads appear structured and include liquidity.
2. **BetRivers/Kambi** - best direct sportsbook follow-up if browser recon confirms stable public Kambi endpoints for the target state.
3. **DraftKings** - useful secondary direct book, especially because ESPN payloads expose DraftKings-linked IDs, but it is still undocumented and more brittle.
4. **Kalshi authenticated order books** - add later if credentials are available; public market list is already implemented.
5. **Betfair/Smarkets/Novig/ProphetX/Pinnacle** - wait for explicit credentials/API approval.

Avoid direct bet365/Fanatics/Hard Rock/theScore scraping unless a stable public JSON path is observed without login, geolocation bypass, CAPTCHA, or protected-flow workarounds.
