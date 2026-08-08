# Which sources are reachable, which are not, and why

This is the record of what happened when every candidate source was actually
asked. It exists so that a wall is documented once rather than rediscovered every
few months by somebody writing an adapter for a host that has never answered.

Reproduce it with:

```bash
python scripts/detect_state.py
python scripts/probe_sources.py --state IL
```

## Action Network: the two endpoint versions are different catalogues — 2026-08-08

Egress: PA (`data/egress_state.json`, detected 2026-08-07T02:24Z). Both versions
asked the same book ids in the same minute, MLB / NFL / NHL / WNBA / soccer.

**Provenance, per row.** The Pennsylvania row is re-derivable from `data/raw` —
it is the 2026-08-07T02:36Z run, and both halves of it are on disk. The other
rows come from live probes run on 2026-08-08 whose **envelopes were not stored**,
so they can be re-checked only by re-running the requests. That includes the
offshore comparison, which is the sole justification for `LEGACY_V1_BASE_URL`,
and the `periods` behaviour below. Treat those as measured-but-unaudited.

| Asked | `web/v1/scoreboard` | `web/v2/scoreboard` |
|---|---|---|
| PA set `74,122,246,255,280,1534,1906,2791,3547,4623` | MLB: **none of the ten**, answered `{15, 30, 68, 69, 1270, 2668}`. WNBA and NFL: `74` and `122` only | MLB: **all ten**, plus defaults 15 and 30. Other leagues: whichever of the ten priced that league |
| offshore `21,35,2495` | Bovada on NFL; Bovada and 1xBet on soccer | **none, on any league** |
| Fliff / Circa / SuperBook `2292,78,14` | absent | absent |
| SugarHouse `708` | absent | absent |
| each id alone — `79`, `123`, `74`, `4623`, `246` | not retested | **each came back on its own** |

Four things follow, and they are why `DEFAULT_BASE_URL` moved:

1. **A wrong base URL here is silent.** v1 answered a Pennsylvania MLB request
   with HTTP 200, eleven games and plausible prices belonging to books nobody
   asked for — the exact failure mode the Scrape rules exist to prevent. Nine of
   PA's ten fetchable republished feeds were on v1; seven of them got nothing of
   their own on any league, and the other two (`an_parx`, `an_betrivers`) got
   their book on WNBA and NFL only. A partial answer is worse than none: it looks
   like it worked.
2. **v2 is not a superset.** The offshore shelf exists only on v1, so
   `an_bovada` and `an_onexbet` pin `LEGACY_V1_BASE_URL` deliberately. They are
   the only two *Action Network* sources permitted to set `base_url` — several
   first-party adapters set their own, which is a different thing — pinned in
   `tests/test_actionnetwork_v2.py`.
3. **v2 needs its settlement windows asked for.** See the next section — this
   was nearly shipped as a silent two-thirds cut to baseball coverage.
4. **The three empty tenants are not an endpoint problem.** Fliff, Circa and
   SuperBook return nothing on either version, so the v2 switch does not clear
   the fixture-contract baseline in `docs/testing.md`, and nothing about the
   request shape will.

On v2, naming an id alone is enough, so the v1-era workaround where bet365 only
appeared when Caesars was named is unnecessary. It is **not** true that every
named id always comes back — a book not pricing that league is simply absent,
which is why `parse` filters by id rather than trusting the request. The
whole-state `fetch_book_ids` set is kept only so every source in a state issues
one identical URL.

**Still unproven: whether any of it parses in Pennsylvania**, for two reasons
that are easy to run together and should not be.

*The endpoint.* On 2026-08-07T02:36Z every Pennsylvania tenant asked for the whole
PA set, but only **`an_fanatics`** was configured for v2 — `an_hardrock` and
`an_bally` hold no PA licence and are not built there at all. Per league, from
`data/raw`:

| | MLB | WNBA | NFL | soccer | NHL |
|---|---|---|---|---|---|
| the one v2 tenant | **all ten** | 9 (2791 absent) | 7 | 5 | no games |
| the nine v1 tenants | none | `74`, `122` | `74`, `122` | none | no games |

So v1 is not blind to every modern id.

Mohegan's 246 and theScore's 4623 **are** on disk with real prices — in that v2
payload, which is stored under `an_fanatics`. `parse_actionnetwork` selects on the
book id in the envelope's own label, so a capture filed under one tenant can never
produce another's rows. The ids have been observed; these two sources have not.
That is exactly the payload-contains versus parse-produces distinction, and it is
why the committed fixtures under those keys are worthless and why a capture under
the *right* key is the only thing that counts.

*The clock.* Every game in that run was `complete` or `inprogress`, so even the
rows that did come back — betPARX's 74 on WNBA and NFL — produce nothing after
the pregame filter. A capture at a dead hour is an empty fixture however healthy
the run looked.

Both faults are fixed by the same recapture: v2, at a pregame hour.

## Action Network v2 returns full-game only unless asked — 2026-08-08

Caught by an adversarial review of the change above, which had asserted the
opposite on the strength of a bad measurement: the reading that "v2 keeps F5/F1"
came from counting the payload's `market_rules` block — a schema listing, not
odds.

Parsed rows from every committed Action Network fixture, whole parse, which is
the measurement that counts:

| Fixture | Endpoint | Periods parsed | Period share, all rows / baseball rows |
|---|---|---|---|
| `an_caesars` | v1 | `full_game 30, first_5_innings 30, first_1_inning 10` | 57.1% / 57.1% |
| `an_betrivers` | v1 | `full_game 54, first_5_innings 30, first_1_inning 25` | 50.5% / 64.7% |
| `an_open`, `an_fanduel`, `an_betmgm` | v1 | `full_game 54, first_5_innings 30, first_1_inning 10` | 42.6% / 57.1% |
| `an_bovada` | v1 | `full_game 18, first_5_innings 18` — no F1 | 50.0% / 50.0% |
| `an_bet365` | v1 | `full_game 30, first_5_innings 20` — no F1 | 40.0% / 40.0% |
| `an_draftkings` | v1 | `full_game 54, first_5_innings 20, first_1_inning 10` | 35.7% / 50.0% |
| `an_onexbet` | v1 | `full_game 12` — carries no period rows at all | 0% |
| `an_hardrock`, `an_fanatics`, `an_bally` | v2 | `full_game 48` — nothing else | 0% |
| `an_parx`, `an_unibet`, `an_thescore` | v1 | parse to **zero rows** — see above | n/a |

So period rows are between a third and two-thirds of the board of any Action
Network tenant that produces one. Moving every republisher to v2 as-is would
have deleted all of them — 40 of `an_open`'s 70 pregame MLB rows — while every
source still reported healthy.

**The recovery is a request parameter, and its near misses are all quiet:**

| Sent with `bookIds` | v2 answer |
|---|---|
| nothing | `{event: 154}` |
| `period=firstfiveinnings` (singular) | `{event: 154}` — ignored |
| `periods=firstfiveinnings` | `{firstfiveinnings: 126}` |
| `periods=event,firstfiveinnings,firstinning` | `{event: 154, firstfiveinnings: 126, firstinning: 96}`, all ten PA books |
| `periods=event, firstfiveinnings, firstinning` (spaces) | `{event: 154}` — accepted and wrong |
| `marketTypes=…` | HTTP 400 |

`REQUESTED_PERIODS` is now sent on every request from every tenant. It is a no-op
on v1 — measured identical market types with and without — so there is no
conditional on the base URL to rot. The literal is pinned, spaces and all, in
`tests/test_actionnetwork_v2.py`.

Every committed v2 fixture predates this and is full-game only;
`test_the_committed_v2_captures_are_full_game_only_because_none_asked` states
that gap and is written to fail when they are recaptured.

### Two things to fix *before* the Pennsylvania recapture — 2026-08-08

Found by adversarial review of the change above. Both are quiet, and both bite
exactly when the next phase runs.

**1. `parse_actionnetwork` de-duplicates on `f"{path}:{event_id}"`, with no book
id in the key.** Envelopes are labelled `scoreboard:{book_id}:{path}` and
`latest_per_endpoint` returns them sorted by that label, so when a new PA capture
lands beside a surviving NJ one for the same source and league, the **lexically
smaller** endpoint label wins regardless of `fetched_at`. Demonstrated with a
synthetic PA Caesars envelope dated seven days after the committed NJ one: alone
it parsed 70 rows; together the parse emitted 70 rows all from
`scoreboard-123-mlb`, with `duplicate_event: 9`. String-comparing the ids, stale
NJ wins for `an_caesars` (123 < 1906), `an_parx` (1929 < 74) and `an_thescore`
(4620 < 4623) — two of which are the books this work exists to unblock. Put the
book id in the fixture key, or assert one book id per source's fixture set.

**Fixed 2026-08-08 by asserting one book id, not by keying per book.** Keying the
de-duplication per book only moves the collision downstream: both licences' rows
then carry the same `dedup_key`, and `drop_duplicate_selections` rejects whichever
half parses second to satisfy storage's UNIQUE constraint. Neither half is the
state's price, so there is nothing to choose between and no merge worth writing.
`_require_one_book` now raises `FormatChangeError` naming both ids, because one
pass writes exactly one `book_id` and two can only mean a stale out-of-state
capture. No committed fixture set carries two ids today, so it fires only on the
hazard.

Re-measured on the real bytes rather than the synthetic Caesars envelope: the
committed `an_bovada` capture as a week-old survivor (book 21) beside a relabelled
copy as today's (2495) parsed to **36 rows, every one of them from
`scoreboard-21-mlb`**, `duplicate_event: 5` — the newer capture's 12 rows gone in
silence. Pinned by
`test_a_second_licences_capture_is_refused_not_quietly_preferred`; deleting the
guard restores the silent merge and the test fails.

**2. `takeable_from_state`'s nationwide half is default-open.** It admits every
base descriptor that is not retail, not a republisher and not US-unavailable — so
a newly registered source that is none of those is reachable from *all four*
states the moment it is added, without appearing in any table. A first-party
`thescore` registered outside `RETAIL_SOURCE_KEYS` would be takeable in DC, where
theScore Bet is not listed at all; `coverage._check_direct_route` would then
accept it as PA's direct route and `coverage_for_state` would grade the book
`DIRECT` on `direct_rows > 0` alone. Register first-party books in
`RETAIL_SOURCE_KEYS` with real per-state routes, which fails loudly when a route
is missing, rather than letting them inherit nationwide reachability. The same
applies to ProphetX and Novig, which are *stakeable* — there the wrong answer is
a leg sized into an alerted position.

**Fixed 2026-08-08 by closing the world.** `NATIONWIDE_SOURCE_KEYS` is now an
enumerated set — `{kalshi}` — and `_check_reachability_is_declared` requires the
four sets to partition `_BASE_SOURCES`, so registering a source without deciding
how it is reachable fails at import with the four choices spelled out. It also
refuses a key that is nationwide *and* retail/view-only/US-unavailable (the union
would then contradict the per-state table) and a set naming a source that no
longer exists.

Takeable sets are unchanged by the fix — PA/IL/NJ/DC return exactly what they
returned before — because the old comprehension and the named set are provably
equal *while* the four sets partition the registry. That equivalence is the
reason a mutation reverting `takeable_from_state` to the comprehension still
passes: the invariant is what fixed this, not the expression. The direction is
pinned separately by
`test_the_nationwide_half_of_takeable_is_exactly_the_named_set`.

The advice above still stands for the sources ahead: theScore Bet belongs in
`RETAIL_SOURCE_KEYS` with per-state routes, and ProphetX and Novig must be
classified explicitly before they can be registered — that is now enforced rather
than remembered.

## Polymarket: two venues share the brand — 2026-08-08

`src/sources/polymarket.py` reads `gamma-api.polymarket.com`, the offshore
platform. The US venue is a different legal entity: QCX LLC, doing business as
Polymarket US, a CFTC-designated contract market Polymarket acquired in July
2025, with its own order book, fee schedule and settlement rules. The offshore
key is therefore in `US_UNAVAILABLE_SOURCE_KEYS` and its legs are labelled "not
reachable from {ST}" like any other unreachable venue. Polymarket US belongs
here as its own source key — `COMMISSIONS` and `SETTLEMENT` are keyed by source
key, so a config variant would read a fee schedule off the wrong venue's bytes.

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
[Polymarket, Kalshi, and Bovada](https://igb.illinois.gov/sports-wagering/cease-and-desist-letters.html);
their adapters remain research inputs but must not be presented as Illinois
betting counterparties.

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
| Circa | app-only surface; no web adapter | `an_circa` | `vsin_circa` | Circa's own site points to VSiN as an odds aggregator. The named VSiN column produced 48 MLB quotes / 8 events with no rejections. It is a Las Vegas line tracker, so use it as fallback and drift evidence rather than proof of Illinois-state price identity. AN is retained but currently omits Circa. |
| theScore Bet | verified IL GraphQL surface; adapter pending | none | none | Official `us-il` edge returned a valid anonymous startup, MLB competition, and lines payload. This is the strongest next first-party adapter candidate. |

The retained Action Network feeds were rechecked from Illinois on 2026-08-03.
The old v1 scoreboard returned games but omitted all six requested books. The
current Action Network web board revealed a v2 endpoint and grouped `markets`
schema. After adding dual-schema parsing, v2 restored real 48-quote MLB captures
for Hard Rock, Fanatics, and Bally. Fliff, Circa, and SuperBook still returned
zero requested-book rows across MLB, WNBA, NFL, NHL, and soccer. Those remaining
three cannot yet supply the real, non-empty captures required by the offline
source contract. Their independent fallbacks remain registered where available,
but the full offline suite remains blocked until Action Network restores those
tenants or a different genuine feed is integrated under an accurately named
source.

### Pennsylvania: no book reaches the two-feed bar

Recorded so this is not rediscovered. Under the rule in `docs/agent-guidelines.md`
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
they are `SINGLE_SOURCE` on paper and `MISSING` in practice.

**The three new VegasInsider columns.** `vi_betmgm`, `vi_betrivers` and
`vi_fanduel` are the opposite case — the adapters work and the captures are the
problem. Their 2026-08-07T01:55Z captures parse to zero rows, but so does
`vi_caesars` against that same overnight page, and feeding the live 2026-08-03
page to each of the three new parsers yields 48 quotes apiece. They need a
recapture on a live slate, not a code change; `test_the_adapter_produced_rows_at_all`
would currently fail on all six, masked only by the pre-existing
`an_fliff` / `an_circa` / `an_superbook` fixture gap that errors first.

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
| theScore Bet | **first party verified**: `env.js` exposes the public GraphQL host; the default edge redirects this Illinois egress to `sportsbook.us-il.thescore.bet`. Anonymous `Startup`, persisted `CompetitionPage`, and `CompetitionPageSectionLinesTabNode` calls all returned `200`. The MLB lines payload contained 47 event nodes, 55 markets, and 147 selections with structured American odds. Parser/capture work remains. |

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

### Front-ends of an order book already registered — 2026-08-08

The same trap outside Kambi. Each of these was named as a Pennsylvania venue to
acquire and each was rejected, because it resells an order book this repository
already collects. Registering one would not add a counterparty; it would add a
second key in front of an existing one.

| Venue | Mirrors | Mechanism |
|---|---|---|
| Robinhood Predictions | `kalshi` | Trades Kalshi's event contracts on Kalshi's order book, with its own per-contract fee added on top |
| Coinbase Predictions | `kalshi` | Kalshi-powered; the contracts are Kalshi's |
| PlaySugarHouse | `betrivers_kambi` | Same Rush Street parent, same Kambi platform. `rsiuspa` and `rsiusil` are already recorded above as byte-identical, 30 of 30 shared moneylines equal |

**`src/distinctness.py` cannot catch these, and that is why they are excluded by
name rather than by measurement.** It compares exact `decimal_odds` equality. A
front-end that adds a fee to the displayed price makes *every* price differ, so
the agreement rate is 0%, the verdict is `DISTINCT` — the strongest possible
"these are two different books" — and the engine publishes an arbitrage between
two windows onto one order book. The measurement is not merely absent here; it
points the wrong way, which is worse than nothing.

So the exclusion is pinned in `tests/test_distinctness.py`
(`TestFrontEndsOfARegisteredBook`) instead: these keys must stay out of
`registry.keys()`, and the class carries the markup case in executable form so
the reason cannot be mistaken for an oversight. If one is ever registered
deliberately,
the way to do it is the existing `tests/fixtures/mirrors/` precedent — captures
under *non-registered* keys, compared in a test — plus every combination
declared in `src/redundancy.py`.

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
