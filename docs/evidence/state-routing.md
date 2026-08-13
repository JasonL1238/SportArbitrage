# State routing: what each licence actually answered

Per-state route evidence: what a licensed host returned from matching egress,
which routes were promoted on it, and which feeds cover which book.

## theScore Bet: the anonymous surface, measured end to end — 2026-08-13

From the operator's **Illinois** egress (fingerprint `34e56847c7ec`), with
`python scripts/recon_sources.py thescore --state IL` in `--mode http` and then
`--mode chrome --include-all --capture-dom --wait-ms 45000`. Three manifests
under `data/research/thescore/IL/`, stamped 20260813T204836Z, 20260813T204855Z
and 20260813T205527Z — 163 responses and 3 websockets on the last. (That tree is
ignored runtime output and is wiped periodically; the findings are recorded here
because the manifests are not durable.) **This supersedes "Parser/capture work
remains" with the actual shape of the work.**

**The host chain, no longer inferred.** `sportsbook.thescore.bet` serves a
Next.js shell (200, 3,501 B, empty `__next`). `/env.js` publishes
`NEXT_PUBLIC_SPORTSBOOK_API_URL = https://sportsbook.us-default.thescore.bet/graphql`
and a websocket twin at `…/graphql/websocket`. The "edge redirect" the 2026-08-03
note described is a **302 on the API, not on the page**: `Startup` is issued to
the *default* edge and answers `302` to the `us-il` one, after which all 19
subsequent operations go to `sportsbook.us-il.thescore.bet`.

**Persisted queries are `GET`, not `POST`**, at
`/graphql/persisted_queries/{sha256Hash}?operationName=…&variables=…&extensions=…`.

**But the endpoint does not require them.** A plain
`POST {"query":"{__typename}"}` to the default edge returned
`{"data":{"__typename":"RootQueryType"}}` — **200, anonymous, no hash, no token**.
Introspection is disabled (`"GraphQL introspection is disabled."`). So the hash
map is how the *client* talks, not a gate: an adapter may send query documents
directly, which removes the rotating-hash failure mode, the bundle fetch that
precedes it, and the `connectToken` that the persisted-query URL carries in its
`variables` parameter. **The query text still has to be learned from what the
application issues** — introspection being off means the schema cannot be asked
for, and inventing a query is inventing an endpoint.

**Twelve anonymous operations, all 200, from the home page alone**: `Startup`
(17 KB), `SportsMenu` (95 KB), `FeaturedMarketsCarouselNode` (130 KB),
`MarketplaceShelves` (93 KB), `PromotionsCarouselNode`, `ChipsCarouselNode`,
`DeconstructedMarketplace`, `Betslip`, `AccountMenu`, `LiveEventsCount`,
`CasinoMenu`, `ReferralTrackerCardNode`. `CompetitionPage` and
`CompetitionPageSectionLinesTabNode` need a click into a league and are not yet
captured. A live subscription runs on
`wss://sportsbook.us-il.thescore.bet/graphql/websocket` — 8 frames sent, 42
received — so prices also push, which no adapter here consumes today.

**The exit IP is real and its field is named.**
`data.startup.regionalMetadata.ipAddress` carries it. It reached the manifest as
`[redacted]` only because `_IPV4` catches it *by shape* — `ipAddress` is not in
`_SENSITIVE_KEYS`, so nothing keyed on the name would have saved it. Beside it:
`currentRegionCode: "US-IL"`, `ipAddressShortRegionCode: "IL"`,
`ipAddressCity: "Chicago"`, `validRegion: true`, and an `anonymousToken` (which
*was* caught by name). Those region fields are the cross-check that catches a
correct host reached from the wrong egress.

**Orientation is stated, not inferred — the highest risk in the adapter is
retired.** `MarketSelection.type` is literally `AWAY_MONEYLINE` / home twin, and
`LiveApiBaseballEvent` carries explicit `homeTeam` / `awayTeam` objects. Two
independent witnesses, so the rule is "require agreement, reject on
disagreement" rather than any separator parsing.

**One trap, and it is the DraftKings `trueOdds` trap again.** `Odds` carries
both `formattedOdds: "+260"` and an unrounded `numeratorLong` /
`denominatorLong` pair — `13515/3751 = 3.603039`, i.e. **American +260.3, not
+260**. The display string is rounded and an adapter that reads it understates
every price. Use the long pair.

Vocabulary for the adapter: `Market.type` (`MONEYLINE`), `Market.status`
(`OPEN`), `Market.updatedAtTime`, `MarketSelection.points`,
`Competition.slug` (`nfl-preseason`), `Organization.slug` (`united-states`),
`Participant.abbreviation` / `fullName` / `resourceUri`, and `TsbDeepLink.webUrl`
(`/sport/baseball/organization/…`) which is what a `betlinks` event URL would be
built from.

**Pennsylvania is still unmeasured over HTTP.** Its edge exists (below), but
nothing has asked it anything, and DNS is not a route.

## Three first-party host maps, measured by DNS with negative controls — 2026-08-13

Egress-independent: DNS resolution does not depend on which state the query
comes from, so these were taken from the operator's default egress and are
reproducible anywhere. **A resolving host is not a board** — none of this says
what any of these hosts serves, only which ones exist. What makes it evidence
rather than trivia is the *controls*: each pattern was probed with a name that
should not exist, and every one of those refused.

| host | result |
| --- | --- |
| `sportsbook.thescore.bet` | resolves (Cloudflare `104.17.242-246.50`) |
| `sportsbook.us-il.thescore.bet` | resolves — confirms the 2026-08-03 redirect target |
| **`sportsbook.us-pa.thescore.bet`** | **resolves** |
| `sportsbook.us-nj.thescore.bet` | resolves |
| `sportsbook.us-zz.thescore.bet` | **NXDOMAIN** ← control |
| `nonsense-control-xyz.thescore.bet` | **NXDOMAIN** ← control |

The `us-zz` control is the load-bearing one: it rules out a wildcard *inside the
`us-XX` pattern*, so Pennsylvania's edge is a real per-state host rather than an
artifact of asking. That retires the "PA edge hostname is unmeasured, do not put
it in `jurisdictions.py`" caveat **at the DNS layer only** — what it answers from
Pennsylvania egress is still unmeasured, and that is what a route needs.

**Fanatics: the "no host exists" reading was wrong, and `venues.md` said so.**
That table recorded `NXDOMAIN` under both transports with next lever "find
current host". The current host was found:

| host | result |
| --- | --- |
| `sportsbook.fanatics.com` | resolves (Akamai) — but redirects to marketing, per 2026-08-12 |
| `sportsbook.betfanatics.com` | resolves |
| **`sportsbook.1il.betfanatics.com`** | **resolves** |
| **`sportsbook.1pa.betfanatics.com`** | **resolves** |
| `sportsbook.1nj.` / `1dc.` / `1oh.betfanatics.com` | resolve |
| `sportsbook.1ca.betfanatics.com` | **NXDOMAIN** ← control |
| `sportsbook.1zz.betfanatics.com` | **NXDOMAIN** ← control |
| `sportsbook.il.betfanatics.com` (no prefix) | **NXDOMAIN** ← control |
| `sportsbook.2il.betfanatics.com` (reindexed) | **NXDOMAIN** ← control |
| `nonsense-control-xyz.betfanatics.com` | **NXDOMAIN** ← control |
| `api.betfanatics.com` | NXDOMAIN |

The pattern is exactly `sportsbook.1{state}.betfanatics.com`, the `1` is a
constant rather than an index, and **the map tracks licensure**: `1oh` resolves
and `1ca` does not, which is the difference between a state where Fanatics is
licensed and one with no legal sports betting at all. `1dc` resolving matches
`an_fanatics`' DC id 3679.

This does **not** overturn the 2026-08-12 closure below, which rests on the
operator checking the *app* and finding the board behind a login. It overturns
one premise of it — that there is no web host to find. The `404` readings from
`sportsbook.1il.betfanatics.com` that the closure note called moot were taken
from third-party egress; from Illinois egress this host has never been asked.
`src/sources/research.py` now points there instead of at the marketing redirect.

**bet365**, for completeness, re-confirming the 2026-08-13 measurement already
relied on: `www.il.bet365.com`, `www.pa.bet365.com` and `www.nj.bet365.com`
resolve via Cloudflare; `www.zz.bet365.com` and `nonsense-control.bet365.com`
are NXDOMAIN. The research profile's stateless `https://www.bet365.com/` — the
origin every failed probe used — is corrected to the per-state host.

## Caesars is gated by an AWS WAF token, not by egress — and its odds are not on REST at all — 2026-08-12

Captured with `python scripts/recon_sources.py caesars --state IL --mode chrome
--include-all --capture-dom --wait-ms 45000` from the operator's **Illinois**
egress (`egress_state.json` state `IL`, fingerprint `34e56847c7ec…`), against the
page the operator confirmed the same day shows odds **while logged out**:
`sportsbook.caesars.com/us/il/bet/`. 146 responses, 38 websockets, manifest
`data/research/caesars/IL/20260812T175756Z/`.

**Real Chrome, logged out, Illinois, and the v4 endpoints still 403.** Every 403
body is the same 919-byte CloudFront page:

| path under `https://api.americanwagering.com/regions/us/locations/il/brands/czr/` | Chrome |
| --- | --- |
| `sb/features` | 200 (12,474 B) |
| `sb/bets/configuration` | 200 (579 B) |
| `sb/v3/sports-menu` | 200 (33,437 B) |
| `sb/v3/teamMetadata` | 200 (84,624 B) |
| `sb/v4/home` | **403** |
| `sb/v4/navigation-items` | **403** (twice) |
| `sb/v4/sports/homepage/quick-picks` | **403** |
| `gw/growth/v4/sports/homepage/banners` | **403** |

So **v4 is not what a browser gets either**, and the long-standing plan to
"re-point the adapter from v3 to v4" is dead on the evidence rather than
untried. It was already recorded as measured in the 2026-08-04 session and
mis-carried forward as an open variable.

**The gate is the token, and this is the decisive pair.** The same two v3 paths
were then requested through the repository's own default transport
(`build_default_client`, Chrome-impersonated `curl_cffi`) from the same egress,
minutes later:

| path | Chrome | `curl_cffi` |
| --- | --- | --- |
| `sb/features` | 200 | **200** |
| `sb/v3/sports-menu` | 200 | **403** |
| `sb/v3/teamMetadata` | 200 | **403** |

One endpoint answers both clients; two answer only the browser. The exit IP is
identical, the state is identical, the TLS fingerprint is Chrome in both cases —
`curl_cffi` already impersonates it, and has since before any of these probes.
What the browser has and the client does not is an **AWS WAF token**: the page
loads `b470c5d1aeb4.edge.sdk.awswaf.com/…/challenge.js`, posts `mp_verify`, and
carries telemetry to the same host. That is the whole difference.

This retires three explanations that have each cost a session: it is not egress
identity (five books answered from this exact egress in run 6 while Caesars
alone was blocked), not geolocation, and not TLS fingerprinting. **Deriving the
token outside the browser is defeating an anti-automation control and is out of
scope permanently** — not a judgement call to revisit, see `venues.md`'s "never
bypass a challenge" and `AGENTS.md` § Scrape.

**Odds never traverse REST.** `v3/sports-menu` is a 19-sport catalogue —
`sportId`, `name`, `eventCount`, `competitions`, `displayOrder` — with **no
prices anywhere in it**, and
`https://api.americanwagering.com/regions/us/locations/il/brands/czr/sb/v3/events/highlights/`,
the path `src/sources/caesars.py:151` actually asks for, **was never requested by
the page at all**. The prices arrive over a Diffusion push socket:

```
wss://api.americanwagering.com/regions/us/locations/il/brands/czr/diffusion?ty=WB&v=25&ca=10&r=0
```

binary frames, 8 sent / 9 received in the capture window (`ty=WB` = WebSocket
binary, `v=25` = Diffusion protocol 25). The other 34 sockets are GeoComply
(`wss.plc-gc.com:9703-9705`, 11 each, no frames) and one
`be-push.us.williamhill.com` — William Hill being Caesars' platform.

So a working Caesars adapter needs **both** a browser session that legitimately
holds the WAF token **and** a Diffusion client speaking a binary push protocol —
not a re-pointed URL. That is a materially larger build than any prior estimate,
and it is the honest reason this book has stayed unreachable while five peers
were promoted.

### Fanatics: closed — the odds board is behind a login — 2026-08-12

The operator checked the Fanatics app directly on 2026-08-12: **the board is not
browsable pre-login.** Combined with there being no browser sportsbook at all
(`sportsbook.fanatics.com` redirects to the marketing site `betfanatics.com`),
this closes the venue for a repository whose whole collection model is anonymous.

There is no remaining first-party path, so **Fanatics is not a reachability
question any more and should not be reopened as one**. It stays a required book
in both IL and PA (`an_fanatics` ids 2990 / 2791), which means it stays at
`SINGLE_SOURCE` until a **second same-licence republisher** exists — that, not
first-party work, is the only thing that moves it. The earlier `404` readings
from `sportsbook.1il.betfanatics.com` were taken from a third-party egress, were
never re-measured from Illinois, and are now moot.

## Pennsylvania first-party routes promoted — 2026-08-08

`configured=PA detected_egress=PA` throughout (each run's collect printed the
match; the egress *fingerprint* is not persisted per run, and the stored
`egress_state.json` was re-detected — with a rotated IP, hence a new
fingerprint — 3 ms before run 32, so no per-run fingerprint claim is made
here). Three routes moved TEMPLATE → VALIDATED on the bar
`../MULTI_STATE.md` sets — parser-clean quotes through matching egress, twice each:

| book | runs | quotes | rejections | replay |
|---|---|---|---|---|
| FanDuel (`sbapi.pa.sportsbook.fanduel.com`, `state=pa`) | 29, 30 | 1579 / 1967 | 0 / 0 | PASS / PASS |
| BetRivers (`rsiuspa`, `market=US-PA`) | 28, 29 | 3699 / 3698 | 0 / 0 | FAIL* / PASS |
| DraftKings (`US-PA-SB`) | 29, 31 | 130 / 130 | 0 / 0 | PASS / PASS |

\* Replay has no per-source filter. Run 28's FAIL is entirely DraftKings'
pre-trueOdds-fix rows drifting against the fixed parser; BetRivers' own run-28
rows replay without drift. Its second fully-PASS run is 29.

Two defects had to fall first, both caught by this validation pass:

- **DraftKings recorded the page's rounding, not the price.** The
  `sportscontent` payload carries `trueOdds: 1.46948357` beside
  `displayOdds.decimal: "1.46"`, and the converter preferred the printed string.
  Measured on this capture, `trueOdds` agrees with the published American price
  on **122 of 122** selections, the display value on only about two-thirds
(66-68 depending on the comparison metric), drifting to 2.06%.
  Fourteen `odds_format_mismatch` errors failed the run; the parser now prefers
  `trueOdds`. Run 28's stored DraftKings rows predate the fix and its replay now
  FAILs with exactly that drift — the replay checker refusing pre-fix evidence,
  which is why DraftKings' second clean run is 31, not 28.
- **FanDuel's `TO_RECORD_*` player-threshold props were neither collected nor
  declared**, so `TO_RECORD_6+_REBOUNDS_WNBA` and the 8+ sibling rejected run
  28. Declared out of scope as a family (`TO_RECORD`), not as the two instances,
  because the threshold and statistic both vary.

What a PA run says after promotion (run 32, core tier, 4627 quotes):
`state_native_sources_below_two` is gone, three books cross-compare, 2
opportunities from 172 cross-book markets, and the remaining ERRORs are two:
`required_book_missing: PlaySugarHouse` — the deliberate sentinel — and
DraftKings' `scopes_refused` (the eventgroup denials below, equally deliberate
in staying loud).

Still TEMPLATE, with the day's measurements on the route:

- **BetMGM** — HTTP 400 `Access id not allowed for application`: the configured
  access id is another state's. The PA web app has to yield its own id
  (recon, not guessing).
- **Caesars** — blocked at the CDN edge (`request blocked` marker) from PA
  egress, so egress alone did not clear the refusal seen from IL.
- **DraftKings caveat**: eventgroups 94682, 42133 and 40253 answer `access
  denied` from this egress, so MLB and tennis produce while WNBA/NFL/NHL do
  not. `scopes_refused` keeps that failing loudly; the VALIDATED status claims
  the route is real, not that the shelf is whole.

### The links the promotion made wrong, and the suite that texted — 2026-08-08

Second adversarial round over this work, two findings that matter beyond prose:

**The arb-link layer was state-blind while the promo layer was not.**
`betlinks.SITE` pinned `betrivers_kambi` to `il.betrivers.com` and `caesars` to
`/us/il/bet`; `jurisdictions.PromoRoute` had carried the correct per-state
BetRivers doors all along. Promoting PA made it operative: run 32's two real
opportunities each carried `il.betrivers.com` on a leg priced from
`rsiuspa`/`US-PA`, under "PLACE BOTH NOW" — another licence's site, which does
not show this state's slip. `bet_link` now takes the governed state and a
`STATE_SITE` table resolves BetRivers/Caesars/Unibet per jurisdiction (agreement
with the promo layer pinned by test); the SMS, the dashboard payload and the
promo planner all thread it through. Ungoverned runs get a stateless brand door,
never a confidently wrong state's.

**The test suite was texting the operator.** Five CLI tests drive
`main(["arb"])` over a genuine 4.76% synthetic arb; `alert_ready()` is true by
default on a signed-in Mac, so every full-suite run reached osascript with the
real number — unnoticed because `notify` is fail-soft and the tests pass either
way. The hermetic conftest fixture now replaces `DEFAULT_BOOK.send` with a
refusal, pinned by a test whose identity check fires before anything could
deliver.

Also from the round: `an_open` was the one Action Network descriptor issuing a
`bookIds`-less v2 request — a shape no capture has ever measured — on every
batch's global pass. It now names id 30, and a test keeps every live AN request
on the named-ids shape across the global list and all four states.

## The Pennsylvania recapture — 2026-08-08T16:38Z

`configured=PA detected_egress=PA` (fingerprint `78e5c3810511` as printed by
`detect_state.py` two minutes before the run; fingerprints are not persisted
per run, and the connection's IP had rotated by the day's later runs), 12:38 ET
on a Saturday, so the whole slate was pregame — the condition the 2026-08-07
attempts lacked. Run 27, thirteen sources asked, alerts off.

| source | id | brand | quotes | events |
|---|---|---|---|---|
| `an_betrivers` | 122 | BetRivers PA | 376 | 35 |
| `an_parx` | 74 | Parx | 376 | 35 |
| `an_caesars` | 1906 | Caesars PA | 331 | 35 |
| `an_fanduel` | 255 | FanDuel PA | 331 | 35 |
| `an_unibet` | 246 | UnibetPA | 316 | 35 |
| `an_draftkings` | 1534 | DK PA | 301 | 35 |
| `an_betmgm` | 280 | BetMGM PA | 267 | 35 |
| `an_thescore` | 4623 | theScore Bet PA | 250 | 19 |
| `an_bet365` | 3547 | bet365 PA | 173 | 19 |
| `an_fanatics` | 2791 | Fanatics PA | 150 | 15 |
| `an_fliff` | 2292 | Fliff | **0** | 0 |
| `an_circa` | 78 | Circa | **0** | 0 |
| `an_superbook` | 14 | Westgate | **0** | 0 |

**2871 quotes across ten of Pennsylvania's twelve republished feeds**, where nine
of them had been dark. betPARX, theScore Bet PA and Unibet PA produced rows for
the first time. The three zeros are the ids that carry no odds at all, recorded
above.

MLB captures for `an_parx`, `an_thescore` and `an_unibet` replaced the committed
fixtures, which asked New Jersey ids (1929 / 247 / 4620) through v1 and parsed to
nothing. Offline, those three now parse to 255 / 225 / 195 rows with **no
rejections** — the `periods=` fix demonstrated end to end rather than argued.
Windows per book, measured on the committed bytes: 74 and 4623 carry
`full_game` + `first_5_innings` + `first_1_inning`; **246 has no `firstinning`
key on any of its 15 games** (full-game and first-five only), so a missing F1
row for Unibet PA is that book's shelf, not a defect.

The run raised `prices_disagree_with_every_other_source` against `an_caesars` —
30 of 312 shared markets more than 0.15 of implied probability from the rest,
worst 0.35, on ATH@BOS, ATL@NYY, BAL@TEX and others. **All thirty are
`moneyline/first_1_inning`, and Caesars is right.** From the raw payload for
ATH@BOS:

| book | first-inning moneyline | vig |
|---|---|---|
| 122 BetRivers PA | home +210, away +400, **draw −129** | 8.6% |
| 1906 Caesars PA | home −210, away +170, **no draw** | 4.7% |

Two coherent books pricing **different bets**. The three-way home leg loses when
the inning is scoreless; the two-way one does not, which is the whole 0.323 →
0.677 difference. betPARX and theScore Bet post the three-way market too, so
Caesars was the minority of one and got graded for it.

No position was ever at risk: `src.arb` groups by `contract_shape` and refuses a
two-way moneyline in a draw-pricing window outright (`ambiguous_tie_settlement`),
which is a rule that already names first-five-innings as its case. The defect was
confined to the report — but an ERROR firing every run for a benign reason is how
a report stops being read, so `_check_price_agreement` now carries the contract
shape in its grouping key, the same distinction `src.arb` was already making. The
Pennsylvania board revalidates with that error gone and the swap detection intact.

Two things this run did **not** settle:

- Every republished row is view-only, so the run reports 0 comparable markets and
  0 arbitrage. Correct, not a regression: none of these is a counterparty.
  Pennsylvania still has no first-party route promoted, which is the next step.
- `an_fanatics` (15 events) and `an_bet365` (19) cover materially less of the
  board than the 35 the rest return. Not investigated.

## Multi-state routing update — 2026-08-03

Live collection now supports IL, PA, NJ, and DC batches. The detected current
state is always included; additional states are explicit. Caesars and Hard Rock
NJ values now live only in the NJ route map. Illinois uses only
`locations/il`/`segment=il`; failed exact-state requests remain failed and never
fall back to NJ. NJ/DC/PA route records are templates pending matching-egress
parser-clean probes. Republished Action Network, VegasInsider, and VSiN rows are
global diagnostic observations and cannot form arbitrage legs.

## Pennsylvania routing status — 2026-08-03

Pennsylvania is configured but not yet live-validated. The active-state map
selects FanDuel `pa`, BetRivers `rsiuspa` / `US-PA`, BetMGM's PA host and
subdivision, DraftKings `US-PA-SB`, and Caesars `locations/pa`. FanDuel promos
send only `PA`; BetRivers promos request only `pa.betrivers.com`.

Hard Rock is not in the Pennsylvania Gaming Control Board's authorized online
sportsbook list, so the map marks it unavailable instead of inventing a PA
segment. The registered adapter and comparison feeds remain in the repository.

An Illinois-exit template probe on 2026-08-03 produced parser-clean payloads for
FanDuel (72 MLB quotes), BetRivers (486), and DraftKings (86). BetMGM returned
HTTP 400 (`Access id not allowed for application`), so its PA access ID still
needs discovery from the public PA application. Caesars was blocked at
CloudFront. Hard Rock is unavailable in PA. These are structural results only:
3/5 candidate routes produced quotes, and none was promoted to validated.

The same session's public egress resolved to Illinois, so the real PA probe was
correctly refused. A later automated lookup attempt from the managed development
environment was also blocked by its privacy approval layer; no PA `ok` cache
rows were written. Live certification still requires running the commands below
from a legitimate Pennsylvania connection and consenting to the IP lookup.

From Illinois, inspect route structure without creating validation evidence:

```bash
ODDS_STATE=PA python scripts/probe_sources.py --state PA --template-only
```

To promote individual PA routes, run `detect_state.py` on a legitimate PA
egress, then run `probe_sources.py --state PA --force`. Only parser-clean quotes
under a matching recent detection can write `ok` to the separate cache.

## Illinois re-probe — 2026-08-03

This is the current baseline from the Illinois connection. It supersedes the
older California reachability notes below; those remain as history because they
show which failures are egress-sensitive.

The Illinois Gaming Board's current [authorized sportsbook
list](https://igb.illinois.gov/sports-wagering/sports-authorized-operating-sportsbooks.html)
names ten online operators: bet365, BetMGM, BetRivers, Caesars, Circa,
DraftKings, theScore Bet, Fanatics, FanDuel, and Hard Rock Bet. "Available in a
comparison table" does not by itself mean an operator is legal or usable in
Illinois. In particular, the IGB has issued cease-and-desist letters to
[Polymarket, Kalshi, and Bovada](https://igb.illinois.gov/sports-wagering/cease-and-desist-letters.html).

**Operator decision, 2026-08-09:** asked directly how Kalshi and Polymarket US
should be presented on Illinois runs given those letters, the operator chose
"keep as-is (takeable)" — no per-state exclusion table is added. What that
mechanically means today: **Kalshi** stays in the nationwide-takeable set on
IL runs, unchanged. **Polymarket** has no takeable referent to keep: the one
registered `polymarket` key reads the offshore book and sits in
`US_UNAVAILABLE_SOURCE_KEYS`, and no `polymarket_us` key exists yet — nothing
named Polymarket is takeable on an Illinois run today. The decision's
Polymarket half is therefore *prospective*: when `polymarket_us` is registered
(after the operator's KYC and keys), it enters the takeable set without an IL
carve-out unless the operator revisits this. (Bovada, the third letter
recipient, is US-unavailable on independent grounds.) An earlier revision of
this note said these feeds "must not be presented as Illinois betting
counterparties"; that wording described the letters' position, and the
recorded decision above — not this note — is the repository's answer. The
letters themselves remain a fact worth re-checking if the IGB escalates.

### First-party acquisition follow-up — 2026-08-04 UTC

All results below used the recorded Illinois egress fingerprint and anonymous
application traffic. Research capture stores only sanitized URLs, safe headers,
response bodies, text WebSocket frames, and the egress hash under ignored
`data/research/`; it never persists the exit IP, proxy URL, cookies, or tokens.

- **Hard Rock IL is repaired and live-validated.** The application proved
  segment `il`, channel `ILLINOIS_ONLINE`, millisecond event timestamps, and
  selection-name handicaps. Two runs returned 202 quotes with zero rejections;
  a later probe returned 172 with zero. A genuine dated RawResponse set replays
  to more than 100 quotes without network access.
- **Caesars IL remains blocked at the first-party core-odds boundary.** The
  exact IL application, v3 sports menu, metadata, and sockets answer, but v4
  navigation/home/quick-picks and current v3 MLB highlights return CloudFront
  403 in headed, headless, and persistent sessions. This needs operator-approved
  access or a naturally successful anonymous response, not WAF bypass code.
- **Fanatics currently exposes sportsbook odds through its native apps.** The
  [operator describes the sportsbook as an iOS/Android
  experience](https://www.fanaticsinc.com/fanatics-sportsbook-online-experience);
  its sportsbook web host redirects to the marketing site and yields no
  anonymous first-party sportsbook payload. Native-client research remains,
  without login or untrusted APK installation.
- **bet365 IL is viable but not registered.** A normal browser displays a full
  anonymous Illinois MLB slate, and the exact IL configuration plus compact
  publisher protocol were identified. Fresh isolated Chromium and stable-Chrome
  profiles load the correct IL shell but their catalog subscription stalls at
  the application preloader, so unattended collection is not yet repeatable.

The exact identifiers proved that day, so a later session does not re-derive them:
Caesars IL answers under `sportsbook.caesars.com/us/il/bet/` and its v3 sports menu
proved the hard-coded competition list stale (current MLB id
`04f90892-3afa-4e84-acce-5b89f151063d`, now in `src/sources/caesars.py`); bet365 IL is
`www.il.bet365.com` with state code 28, IL locale, the compact pull-pod protocol and
`365lpodds.com` sockets; Fanatics' `sportsbook.fanatics.com` redirects to the
`betfanatics.com` marketing site. The generated Caesars and bet365 browser profiles
were deleted after diagnosis, so no anonymous session cookies were retained — only the
sanitized ignored manifests remain.

### Redundant feed coverage

Republished rows retain their own source keys for provenance but are one
counterparty with the underlying sportsbook. `src.redundancy` prevents any two
of those observations from becoming opposite legs of an arbitrage and flags
material price drift instead.

| Book | First party | Action Network | Independent comparison | Current Illinois result |
|---|---|---|---|---|
| DraftKings | `draftkings` | `an_draftkings` | `vi_draftkings` | **Three paths registered.** Current first party uses the official `sportscontent` endpoint through browser transport: 48 MLB quotes, zero rejections. AN and VI both produced quotes. |
| Caesars | `caesars` | `an_caesars` | `vi_caesars` | Three paths registered; first-party CloudFront is blocked, while AN and VI produced MLB quotes. |
| FanDuel | `fanduel` | `an_fanduel` | VI column verified, adapter not registered yet | First party 48 MLB quotes; AN 112. |
| BetMGM | `betmgm` | `an_betmgm` | VI column verified, adapter not registered yet | First party 48; AN 104. |
| BetRivers | `betrivers_kambi` | `an_betrivers` | VI `RiversCasino` column verified, adapter not registered yet | First party 476; AN 136. |
| Hard Rock Bet | `hardrock` | `an_hardrock` | `vi_hardrock` | **Three paths registered.** First party now returns current IL core markets parser-clean; AN v2 and VI provide diagnostic comparison rows. |
| bet365 | not implemented | `an_bet365` | `vi_bet365` | AN and VI produce comparison rows. A normal browser shows first-party IL odds, but fresh isolated automation does not complete its catalog subscription. |
| Fanatics | not implemented | `an_fanatics` | `vi_fanatics` | AN v2 and VI produce comparison rows. The current web host is marketing-only; native-app first-party discovery remains. |
| Circa | app-only surface; no web adapter | `an_circa` | `vsin_circa` | Circa's own site points to VSiN as an odds aggregator. The named VSiN column produced 48 MLB quotes / 8 events with no rejections. It is a Las Vegas line tracker, so use it as fallback and drift evidence rather than proof of Illinois-state price identity. *(2026-08-09: `an_circa` has since been deregistered — see `action-network.md` § "Three registered ids carry no odds"; `vsin_circa` is now Circa's only feed.)* |
| theScore Bet | verified IL GraphQL surface; adapter pending | none | none | Official `us-il` edge returned a valid anonymous startup, MLB competition, and lines payload. This is the strongest next first-party adapter candidate. *(2026-08-10: superseded — `an_thescore` now carries IL id 4601 and is proven: 74 soccer rows, run 35, replay PASS; see `action-network.md` § "theScore Bet's Illinois id answers". The "none" in the Action Network column describes the 2026-08-03 catalogue state.)* |

The retained Action Network feeds were rechecked from Illinois on 2026-08-03.
The old v1 scoreboard returned games but omitted all six requested books. The
current Action Network web board revealed a v2 endpoint and grouped `markets`
schema. After adding dual-schema parsing, v2 restored real 48-quote MLB captures
for Hard Rock, Fanatics, and Bally. Fliff, Circa, and SuperBook still returned
zero requested-book rows across MLB, WNBA, NFL, NHL, and soccer. Those remaining
three cannot supply the real, non-empty captures required by the offline
source contract. *(This paragraph's ending is superseded: on 2026-08-09 the
operator approved deregistering all three rather than waiting — see
`action-network.md` § "Three
registered ids carry no odds" — and the full suite is green. `vsin_circa`
remains registered as Circa's fallback.)*

### Pennsylvania: no book reaches the two-feed bar

Recorded so this is not rediscovered. Under the rule in `AGENTS.md`
§ Scrape, a book counts as observed through one first-party route **or** two
republished feeds carrying *Pennsylvania's own* licence that agree on price.

**No PA book reaches the two-feed bar.** Ten of the eleven file exactly one
same-licence feed — Action Network's per-state book id — and PlaySugarHouse files
none at all. The only second feed available anywhere is VegasInsider, whose
`/odds/las-vegas/` column is a different licence of the same brand; it is
recorded as context and excluded from both the count and the price comparison.

That is a statement about the *second* path only. Five books are satisfied
through the **first** path — a first-party route — and the table's own "best
reachable state" column says so. The six with no first-party route are the ones
with no way to comply at all.

| Book | First party | Same-licence feed (AN book id) | Cross-licence only | Best reachable state |
|---|---|---|---|---|
| BetMGM | `betmgm` | `an_betmgm` (280) | `vi_betmgm` | `DIRECT` |
| BetRivers | `betrivers_kambi` | `an_betrivers` (122) | `vi_betrivers` | `DIRECT` |
| Caesars | `caesars` | `an_caesars` (1906) | `vi_caesars` | `DIRECT` |
| DraftKings | `draftkings` | `an_draftkings` (1534) | `vi_draftkings` | `DIRECT` |
| FanDuel | `fanduel` | `an_fanduel` (255) | `vi_fanduel` | `DIRECT` |
| bet365 | none | `an_bet365` (3547) | `vi_bet365` | `SINGLE_SOURCE` (warning) |
| Fanatics | none | `an_fanatics` (2791) | `vi_fanatics` | `SINGLE_SOURCE` (warning) |
| betPARX | none | `an_parx` (74) — **unproven** | none | `SINGLE_SOURCE` (warning) |
| Mohegan Pennsylvania | none | `an_unibet` (246) — **unproven** | none | `SINGLE_SOURCE` (warning) |
| theScore Bet | none | `an_thescore` (4623) — **unproven** | none | `SINGLE_SOURCE` (warning) |
| PlaySugarHouse | none | none | none | `MISSING` (error) |

**The three unproven feeds.** `an_parx`, `an_unibet` and `an_thescore` are the
*only* observation path for their books, and no capture in the repository shows
any of them returning a row. The captures taken 2026-08-07T01:55Z asked for the
New Jersey ids 1929 / 247 / 4620 and came back carrying odds for other books on
the same games (`{15, 30, 123}`, `{15, 30, 68, 69, 71, 75}`, `{15, 30, 69, 75}`)
— the requested book was absent, not the slate. The Pennsylvania ids 74 / 246 /
4623 have never been requested. So for these three the coverage table asserts a
watcher that is not known to answer; if a live capture confirms the ids are
wrong, the honest record is no working feed rather than a thin one. Until then
they are `SINGLE_SOURCE` on paper and `MISSING` in practice. *(Resolved
2026-08-08: the recapture asked all three PA ids and every one answered —
255 / 195 / 225 rows, § "The Pennsylvania recapture". The 4623 capture was
later retired from the fixture store by the 2026-08-09 IL licence swap, its
proof standing on record — `action-network.md` § "theScore Bet's Illinois id
answers".)*

**The three new VegasInsider columns.** `vi_betmgm`, `vi_betrivers` and
`vi_fanduel` are the opposite case — the adapters work and the captures are the
problem. Their 2026-08-07T01:55Z captures parse to zero rows, but so does
`vi_caesars` against that same overnight page, and feeding the live 2026-08-03
page to each of the three new parsers yields 48 quotes apiece. They need a
recapture on a live slate, not a code change; `test_the_adapter_produced_rows_at_all`
would currently fail on all six, masked only by the pre-existing
`an_fliff` / `an_circa` / `an_superbook` fixture gap that errors first.
*(Resolved 2026-08-10: the dead-slate captures were replaced with live
IL-egress ones parsing 18/54/54 rows, and the fixture gap is gone with the
deregistration — see `action-network.md` § "One stale board retired for vintage
coherence".)*

bet365 and Fanatics previously read as cross-checked; their second feed was the
Las Vegas column, so they no longer can. For the six books with no first-party
route the only path to compliance is a genuine Pennsylvania first-party adapter
or a second PA-licensed republisher — **not** relabelling a national feed as
local, which `src/coverage.py`'s import-time invariant refuses outright.

`an_bally` and `an_hardrock` are `UNAVAILABLE` in PA (no licence) and are not
built there.

### First-party endpoint findings

| Source | Result from Illinois |
|---|---|
| FanDuel IL | working, 48 MLB quotes / 8 events |
| BetRivers IL | working, 476 / 8 |
| BetMGM IL | working, 48 / 8 |
| DraftKings IL | **repaired**: current browser-observed `sportscontent/.../leagueSubcategory/v1/markets` route returns 43 KB JSON and parses 48 / 8; retired v5 captures still replay |
| Caesars IL and legacy NJ | both `403` / request blocked |
| Hard Rock IL | **working and validated**: current route returns parser-clean core markets; NJ remains template-only pending NJ egress |
| Pinnacle | `401 No authorization token provided`; guest-token discovery required |
| bet365 | normal browser shows full IL MLB odds; fresh isolated browser catalog subscription stalls |
| Fanatics | sportsbook web host redirects to marketing; first-party discovery remains native-app work |
| Bally Kambi | `429 No access` |
| Circa | official site says the complete real-time menu is in its mobile app; no public first-party web odds surface found. Its site explicitly lists VSiN and WagerTalk as aggregators, so `vsin_circa` is the independent fallback. |
| theScore Bet | **first party verified**: `env.js` exposes the public GraphQL host; the default edge redirects this Illinois egress to `sportsbook.us-il.thescore.bet`. Anonymous `Startup`, persisted `CompetitionPage`, and `CompetitionPageSectionLinesTabNode` calls all returned `200`. The MLB lines payload contained 47 event nodes, 55 markets, and 147 selections with structured American odds. Parser/capture work remains. *(2026-08-13: superseded — re-measured end to end, and two details here are wrong. The redirect is a `302` **on the API**, not on the page; and the persisted queries are `GET`s that the endpoint does not require at all, since a plain anonymous `POST` of a query document answers `200`. See § "theScore Bet: the anonymous surface, measured end to end".)* |

The theScore request sequence is state-sensitive and should be implemented as
one adapter rather than hard-coded curl calls: fetch the official public bundle
for current persisted-query hashes, call `Startup` on the resolved regional
edge, keep the anonymous token in memory only, then capture the competition and
lines responses. The startup payload includes the raw exit IP, so it must be
sanitized before persistence; only the region and an IP fingerprint belong in
diagnostic storage.

The full endpoint probe returned usable JSON for 15 of 22 research candidates.
Working non-retail sources included LeoVegas, Matchbook, Smarkets, SX Bet,
Cloudbet, and 1xBet. Those results establish transport health; they do not make
an unlicensed venue an Illinois counterparty.

Everything below was first measured on **2026-07-28** from a host in Davis,
California with plain `httpx`. That pass classified several big books as
permanently closed. **That policy is retired.** The collector now defaults to
Chrome TLS impersonation (`curl_cffi`) and will use `ODDS_HTTP_PROXY` when set.
Rows below that still say "closed" mean *not yet opened with the new transport*,
not *forbidden to open*. Re-probe with:

```bash
python scripts/probe_sources.py --only blocked --template-only
```

---
