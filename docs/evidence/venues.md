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
| Fanatics | NXDOMAIN | NXDOMAIN | ~~find current host~~ ~~**found 2026-08-13**~~ — **closed 2026-08-14, asked from IL egress on both transports.** The DNS-confirmed `sportsbook.1{state}.betfanatics.com` map is a licensure signal, not a route: all five licensed states serve a byte-identical 548 B nginx `404` on every path. The app host `sportsbook.fanatics.com` is an Akamai Bot Manager challenge on plain HTTP and `301`s to the marketing site through a real browser. `sportsbook.betfanatics.com/sportsbook` is a real `401`. No anonymous web board exists; next lever is a logged-in capture through the mobile rig, gated on fixture viability (`state-routing.md` § "Fanatics has no anonymous board on the web") |
| bet365 | `403` Cloudflare *(stateless origin)* | `403` *(stateless origin)* | **2026-08-14, IL egress**: the per-state `www.il.bet365.com` answered `200` with the full shell and `/pullpodapi/gethomepageadditionalpods` served 754 prices with no token — so the 403s above belong to `www.bet365.com`, which is not the book. **But that egress is now hard-blocked** (`403`, 4,547 B, Cloudflare WAF, IP-scoped) after ~100 probing requests, so none of it is reproducible from here. Only the *home page* is HTTP-reachable; the league board still stalls exactly as recorded on 2026-08-04. Next lever needs a different IL egress (`state-routing.md` § "bet365 Illinois: the home page is HTTP") |
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

---

## DraftKings Illinois — three route generations, 2026-08-14

Measured from the machine's own IL egress (no proxy; `data/egress_state.json`
records the IL fingerprint). All ids below were read from the route table the
public NFL page embeds for itself, never guessed or iterated.

**What was wrong.** Run 13 collected **78 rows, MLB only**, from a book holding an
IL licence. Two defects, and the second is why the first stayed invisible:

| League | id | Route it was using | Answer |
|---|---|---|---|
| MLB | 84240 | `leagueSubcategory/v1` sub `4519` | correct, 14 events |
| NFL | 88808 | `leagueSubcategory/v1` sub `10500` | **`200` carrying futures** |
| WNBA | 94682 | retired `api/v5/eventgroups` | `access denied` |
| NHL | 42133 | retired `api/v5/eventgroups` | `access denied` |
| EPL | 40253 | retired `api/v5/eventgroups` | `access denied` |
| NBA | 42648 | retired `api/v5/eventgroups` | not requested (off-season) |

Subcategory `10500` had gone stale. It answered `200` with 24,210 bytes holding a
single 38-way market named `Winner` on an event called *NFL 2026/27 Season*,
whose participants are **US states** (California, Maryland, Florida, Ohio,
Texas…) and whose `eventParticipantType` is `MultiTeam`. The parser refused to
build rows from it — correctly — but the scope counted as *produced*, so the run
reported `draftkings ok=1` and `scopes_failed=3` rather than 4. The capture is
committed as
`tests/fixtures/raw/draftkings__20260814T015421Z_sportscontent-88808-futures_*.json`.
NFL's current Game Lines subcategory is `4518`, not `10500`; it is recorded here
only as evidence that these ids move, because the adapter no longer uses any.

**What replaced it.** The page itself reads
`api/sportscontent/controldata/league/primaryMarkets/v1/markets`, parameterised
only by league id plus two constants — `$filter=... AND type eq 'Fixture'` for
events and `$filter=tags/any(t: t eq 'PrimaryMarket')` for markets. There is no
subcategory id on this route, so there is nothing seasonal left to keep current,
and `type eq 'Fixture'` excludes the futures class structurally.

Verified 2026-08-14 with a **plain HTTP client** — no Akamai `403`, no browser
fallback needed, which is a change from the `403` recorded above:

| League | id | events | markets |
|---|---|---|---|
| MLB | 84240 | 11 | Moneyline, Run Line, Total |
| NFL | 88808 | 100 (capped) | Moneyline, Spread, Total |
| NFL Preseason | 24685 | 10 | Moneyline, Spread, Total |
| WNBA | 94682 | 5 | Moneyline, Spread, Total |
| NBA | 42648 | 41 | Moneyline, Spread, Total |
| NHL | 42133 | 31 | Moneyline, Puck Line, Total |
| EPL | 40253 | 10 | Moneyline only |

Three findings worth not rediscovering:

- **`24685` is *NFL Preseason*, a separate league from NFL (`88808`).** In August
  it is the one holding games other books are pricing; `88808`'s earliest event
  is 2026-09-10. Both are registered and both normalize to the `NFL` league key.
- **`top` caps at 100.** The page asks for 20; 100 is served and `300` is refused
  with `HTTP 400 {"errorStatus":{"code":"MRKTBFF-400"}}`. NFL fills the cap, so
  it is reported through `ScopeTally.truncated` rather than passing as a whole
  slate. No paging parameter was tried — the page does not use one.
- **EPL returns Moneyline only on this route**, where the subcategory route also
  carried totals. Accepted: the alternative was the zero rows EPL produced
  before. Revisit if soccer totals become load-bearing.

Result: **1,212 quotes across 207 events** (NFL 110, NBA 41, NHL 30, MLB 11,
EPL 10, WNBA 5), against 78 MLB-only rows before.
