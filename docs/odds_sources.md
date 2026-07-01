# Odds Source Acquisition Plan

Last reviewed: 2026-07-01

## Integration Priority

1. **The Odds API** - already implemented. Best simple broad sportsbook feed for local testing and low-frequency polling.
2. **SportsGameOdds** - best affordable next sportsbook aggregator candidate. Adds richer odds objects, fair odds, props, consensus, and higher polling tiers.
3. **Polymarket** - public CLOB REST/WebSocket market data; useful for prediction-market sports prices and order-book liquidity.
4. **Kalshi** - official REST, WebSocket, and FIX; order books and sports filters exist. Strong next exchange adapter if API credentials are available.
5. **Betfair Exchange** - official API-NG and stream API. Strong direct exchange source if account/app-key access is available in your jurisdiction.
6. **Pinnacle** - official REST API has fixtures/odds and delta calls, but access is not generally open. Implement only after approval.
7. **OpticOdds / Sportradar** - commercial real-time feeds. Evaluate after local scanner logic proves useful.

## Exchanges / Prediction Markets

| Source | Official API | Live feed | Auth | Sports | Depth/liquidity | Adapter status |
|---|---:|---:|---|---|---:|---|
| Kalshi | Yes | REST, WebSocket, FIX | Public market data; RSA API key for trading/private endpoints | Yes, docs include sports filters/live data | Yes | Planned |
| Polymarket | Yes | REST, public market WebSocket, sports score WebSocket | Public market data; wallet/L2 auth for trading/user data | Yes | Yes | Read-only quote parser added |
| Betfair Exchange | Yes | API-NG + Exchange Stream API | App key/session token | Broad where available | Yes | Research only |
| Matchbook | Yes | Primarily REST | Account/session | Sports exchange | Yes | Research only |
| Smarkets | Likely | Needs account/docs validation | Account/API access | Sports exchange | Yes | Research only |
| Sporttrade | Limited public docs | Unknown | Account/commercial | Sports exchange | Likely | Research only |
| ProphetX | Limited public docs | Unknown | Commercial/account | Sports exchange | Likely | Research only |
| Novig | Limited public docs | Unknown | Commercial/account | Sports/prediction markets | Likely | Research only |

## Sportsbook / Aggregator APIs

| Source | Fit | Notes |
|---|---|---|
| The Odds API | Implemented | Broad book coverage, simple REST, quota headers, no public WebSocket. |
| SportsGameOdds | High | Affordable next adapter; REST polling, richer objects, many books/leagues, WebSocket on higher tiers. |
| OpticOdds | High/commercial | Real-time feed, many books, props/alternates; pricing/demo required. |
| Sportradar/Betradar | Enterprise | Very strong data but likely too heavy/expensive for MVP. |
| OddsJam/Unabated style | Conditional | Useful products, but public API access is not clear. Pursue only via partner/sales access. |

## Direct Sportsbook Acquisition

Use direct official APIs only where explicitly supported. Most consumer sportsbook web/app endpoints are private implementation details, often geo-sensitive and unstable.

| Book | Recommended path | Direct status |
|---|---|---|
| DraftKings | Aggregator | No supported public odds API found. |
| FanDuel | Aggregator | No supported public odds API found. |
| BetMGM | Aggregator | No supported public odds API found. |
| Caesars / William Hill US | Aggregator | No supported public odds API found. |
| BetRivers | Aggregator | No supported public odds API found. |
| Fanatics | Aggregator | No supported public odds API found. |
| Hard Rock Bet | Aggregator | No supported public odds API found. |
| ESPN BET / theScore Bet | Aggregator | ESPN BET brand retired; use theScore Bet where aggregators expose it. |
| bet365 | Aggregator | Direct scraping is brittle/geofenced; limited aggregator coverage varies by region. |
| Pinnacle | Official API if approved | Strong official fixtures/odds/delta model, but access is gated. |
| William Hill UK | Aggregator | No supported public odds API found. |
| Paddy Power | Aggregator or Betfair Exchange | Paddy sportsbook no public API; Betfair Exchange is official. |

## Adapter Rules

All new sources should emit `PriceQuote` rows or existing `Event` models:

- Preserve raw payloads and source IDs.
- Include event IDs, market IDs, selection IDs, timestamps, status, and liquidity when available.
- Prefer official APIs, WebSocket/SSE, or public JSON endpoints.
- Use browser/network interception only for local investigation, not production monitoring.
- Reject uncertain event matches; do not create cross-source arbitrage unless the match is above the auto-merge threshold.
- Apply fee, commission, slippage, stale-data, and liquidity filters before alerting.
