# Source Audit

Last updated: 2026-07-01

This audit separates proven local smoke coverage from sources that require credentials, commercial access, or aggregator substitution. Smoke tests are intentionally short: 1-3 events or markets only.

## Smoke Test Commands

```bash
python scripts/smoke_sources.py --source espn_odds --sport baseball_mlb --max-items 2
python scripts/smoke_sources.py --source polymarket --max-items 2
python scripts/smoke_sources.py --source odds_api --sport baseball_mlb --max-items 1
```

`odds_api` skips cleanly when `ODDS_API_KEY` is not set.

## Audit Table

| Source | Type | Access method | Auth required | Current implementation status | Live odds/order book retrievable now? | Parsing works? | Normalized `PriceQuote` works? | Sample command to test | Sample raw payload path | Known risks / blockers | Recommendation |
|---|---|---|---|---|---:|---:|---:|---|---|---|---|
| Current implemented sources | Mixed | Adapter registry | Varies | ESPN, The Odds API, Polymarket parser, local store | Partial | Yes for fixture-backed sources | Yes for ESPN/Polymarket fixtures | `python -m pytest tests/test_source_smoke_fixtures.py -q` | `tests/fixtures/source_smoke/` | Registry still separates Event adapters and Quote adapters | Keep improving |
| ESPN odds | Media/public JSON | Public ESPN scoreboard + per-event summary JSON | None | Implemented | Yes for MLB smoke | Yes | Yes | `python scripts/smoke_sources.py --source espn_odds --sport baseball_mlb --max-items 2` | `tests/fixtures/source_smoke/espn_odds_latest.json` | Limited book coverage, often DraftKings only; media API not contractual | Use as free sanity-check source |
| Polymarket | Prediction market / CLOB | Public REST CLOB; public WebSocket documented | None for market data; wallet/L2 auth for trading/user | Read-only parser + smoke script | Yes | Yes | Yes | `python scripts/smoke_sources.py --source polymarket --max-items 2` | `tests/fixtures/source_smoke/polymarket_latest.json` | Sports event matching can be weak; prediction markets are binary and not sportsbook taxonomy | Implement/expand now |
| The Odds API | Sportsbook aggregator | Official REST API | API key | Implemented, not live-smoked here because key absent | Not without key | Fixture tests pass | Yes through `quotes_from_events` | `ODDS_API_KEY=... python scripts/smoke_sources.py --source odds_api --sport baseball_mlb --max-items 1` | None yet | Credit budget, no public WebSocket, paid tiers for more coverage | Use now when key is available |
| SportsGameOdds | Sportsbook aggregator | Official REST; WebSocket on higher tiers | API key | Not implemented | Not without key | Not yet | High feasibility | Future: `python scripts/smoke_sources.py --source sportsgameodds --sport baseball_mlb --max-items 1` | None | Requires account; update cadence depends on tier | Implement next aggregator |
| OpticOdds | Sportsbook aggregator | Commercial API / push feeds advertised | API/commercial access | Not implemented | Not without demo/commercial access | Not yet | High feasibility | None until credentials/docs access | None | Quote/demo pricing; protocol details require account | Defer until paid trial/demo |
| Kalshi | Regulated prediction exchange | Official REST + WebSocket + FIX | API key/signature for useful market/orderbook endpoints | Not implemented | Market list likely; orderbook requires auth | Not yet | High feasibility | `curl -L 'https://external-api.kalshi.com/trade-api/v2/markets?limit=10'` | None | Auth signing; sports availability varies | Implement after credentials |
| Betfair | Betting exchange | Official API-NG REST/JSON-RPC + Exchange Stream API | App key + session token/certs | Not implemented | Not from current unauthenticated setup | Not yet | High feasibility | Authenticated `listMarketBook` JSON-RPC | None | U.S./geo/account restrictions; commission/liability handling | Defer unless account/app key available |
| Matchbook | Betting exchange | Official REST | Session token/account | Not implemented | Requires account/session for useful prices | Not yet | High feasibility | Authenticated `GET /edge/rest/events?...` | None | Session lifecycle, fair-use, jurisdiction | Implement after credentials |
| Smarkets | Betting exchange | Official REST/OpenAPI | Session token; API-user status for unrestricted latest prices | Not implemented | Requires approved account | Not yet | Medium-high feasibility | Authenticated session + prices endpoints | None | API-user approval, country/IP restrictions | Defer until approved |
| Pinnacle | Sportsbook / sharp book | Official REST API | HTTP Basic with approved/funded API account | Not implemented | Requires approved access | Not yet | Medium feasibility | Authenticated `GET /v1/odds?sportId=...` | None | Public access closed/gated; no order-book liquidity | Use only if approved |
| Novig | Sports prediction/exchange market | REST + GraphQL + WebSocket docs | OAuth/JWT/API key/partner | Not implemented | Requires credentials/partner setup | Not yet | High feasibility | Authenticated market/orderbook endpoint | None | Partner/API access required; exact prod auth needed | High priority after credentials |
| ProphetX | Sports exchange API | REST + Pusher WebSocket | Token login/API access | Not implemented | Requires credentials | Not yet | Medium-high feasibility | Authenticated `/mm/get_sport_events`, `/mm/get_markets` | None | Commercial onboarding, short access tokens, WSS auth | Defer until credentials |
| Sporttrade | Regulated sports exchange | Public docs mention SSE/reference stream; live price REST unclear | Unclear/account likely | Not implemented | Not enough public order-book access proven | Not yet | Medium feasibility if docs open | Reference SSE only, no proven orderbook smoke | None | Incomplete/gated docs, jurisdiction-specific | Defer |
| DraftKings | Sportsbook | Licensed aggregator preferred | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | The Odds API/SportsGameOdds bookmaker key | None | Private web/app endpoints are brittle, geo-sensitive, ToS risk | Use aggregator |
| FanDuel | Sportsbook | Licensed aggregator preferred | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | Private endpoints, coverage tier variance | Use aggregator |
| BetMGM | Sportsbook | Licensed aggregator preferred | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | Region/market coverage variance | Use aggregator |
| Caesars | Sportsbook | Licensed aggregator; often William Hill US key | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | Brand/key mismatch Caesars vs William Hill US | Use aggregator |
| BetRivers | Sportsbook | Licensed aggregator preferred | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | Smaller book coverage may vary | Use aggregator |
| Fanatics | Sportsbook | Licensed aggregator preferred | Aggregator API key; paid tier may be needed | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | Evolving product; paid-only coverage possible | Use aggregator |
| Hard Rock Bet | Sportsbook | Licensed aggregator; state-specific keys possible | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | State-specific odds variants | Use aggregator |
| ESPN BET / theScore Bet | Sportsbook | Aggregator under current brand/key naming | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | ESPN BET brand retired/rebranded to theScore Bet | Use aggregator |
| bet365 | Sportsbook | Licensed/commercial aggregator where covered | Aggregator/commercial auth | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key if available | None | Heavy geofencing/anti-automation; region coverage limited | Use aggregator |
| William Hill | Sportsbook | UK/EU aggregator; US maps separately | Aggregator API key | Not direct | No supported direct public odds API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | UK vs US/Caesars ambiguity | Use aggregator |
| Paddy Power | Sportsbook | Aggregator; Betfair Exchange is separate | Aggregator API key | Not direct | No supported direct sportsbook API | Via aggregator only | Via aggregator only | Aggregator bookmaker key | None | Do not treat sportsbook prices as Betfair exchange liquidity | Use aggregator |

## Short Smoke Results

| Source | Result | Notes |
|---|---|---|
| ESPN odds | Passed | 2 MLB events fetched, 8 quotes normalized, raw fixture saved. |
| Polymarket | Passed | 2 markets fetched, 4 order-book quotes normalized, raw fixture saved. |
| The Odds API | Skipped | `ODDS_API_KEY` was not set in the local environment. |

## Best Next Sources

1. **Polymarket** - already smoke-proven without auth; expand market discovery, WebSocket, and sports matching.
2. **The Odds API** - run keyed smoke next; broadest immediate sportsbook coverage.
3. **SportsGameOdds** - best affordable next aggregator to implement if you want broader book/props coverage.
4. **Kalshi** - good exchange mechanics after credentials; sports coverage varies.
5. **Novig** - strong sports-native exchange fit if partner/API credentials are available.
6. **Betfair / Matchbook / Smarkets / ProphetX / Pinnacle** - implement only after account/API access is confirmed.

Direct sportsbook scraping for DraftKings, FanDuel, BetMGM, Caesars, BetRivers, Fanatics, Hard Rock Bet, theScore Bet, bet365, William Hill, and Paddy Power should be avoided as a production strategy unless written permission or official API access exists.
