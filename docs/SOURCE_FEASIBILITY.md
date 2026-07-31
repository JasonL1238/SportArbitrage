# Which sources are reachable, which are not, and why

This is the record of what happened when every candidate source was actually
asked. It exists so that a wall is documented once rather than rediscovered every
few months by somebody writing an adapter for a host that has never answered.

Reproduce it with:

```bash
python scripts/probe_sources.py
```

Everything below was first measured on **2026-07-28** from a host in Davis,
California with plain `httpx`. That pass classified several big books as
permanently closed. **That policy is retired.** The collector now defaults to
Chrome TLS impersonation (`curl_cffi`) and will use `ODDS_HTTP_PROXY` when set.
Rows below that still say "closed" mean *not yet opened with the new transport*,
not *forbidden to open*. Re-probe with:

```bash
python scripts/probe_sources.py --only blocked
```

---

## Registered — twelve distinct sources

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

## Permanently closed (still)

### Previously blocked under plain `httpx` — reopen candidates

Re-probed **2026-07-30** from Davis, CA with (1) `curl_cffi` Chrome
impersonation and (2) Playwright Chromium seeded on the sportsbook origin.
Neither opened odds JSON without a licensed-state exit IP:

| Venue | curl_cffi | Playwright (CA egress) | Next lever |
|---|---|---|---|
| DraftKings | `403` Akamai | HTML shell loads; `sportsbook-nash` API still `403`; no odds XHR | `ODDS_HTTP_PROXY` to IL/NJ residential, then adapter |
| Caesars | `403` CloudFront | `403` | same — licensed-state proxy |
| Fanatics | NXDOMAIN | NXDOMAIN | find current host |
| bet365 | `403` Cloudflare | `403` | proxy + Playwright challenge solve |
| Betway / Bally / Fliff / Hard Rock | no simple public GET | — | reverse runtime/XHR or mobile |
| ESPN BET | discontinued | — | skip |

BetMGM Illinois still answers under Chrome impersonation (registered).  Big-book
adapters are blocked on **egress identity**, not on missing code paths: set
`ODDS_HTTP_PROXY` and re-run `python scripts/probe_sources.py --only blocked`.

---

## Mirrors: the trap that looks like progress

Kambi is a *platform*. One adapter plus an operator token reaches BetRivers,
LeoVegas, Unibet and a long tail of others, which reads like a way to go from
three sources to a dozen in an afternoon.

It is not, because most of those tokens are **licences of one book**:

| Tenants | Verdict | Evidence |
|---|---|---|
| `rsiusil`, `rsiusnj`, `rsiuspa`, `rsiusmi`, `rsiusin`, `rsiuswv`, `rsiusny` | **mirrors of each other** | same 16 fixtures, byte-identical listView payloads (same sha256), 30 of 30 shared moneyline prices equal |
| `kambi` (reference tenant) | **mirror of the above** | as above |
| `ub`, `ubuk`, `ubse`, `ubdk`, `ubnl`, `ubro`, `pafse`, `atg`, `jvh` | **mirrors of each other** | identical to one another |
| `leo` (LeoVegas) | **distinct** | same slate, its own prices — Cleveland at 2.28 where BetRivers had 2.38 |

A mirror is invisible to every other check in this pipeline. It satisfies the
source contract, emits valid rows, raises the cross-source coverage counts, and
clears `require_distinct_sources` in `src/arb.py` — because that compares source
*keys*, and two keys is exactly what a mirror has. What it does not have is two
counterparties, so an "arbitrage" between them is a position nobody can hold, at
the top of the report, indistinguishable from a real one.

So distinctness is **measured**, in `src/distinctness.py`, against a live slate,
and the measurement is pinned in `tests/test_distinctness.py` using captured
payloads from all three tenants. A table asserting "these two are different"
would go stale the first time an operator changed platform; a price comparison
does not.

Candidate tenants rejected as mirrors are listed above **with the source they
mirror**, which is the part that stops them being rediscovered and re-added.

---

## Two corrections to the original plan

**Polymarket is far better structured than assumed.** The plan expected
"prose-titled binaries" needing a confidence-scored decomposer, and recorded that
standardised game totals largely do not exist. Neither is true of the Gamma API
as it stands: an event carries `teams` with an explicit `ordering: home|away`, a
`startTime`, and markets carrying `sportsMarketType` (`moneyline`, `spreads`,
`totals`) with a numeric `line`. Totals exist at 7.5, 8.5 and 9.5 on the MLB
slate. So the mapping is from structured fields only, exactly as required, and no
title is ever parsed.

**`settlement_shape_mismatch` was never all-or-nothing.** The plan lists it
alongside `source_prices_itself_to_lose` as a refusal that discards the whole
group. Reading the code, it records a diagnostic and then carries on per contract
shape; only `source_prices_itself_to_lose`, `mixed_sport` and
`legs_disagree_on_the_game` actually abandoned the group, and those are what were
fixed.

---

## Notes on charges

Getting a commission rate wrong in the *generous* direction manufactures
arbitrage; getting it wrong in the strict direction only loses one. Every
uncertainty below is resolved in the second direction on purpose.

- **Matchbook, Smarkets** — 2% of net winnings, the published standard rate. An
  account can earn a lower one through volume, so using the standard rate
  understates the edge rather than inventing one.
- **SX Bet** — its own `/metadata` reports `oracleFees` of `5 × 10^18` against a
  `10^20` scale. That reading is not stated in words anywhere in the payload, so
  it is taken as **5% of net winnings**, the most expensive plausible
  interpretation.
- **Kalshi** — the published schedule, `0.07 × p × (1−p)` per contract, charged on
  entry.
- **Polymarket** — read from the market payload's own
  `feeSchedule: {rate: 0.05, exponent: 1, takerOnly: true}`, which is
  `0.05 × min(p, 1−p)` per contract.

A charge on entry is not a rate on winnings with some equivalent number: it is
due whether or not the contract settles your way. Both shapes are modelled
separately in `src/commission.py`.

---

## Notes on settlement

A commission changes how much a leg pays. What a venue does with a game that is
never played decides whether the leg is part of a hedge at all, and the venues
here do three different things. Quoted from the captured payloads:

- **Sportsbooks and the exchanges** — void a cancelled or long-postponed fixture
  and return the stake. Matchbook, Smarkets and SX Bet are in this regime with
  the books: their markets void with the underlying event.
- **Kalshi** — `rules_secondary`, on every one of the 354 captured markets: *"If
  this game is postponed or delayed, the market will remain open and close after
  the rescheduled game has finished (within two days). If the game is cancelled
  or rescheduled to over two days away, the market will resolve to a fair price
  in accordance with the rules."*
- **Polymarket** — market `description`: *"If the game is postponed, this market
  will remain open until the game has been completed. If the game is canceled
  entirely, with no make-up game, this market will resolve 50-50."*

So a book leg against a Kalshi leg on a rained-out game leaves the book refunding
while Kalshi settles from the make-up game — a naked one-sided bet for the whole
Kalshi stake, on a position that was reported as risk-free. Against Polymarket, a
contract bought at 0.75 returns 0.50 while the book returns everything.

`src/settlement.py` records the regime per source and the registry refuses a
source that declares none, exactly as it does for commission. The rules are not
*modelled* — each venue reserves discretion the payloads do not pin down, and a
caveat that overstated its own precision would be worse than one that says
plainly what is unknown. What is modelled is the only thing the engine needs:
which venues void together. Every position spanning two regimes carries the
note.

---

## Traps the adapters had to be corrected for

Recorded because each was found only by reading a venue's bytes against another
venue's, and every one of them was silent — valid rows, healthy sources, no
finding of any kind.

- **Bovada writes an Asian split line as two fields.** `price.handicap` and
  `price.handicap2` are the two halves the stake divides between, always exactly
  half a point apart (54 of 54 in the fixtures, 114 of 114 live). The line is
  their midpoint. Reading only the first published a −0.25 handicap as a
  draw-no-bet, which is a different contract, not a near-miss: **41 of 43
  reported positions on a live slate were built on a mis-stated line**, and
  reading the pair correctly took that run from 43 opportunities to 2.

- **Kalshi does not publish `no_ask_size_fp`.** It appears in none of the 354
  captured markets, so 255 of 590 rows carried no bankroll cap and were sized
  entirely from the other leg. Buying NO at its ask is selling YES at its bid —
  `no_ask == 1 - yes_bid` holds exactly on all 354 — so the depth is the YES bid
  size. One of the caps that reappeared is $0.12.

- **Matchbook's `odds` means whatever `odds-type` says.** Both fields are on all
  17,057 captured prices and agree, because the type is `DECIMAL` on every one.
  The dangerous direction is AMERICAN: a true decimal 4.00 arrives as `+300` and
  would be recorded as decimal 300.0 — an implied probability of 0.33%, inside
  the plausibility band, on every underdog. The adapter reads `decimal-odds`,
  which is unconditional.

- **SX Bet's `type` is not a global vocabulary.** Type 52 is the match winner in
  tennis and a draw-loses two-way in soccer. One flat table held only the
  US-sport codes, so the whole soccer and tennis book was fetched and discarded
  while the source declared eighteen leagues and delivered three. The table is
  keyed by `(sport, type)`, which made it safe to add tennis and correct to leave
  soccer out until someone maps it deliberately.

- **Pinnacle's `isLive` is a product flag, not a clock.** It says whether a
  matchup has moved to the in-play book, which is not whether the game has begun:
  248 rows across 18 fixtures carried a start time in the past, all `isLive:
  false`, all emitted as active — including a tennis match two hours old priced
  at 1.01. Every other adapter already compared the start against the capture.

- **Pinnacle routes unrecognised soccer competitions to one catch-all league,**
  and the competition name was then read by nothing — so `Club Friendlies Women`
  Utrecht v De Graafschap produced a key byte-identical to the men's fixture. The
  marker is now taken from the competition and appended to the club, for club
  sports only: a player is the same player whatever the draw is called.

- **Kambi's betoffer responses carry no `range` field.** The truncation guard read
  `range.total` and therefore returned `None` unconditionally, which killed both
  the fetch-time batch-halving retry and the parse-time rejection. The `events`
  array is real and echoes the request exactly, so a shortfall there is the same
  signal against bytes that exist.

---

## What this leaves

**Eleven registered sources** today. Expanding past that means opening the
Akamai / CloudFront / Cloudflare books with impersonation and, when needed, a
proxy — that work is in scope now.

Four of the ten are exchanges or prediction markets, whose two-sided quotes and —
for two of them — stated liquidity make a position *more* checkable than any
sportsbook's posted price does.
