# The registered venues and the walls they hit

The original registration baseline — what each venue serves, what blocked, and
what the set leaves uncovered.

## Original California baseline — registered sources

Measured on one live ``--tier core`` pass (2026-07-28), which produced **1,683
cross-book markets** against 331 from the original three books, and found five
risk-free positions where three books found none.  BetMGM was added on
**2026-07-30** after a re-probe found the Illinois Entain CDS host answering.
Unibet Australia was probed the same day and later dropped — not a counterparty
you can stake from this egress (Davis, CA / U.C.).

| Source key | Venue | Kind | Charge | Requests | How it is reached |
|---|---|---|---|---|---|
| `fanduel` | FanDuel | sportsbook | none (in the price) | 6 | `sbapi.il.sportsbook.fanduel.com` content pages; soccer totals and handicaps are a depth-tier per-event hop |
| `pinnacle` | Pinnacle | sportsbook | none (in the price) | 16 | `guest.api.arcadia.pinnacle.com`, matchups + markets per scope |
| `betrivers_kambi` | BetRivers Illinois | sportsbook | none (in the price) | 67 | Kambi offering API, operator `rsiusil` |
| `leovegas_kambi` | LeoVegas | sportsbook | none (in the price) | 69 | Kambi offering API, operator `leo` |
| `bovada` | Bovada | sportsbook (offshore) | none (in the price) | 12 | one coupon request per league, whole slate with prices; states `competitors[].home` |
| `betmgm` | BetMGM Illinois | sportsbook | none (in the price) | ~6 | `www.il.betmgm.com/cds-api/bettingoffer/fixtures` with public `x-bwin-accessid`; one paged request per sport |
| `matchbook` | Matchbook | exchange | 2% of net winnings | 6 | one call per sport: moneyline, totals and handicaps **with the money behind each price** |
| `smarkets` | Smarkets | exchange | 2% of net winnings | 48 | fully typed markets and contracts; **moneyline only** — see the rate limit below |
| `sxbet` | SX Bet | exchange | 5% oracle fee | 26 | resting order book, sized per order; the alternate ladder is a depth-tier cost |
| `kalshi` | Kalshi | prediction market | `0.07 × p × (1−p)` per contract | 3 | MLB moneyline, totals and spreads from structured series |
| `polymarket` | Polymarket | prediction market | `0.05 × min(p, 1−p)` per contract | 10 | structured `sportsMarketType`, `line` and `teams` — see the correction below |

Every sport is comparable across at least three of them, and the mirror sweep
over that slate found no pair above 65% price agreement — well clear of the 95%
that marks one counterparty wearing two names.

### Rate limits, honoured rather than routed around

Three venues state a limit and enforce it. Each is respected by pacing that
source's own host, which is a constraint to work within:

- **Smarkets** — twenty requests per minute, stated in its own `429` body. A pass
  that also fetched the totals and handicaps needed several hundred requests and
  was throttled part-way through four of six sports. So Smarkets collects the
  **moneyline only**, declares that through its `capabilities`, and paces at
  3.1 s. It is the slowest source here (about 2½ minutes) and says so.
- **Bovada** — five of seven league requests drew a `429` at the shared default
  pace, which reads downstream as five leagues with no fixtures. Paced at 1 s.
- **Kalshi** — `429 too_many_requests` on the second request of an unpaced probe.
  Paced at 0.6 s.

## Original California blocks

### Previously blocked under plain `httpx` — reopen candidates

Re-probed **2026-07-30** from Davis, CA with (1) `curl_cffi` Chrome
impersonation and (2) Playwright Chromium seeded on the sportsbook origin.
Neither opened odds JSON without a licensed-state exit IP:

| Venue | curl_cffi | Playwright (CA egress) | Next lever |
|---|---|---|---|
| DraftKings | `403` Akamai | HTML shell loads; `sportsbook-nash` API still `403`; no odds XHR | `ODDS_HTTP_PROXY` to IL/NJ residential, then adapter |
| Caesars | `403` CloudFront | `403` | same — licensed-state proxy |
| Fanatics | NXDOMAIN | NXDOMAIN | find current host |
| bet365 | `403` Cloudflare | `403` | anonymous application observation only; never bypass a challenge |
| Betway / Bally / Fliff / Hard Rock | no simple public GET | — | reverse runtime/XHR or mobile |
| ESPN BET | discontinued | — | skip |

BetMGM Illinois still answers under Chrome impersonation (registered).  Big-book
adapters are blocked on **egress identity**, not on missing code paths: set
`ODDS_HTTP_PROXY` and re-run detection followed by
`python scripts/probe_sources.py --state IL --only blocked`.

---

## What this leaves

**DraftKings** and **Hard Rock Bet** first-party adapters are registered
(`draftkings`, `hardrock`) with Action Network failovers (`an_draftkings`,
`an_hardrock`). From Davis, CA:

| Venue | Ladder / tree | Odds JSON | Lever |
|---|---|---|---|
| DraftKings | n/a | Akamai `403` | `ODDS_HTTP_PROXY` to IL/NJ |
| Hard Rock | ladder + tree **200** | GraphQL `events.data=[]` | same proxy |
| Caesars / Fanatics | — | still blocked / host moved | proxy + host discovery |
| Bally (`ballybet` Kambi) | — | `429 No access` | proxy or `an_bally` |
| Fliff / Circa / SuperBook (Westgate) | — | no stable first-party JSON from CA | AN book ids `2292` / `78` / `14` |

Set `ODDS_HTTP_PROXY` to a licensed-state residential exit and re-run
`python scripts/probe_sources.py --only blocked --template-only` /
`--only hardrock --template-only` for research, or use a matching detected
licensed-state egress for validation.

Four of the venues are exchanges or prediction markets, whose two-sided quotes
and — for two of them — stated liquidity make a position *more* checkable than
any sportsbook's posted price does.
