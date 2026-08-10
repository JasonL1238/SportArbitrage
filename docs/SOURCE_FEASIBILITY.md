# Which sources are reachable, which are not, and why

This is the record of what happened when every candidate source was actually
asked. It exists so that a wall is documented once rather than rediscovered every
few months by somebody writing an adapter for a host that has never answered.

Reproduce it with:

```bash
python scripts/detect_state.py
python scripts/probe_sources.py --state IL
```

## ProphetX and Novig: credentialed exchanges, adapters ahead of keys — 2026-08-09

Both venues' official documentation was read (not probed — neither has an
anonymous surface to probe) and both adapters were written against it:
`src/sources/prophetx.py` and `src/sources/novig.py`, **deliberately
unregistered** per the operator's decision until keys are supplied and a
genuine capture exists. The keys are reserved in
`registry.CREDENTIALED_SOURCE_KEYS`; credentials are environment-only
(`ODDS_PROPHETX_ACCESS_KEY` / `ODDS_PROPHETX_SECRET_KEY`,
`ODDS_NOVIG_CLIENT_ID` / `ODDS_NOVIG_CLIENT_SECRET`); construction succeeds
without them and `fetch_raw` refuses as `login_required` before any socket.
The login/token responses are never captured — the body is the secret — and
the bearer token travels in a request header, which envelopes do not persist.

**ProphetX** (docs.prophetx.co, read 2026-08-09):

- Sandbox `https://api.sandbox.prophetx.dev/partner`; production
  `https://cash.api.prophetx.co/partner` (from the switch-to-production page;
  credentials are per-environment and must not be reused across them).
- `POST /auth/login` `{access_key, secret_key}` → `data.access_token`
  (10-minute lifetime on the reference page, 20 on the integration guide) and
  `data.refresh_token` (3 days).
- `GET /mm/get_tournaments` → `GET /mm/get_sport_events?tournament_id=` →
  `GET /mm/get_multiple_markets?event_ids=` (form-style non-exploded).
  Selections carry decimal `odds`, `display_odds`, `competitor_id`,
  `outcome_id`, and a `stake` number whose semantics the reference does not
  state — available-to-match or already-matched volume, and in what currency,
  are open questions, so the adapter publishes no stake ceiling until the
  first credentialed session answers them.  Events carry
  `competitors[].side`, `scheduled` (ISO 8601) and `status`.
- **The reference marks `get_markets` and `get_multiple_markets` deprecated
  without naming a successor.** First credentialed session must re-check; a
  `/v4/` price-ladder endpoint already exists, so a versioned market surface
  may too.
- No self-serve keys: sandbox access is granted by the ProphetX team
  (api@prophetx.co). No fee schedule is published in the API docs — ask when
  keys are issued; the `COMMISSIONS` entry cannot be written from marketing
  copy.

**Novig** (docs.novig.com, read 2026-08-09):

- Production `https://api.novig.us`; QA `https://api-qa.novig.us`.
- `POST /nbx/v1/auth/emm-token` (`grant_type=client_credentials`, `client_id`,
  `client_secret`) → 30-minute bearer. The docs show the fields without naming
  the encoding; the adapter sends RFC 6749 form-encoding as a pre-encoded
  string with an explicit content type, so every transport client (curl_cffi,
  browser mode, injected test clients) puts the same bytes on the wire —
  confirm the venue accepts it on first session.
- `GET /nbx/v2/emm/events?league=&status=OPEN_PREGAME&limit=&offset=`
  (512 req/s), `GET /nbx/v2/emm/markets/open?league=&marketType=` and
  `GET /nbx/v2/emm/book/{marketId}?currency=CASH` (128 req/s each). League
  spellings match this repository's canonical keys.
- **Only bids rest on the book**, in price–time priority, as decimal
  probabilities to three places. The takeable price of outcome A is therefore
  derived: `1 − best_bid(B)`. The market payload's `last` is trade history,
  not a takeable price, and is never published as one.
- Spread handicaps are read from the two outcome **descriptions** and judged
  as a pair — they must be sign-opposed and agree in magnitude with the
  market's own `strike` — because the strike alone is one unsigned number
  serving two opposite handicaps. A signed token of ±100 or more is an
  American price, not a line: exchange rows display prices ("Phillies -110"),
  and reading one as a handicap publishes a -110 line that validation faults
  on MLB and silently accepts as junk in the high-total leagues.
- The venue's market-type enum is documented open (`MONEY / SPREAD / TOTAL /
  …`), so whether it reuses those strings for halves and quarters is
  **unknown**. Two defences, in order: the `_MARKET_TYPES` allowlist is the
  primary one (the enum's other documented members — `TEAM_TOTAL`,
  `PLAYER_GOALS` — are their own types, so a half plausibly is too), and a
  text screen is the secondary one. Both adapters screen the market's
  **label-bearing** fields (`_common.MARKET_LABEL_KEYS` — `name`, `label`,
  `description`, `group_name`, `sub_type`, …) *and* each outcome's own label
  against one shared vocabulary (`_common.PERIOD_MARKERS`, compact spellings
  in both orders). Not every string field: settlement prose is prose, and a
  `rules` note reading "void if the game is suspended before the end of the
  regulation **period**" tokenises into a marker and silently deletes a live
  full-game market — invisibly at fetch time, where a screened-out market has
  no counter at all. `OT` is excluded from the vocabulary since it marks
  whether extra time counts toward a whole-game price rather than naming a
  narrower window, and `ALL_PERIODS`-style phrases are read as whole-game.
  Novig screens at fetch as well as at parse, so a sub-period market does not
  spend one of the 60 per-league book requests before being discarded.
- **The residual is real and unresolved**: a sub-period market spelled with a
  game type and carrying no period word anywhere in its payload is
  indistinguishable from a full-game one, and would publish as full-game. The
  documented `markets/open` shape carries no period field at all, so nothing
  in the payload settles it — the first genuine capture must, and until then
  this is a stated gap rather than a closed one.
- The fees page settles two questions the API reference leaves open:
  `qty` is defined as **100 qty = 1 contract of $1.00 payout** (so the stake
  available at the derived price is `(qty/100) × (1 − bid)` dollars), and the
  straight-contract taker fee (`P × (1−P) × 0.03 × contracts`) is charged
  **only on fills matched while the event is `OPEN_INGAME`** — a pregame fill
  carries no fee, so the eventual `COMMISSIONS` entry must encode
  pregame-zero rather than the live coefficient. Makers pay nothing in both
  regimes.

Registration checklist when keys arrive, for either venue — deliberately
identical to the copy on `CREDENTIALED_SOURCE_KEYS` in
`src/sources/registry.py`, because a step missing from either copy is how an
operator registers per instructions and still fails the suite or ships a
wrong page:

1. descriptor in `_BASE_SOURCES`, classified in the four reachability sets;
2. `COMMISSIONS` and `SETTLEMENT` entries (Novig's commission must encode
   pregame-zero, per the fees measurement above);
3. a genuine capture committed under `tests/fixtures/raw/`;
4. distinctness against every registered source, plus `src/redundancy.py`
   for any intentional failover pair;
5. a `src/betlinks.py` entry — `tests/test_betlinks.py` asserts every
   registered non-consensus key resolves to a link;
6. a `src/report.SOURCE_NOTES` entry with `kind: exchange` — without one the
   sources page renders the venue as an unknown sportsbook;
7. a `src/report.SKIP_NOTES` entry for every skip reason the adapter emits
   that no existing note covers —
   `test_every_real_skip_reason_has_an_explanation` is parametrised off the
   registry and goes red on an unexplained reason. Prefer an existing
   spelling to a new one;
8. flip the stays-unregistered pin in `tests/test_prophetx_novig.py`;
9. the full acceptance bar in the plan (healthy collect, replay PASS,
   counted in `comparable_group_count`).

If a response body carries a partner or account identifier, there is no
sanctioned path to a committed fixture — that would be the finding.

## Polymarket US and Crypto.com Sports: both walls, measured — 2026-08-09

Anonymous probes from the current (California) egress — both venues are
CFTC-regulated national markets, so no state egress governs them the way it
governs a state-licensed book.

**Polymarket US (QCX LLC)** has **no anonymous market-data surface**:

    GET https://api.polymarket.us/v1/markets?limit=2
    → 401 text/plain  "Missing required API key headers"

That settles a contradiction in the third-party writeups (one guide lists
"public market endpoints" and in the same breath says "there is no sandbox,
demo, or unauthenticated access mode" — the wire agrees with the second
claim). Keys are Ed25519 pairs minted at `polymarket.us/developer` **after
KYC through the iOS app**, which only the operator can complete. So
`polymarket_us` is a credentialed venue in exactly the ProphetX/Novig sense
and follows the same doctrine: no adapter is registered — and none is written
yet, because the Ed25519 request-signing scheme is documented only behind
that developer portal, and an auth implementation guessed from blog posts
would be wrong in the way that matters. When the operator supplies a key
pair, the adapter joins `CREDENTIALED_SOURCE_KEYS` with its own source key,
commission schedule and settlement rules — never as a config variant of the
offshore `polymarket` key (see the 2026-08-08 section below).

**Crypto.com Sports (CDNA)** has **no public sports market-data surface at
all today**:

- The Exchange v1 public API answers anonymously but carries only crypto
  instruments — `public/get-instruments` on 2026-08-09: 333 perpetual swaps,
  582 currency pairs, 10 futures, zero event contracts (the one
  "sports-flavoured" symbol is `NFLXUSD-PERP`, the Netflix stock perp).
- The predictions REST/WebSocket API their developer page advertises is
  marked **"Coming soon"** (only FIX is listed available, an institutional
  order-flow protocol, not an anonymous data surface), and the documented
  example path answers 404 on both plausible hosts:
  `api.crypto.com/api/v1/predictions/events` → 404 `{"code":"10004"}`;
  `crypto.com/api/v1/predictions/events` → 404 HTML.
- Sports event contracts trade only in the retail app, behind an account.

The finding is the wall itself: nothing to adapt until the predictions API
ships, and it is worth re-probing when it does — their own page says
predictions data will cover "crypto, politics and economics", so whether
CDNA's *sports* contracts will be in it is itself unanswered.

## Action Network publishes its book catalogue — 2026-08-08

`https://api.actionnetwork.com/web/v1/books` returns **456 books**, each with an
`id`, a `display_name` and a `source_name`. Anonymous GET, no key, same headers as
the scoreboard. There is no `web/v2` equivalent — `v2/books`, `v1/sportsbooks` and
`v2/sportsbooks` are all `404 {"statusCode":404,"error":"Not Found"}`.

This settles by evidence a question three files had recorded as unanswerable. The
note in `src/coverage.py` said the payload carries no book catalogue "so the
mapping cannot be settled from the bytes we collect" — true of the *scoreboard*
payload, and false of the API. Every numeric id in this repository can now be
tied to a brand. A copy is kept at `data/research/actionnetwork_books_20260808.json`.

Pennsylvania's shelf, as Action Network names it:

| id | `display_name` | `source_name` | asked by |
|---|---|---|---|
| 74 | `Parx` | `paparx` | `an_parx` |
| 122 | `BetRivers PA` | `pabetrivers` | `an_betrivers` |
| 246 | `UnibetPA` | `paunibet` | `an_unibet` |
| 255 | `FanDuel PA` | `fanduelpa` | `an_fanduel` |
| 280 | `BetMGM PA` | `betmgmpa` | `an_betmgm` |
| 912 | `Betway PA` | `betwaypa` | *nobody — not named by the operator* |
| 1534 | `DK PA` | `draftkingspa` | `an_draftkings` |
| 1906 | `Caesars PA` | `caesarspa` | `an_caesars` |
| 2791 | `Fanatics PA` | `fanaticspa` | `an_fanatics` |
| 3547 | `bet365 PA` | `bet365pa` | `an_bet365` |
| 4623 | `theScore Bet PA` | `thescorebetpa` | `an_thescore` |

Ten of the eleven are asked for and every one is the book its source key claims.
Three things follow that were open before:

- **theScore Bet is not gone.** The catalogue lists twenty-one live per-state
  theScore Bet books, 4623 among them. An earlier note asserting a US withdrawal
  had no support and has been removed.
- **246 is Unibet Pennsylvania, and no book in the catalogue is named Mohegan** —
  nothing matches `mohegan`, `sun` or `pocono`. Mohegan Sun Pocono's PA skin did
  run as Unibet, so the mapping is plausible, but it is a licence-holder question
  and no amount of further scraping closes it.
- **`an_superbook` asks for id 14, which is `Westgate`.** There is no SuperBook in
  the catalogue under any spelling (`super` matches only Superbet RO/PL/BR, and
  `sbk` is 3088). The key claims a brand it is not configured for; if id 14 ever
  returned rows they would be Westgate's, published as SuperBook.

Two ids that appear when a request is refused rather than answered: **15 is
`Consensus` and 30 is `Open`** — not sportsbooks. That pair is what comes back
when v2 is asked for a book it does not carry, which is how the failure below
looks like success.

### Three registered ids carry no odds on either endpoint — 2026-08-08

`an_fliff` (2292 `Fliff`), `an_circa` (78 `Circa`) and `an_superbook` (14
`Westgate`) are all in the catalogue and all return **nothing**. Asked alone on
MLB from a PA egress, each on both versions:

| source | id | v1 books returned | v2 books returned |
|---|---|---|---|
| `an_fliff` | 2292 | 15, 30, 68, 69, 71, 75 | 15, 30 |
| `an_circa` | 78 | 15, 30 | 15, 30 |
| `an_superbook` | 14 | 15, 30 | 15, 30 |

Fifteen games on the board each time, and the asked-for id in none of them. In
the live run this is `empty_after_parse: parsed 0 quotes from 5 responses;
skipped={'odds_for_other_book': 934}` — the payload is full of prices, none of
them this book's.

**These three were the entire 1617-error baseline.** `conftest.py::_load` is
session-scoped and asserts a fixture exists for every registered source, so three
missing fixtures aborted the whole contract suite rather than three tests of it. No
fixture could be captured, because there was nothing to capture. They stayed
registered and failing per the agreed plan until the call was revisited.

**Deregistered 2026-08-09, with the operator's explicit approval.** The decision
was put to the operator during the Illinois campaign planning and approved: all
three descriptors removed, along with their `COMMISSIONS`/`SETTLEMENT` entries,
bet links (including SuperBook's Colorado door — no feed produces a row to link
from), `SOURCE_NOTES`, the `an_circa`/`vsin_circa` redundancy pair, and their
Pennsylvania view-only listings. Replay of stored runs that hold their raw
responses (180 on disk, all parsing to zero quotes) records "no adapter
available" as a note rather than a failure, because losing the ability to
re-parse bytes that produced no rows loses nothing. Circa's only remaining feed
is `vsin_circa`, the Las Vegas line tracker, declared `OTHER_LICENCE` context in
Illinois's required-book table.

**Re-registration condition, in order:** Action Network restores the tenant
(the id answers with its own rows on v2), **then** a genuine non-empty capture
is committed. Not the reverse — registering ahead of the capture is exactly
what bought the 1617-error baseline the first time.

**A pattern for the next deregistration:** removing a key from
`REPUBLISHED_SOURCE_KEYS` also lifts view-only classification from any rows it
left in run history — the dashboard would render them as an unknown,
bettable-looking sportsbook. Safe this time only because the store was checked
first and holds **zero quotes** for all three keys (180 raw responses, all
parsing to nothing). Deregistering a republisher that *did* store quotes needs
an answer for its historical rows before the key comes out.

### theScore Bet's Illinois id answers — 2026-08-10T01:05Z

The first-ever request of Action Network id 4601 (`theScore Bet IL`,
confirmed by name in the catalogue) returned **74 soccer rows across 11
fixtures, zero rejections**, from matching Illinois egress on run 35 (replay
PASS). The NFL board answered but carried no 4601 odds (no preseason lines
posted); MLB/WNBA/NHL were a dead Sunday-evening slate. So the watcher
`REQUIRED_BOOKS["IL"]` declares for theScore Bet is now **proven to answer**,
which `an_parx` and `an_unibet` never were for PA until their recapture.

The key's fixture store now holds this Illinois capture — and only it,
because the parser refuses two licences' captures in one store. The 4623
Pennsylvania capture (225 rows, 2026-08-08) was retired from the store; its
proof stands recorded in § "The Pennsylvania recapture" and the file returns
with the next PA-egress pass. The same swap discipline applies to every
`an_*` key the Illinois recapture touches: **replace the fixture set per key,
never append across licences.**

### Deliberate parser changes and the replay verdict — 2026-08-09

`replay of run N: FAIL` with "row count differs / replay lost row" is what a
**deliberate parser change** looks like on a run stored before the change, and
the mid-move guard is one: stored runs **14** (IL, 2026-08-03, vi_hardrock
42→36), **19** (GLOBAL) and **20** (PA, both 2026-08-06, 56→50 each) now
re-parse to fewer rows *by design* — the dropped legs are the mid-move
pairings the guard exists to delete, including the 0.5167 moneyline pairing
the coverage prose cites. Their bytes verify against their recorded sha256s;
nothing is corrupted. `replay_run` now says this itself: a non-migrated FAIL
whose bytes verify opens with "the current parser disagreeing with the parser
that stored this run" and points here. Do not re-diagnose these three runs.

### One stale board retired for vintage coherence — 2026-08-09

Un-darkening the contract suite required live 2026-08-10T00:50Z recaptures of
`vi_betmgm`/`vi_fanduel`/`vi_betrivers` (their 2026-08-07 captures were the
dead-slate pages that parse to zero). On a Sunday-evening board the only
pregame content was NFL preseason, so those fixtures' rows are NFL — and the
offline fixture pool now spans capture vintages on shared NFL events.
`an_parx`'s 2026-08-08 PA NFL board carried LAC@HOU with the away side
favoured; by 2026-08-10 the line had genuinely crossed to the home side, and
the cross-book identity test correctly refused the pool. The stale
`scoreboard-74-nfl` file was retired (its MLB board — **255 rows**; the
retired NFL board parsed to 96, and an earlier revision of this note wrongly
attributed their 351-row total to the surviving board — remains `an_parx`'s
contract evidence); it cannot be recaptured from Illinois because 74 is a
Pennsylvania-scoped request, and the next PA-egress recapture replaces it
with a fresh board. Retiring a superseded genuine capture is fixture
curation, not synthesis — nothing was edited, and the removal is recorded
here.

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

## Pennsylvania first-party routes promoted — 2026-08-08

`configured=PA detected_egress=PA` throughout (each run's collect printed the
match; the egress *fingerprint* is not persisted per run, and the stored
`egress_state.json` was re-detected — with a rotated IP, hence a new
fingerprint — 3 ms before run 32, so no per-run fingerprint claim is made
here). Three routes moved TEMPLATE → VALIDATED on the bar `MULTI_STATE.md`
sets — parser-clean quotes through matching egress, twice each:

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
| Circa | app-only surface; no web adapter | `an_circa` | `vsin_circa` | Circa's own site points to VSiN as an odds aggregator. The named VSiN column produced 48 MLB quotes / 8 events with no rejections. It is a Las Vegas line tracker, so use it as fallback and drift evidence rather than proof of Illinois-state price identity. *(2026-08-09: `an_circa` has since been deregistered — see § "Three registered ids carry no odds"; `vsin_circa` is now Circa's only feed.)* |
| theScore Bet | verified IL GraphQL surface; adapter pending | none | none | Official `us-il` edge returned a valid anonymous startup, MLB competition, and lines payload. This is the strongest next first-party adapter candidate. |

The retained Action Network feeds were rechecked from Illinois on 2026-08-03.
The old v1 scoreboard returned games but omitted all six requested books. The
current Action Network web board revealed a v2 endpoint and grouped `markets`
schema. After adding dual-schema parsing, v2 restored real 48-quote MLB captures
for Hard Rock, Fanatics, and Bally. Fliff, Circa, and SuperBook still returned
zero requested-book rows across MLB, WNBA, NFL, NHL, and soccer. Those remaining
three cannot supply the real, non-empty captures required by the offline
source contract. *(This paragraph's ending is superseded: on 2026-08-09 the
operator approved deregistering all three rather than waiting — see § "Three
registered ids carry no odds" — and the full suite is green. `vsin_circa`
remains registered as Circa's fallback.)*

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
*(Resolved 2026-08-10: the dead-slate captures were replaced with live
IL-egress ones parsing 18/54/54 rows, and the fixture gap is gone with the
deregistration — see § "One stale board retired for vintage coherence".)*

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

**And betPARX prices like a Kambi tenant too — 2026-08-08.** On run 27's real
rows, `compare_sources(an_betrivers, an_parx)` returns **MIRROR** ("one feed in
NFL, 32/32 shared prices equal"). Harmless today, because both feeds are
view-only and `screen_candidate` skips view-only candidates by design. It stops
being harmless the day betPARX or theScore gets a **first-party** Kambi route:
that source would be a distinctness candidate against `betrivers_kambi`, and if
the PA books genuinely share a Kambi price feed, the pair must land MIRROR or
UNDECIDED — a DISTINCT verdict on a thin slate would arm the false-arb trap this
section documents. Run the gate on a full slate before promoting either, and
treat "failed to prove a mirror" as exactly that.

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
