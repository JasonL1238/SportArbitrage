# Action Network: the republished layer

Every measurement of the Action Network republisher layer: which book ids
answer, how the two endpoint versions differ, and which markets each returns.

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
*(2026-08-10 update: the position-forming surfaces — the `arb` command, the
page's arb payload, and the source badge — now fold any stored key the
registry no longer knows into the run's view-only set, so a ghost key can no
longer leg a position or a text.  Promo planning does NOT carry that guard —
it prices only the jurisdiction's latest run, where ghost keys cannot occur
today — so the residual rule of this pattern is: never deregister a key that
holds rows in a jurisdiction's CURRENT latest run; collect a fresh run
without it first, then deregister.)*

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
proof stands recorded in `state-routing.md` § "The Pennsylvania recapture" and
the file returns
with the next PA-egress pass. The same swap discipline applies to every
`an_*` key the Illinois recapture touches: **replace the fixture set per key,
never append across licences.**

### Deliberate parser changes and the replay verdict — 2026-08-09, population corrected 2026-08-10

`replay of run N: FAIL` with comparison-shaped problems under the
verified-bytes preamble is what a **deliberate parser change** looks like on
a run stored before the change. An earlier revision of this section drew the
boundary around three runs; the full population, measured offline 2026-08-10
by replaying **all 35 stored runs**, is twenty:

- **PASS**: 13, 23, 27, 29, 30, 31, 32, 33, 34, 35.
- **Nothing to replay**: 5, 6, 11, 12, 26.
- **FAIL — the mid-move guard plus older fingerprints**: **14** (IL,
  2026-08-03, vi_hardrock 42→36), **19** (GLOBAL) and **20** (PA, both
  2026-08-06, 56→50 each) re-parse to fewer rows *by design* — the dropped
  legs are the mid-move pairings the guard exists to delete, including the
  0.5167 moneyline pairing the coverage prose cites.
- **FAIL — the older fingerprints alone**: **15, 16, 17, 18, 21, 22, 24,
  25** carry the same two earlier deliberate changes without the vi_hardrock
  drop: five cloudbet `first_5_innings` spread lost/invented **pairs** per
  run (the away leg's line respelled `'0'` → `'-0'` by the zero-line sign
  canonicalization) and DraftKings `decimal_odds`/`implied_probability`
  precision differences from the trueOdds change. **28** (PA, 2026-08-08
  17:06Z) is DraftKings-only: the trueOdds change went live between run 28
  and run 29 (17:11Z, commit 5832927), so 28 is the last run stored under
  the old readings.
- **FAIL — pre-campaign legacy, comparison-shaped**: **1, 7, 8, 9, 10**
  (the 2026-07-30 era) fail under the verified-bytes preamble with larger
  diffs from the many parser evolutions since — run 1 replays stored
  2,680 → replayed 3,307 (+627 net: lost `an_bet365` first-5-innings
  moneylines, invented cloudbet full-game moneylines, and more; the
  listing discloses its own truncation with exact counts). No section
  enumerates every legacy delta and none is planned: the preamble and
  this paragraph are the diagnosis.
- **FAIL — pre-campaign legacy, NOT comparison-shaped**: **2, 3, 4** open
  with `onexbet: replay raised FormatChangeError: … unknown sport endpoint
  'linefeed-american-football'` and `unibet_au: no adapter available to
  replay this source`, then the row-count diff.  They carry **no preamble,
  by design**: onexbet's bytes read and sha-verify but the current parser
  refuses that era's payload shape, and `unibet_au` stored rows and was
  later deregistered, so its rows are genuinely irreproducible — and its
  raw files are skipped unread, so nothing vouches for them.  These three
  are recorded here as permanently irreproducible legacy runs; that is
  the diagnosis, not corruption.

Seventeen of the twenty (all but 2/3/4) open with the verified-bytes
preamble placing the differences "between the stored rows and the current
parser's reading of those verified bytes"; `replay_run` says it itself and
points here. Do not re-diagnose any run on this list while its output
matches what this section records. A FAIL **outside** this list, an
unexpected "stored bytes unreadable" problem, or a sha256 mismatch is a
real alarm.

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
`RETAIL_SOURCE_KEYS` with per-state routes, and any stakeable exchange must be
classified explicitly before it can be registered — that is now enforced rather
than remembered. (The ProphetX and Novig adapters named above were removed
unregistered on 2026-08-11; see `exchanges-and-mirrors.md`. The
enforcement is what outlives them.)
