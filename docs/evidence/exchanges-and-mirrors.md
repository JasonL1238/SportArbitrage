# Exchanges, prediction markets, and the mirrors that look like progress

Credentialed exchanges, prediction markets, and the venues that turned out to
be front-ends of an order book already registered.

## The offshore Polymarket is deregistered; the brand now names one venue — 2026-08-13

Removed at the operator's request once `polymarket_us` was collecting: the
adapter, its four committed captures, its `COMMISSIONS` / `SETTLEMENT` /
`betlinks` / `SOURCE_NOTES` entries, and its place in
`US_UNAVAILABLE_SOURCE_KEYS`. Restore with
`git log -- src/sources/polymarket.py`.

**The reason is the operator's, and it is a scope decision rather than a
measurement:** the offshore book is not executable from the United States, so
its prices were context only — hidden behind the dashboard's offshore toggle,
labelled "not reachable from {ST}" on every position, and never a leg anyone
could take. Against that, carrying it cost a second venue under a name already
proven confusable: the 2026-08-09 probe that read this brand as KYC-gated had
in fact asked the wrong *host* of the *other* venue, and that cost three days.

**What was argued against removing it, recorded because the decision may want
revisiting:** it was the only independent watcher of `polymarket_us`, and the
distinctness screen run the same day needs both venues live to be repeated. It
also sat in a class of **eleven** US-unstakeable sources — `pinnacle`,
`bovada`, `cloudbet`, `matchbook`, `smarkets`, `sxbet`, `onexbet`,
`leovegas_kambi`, `an_bovada`, and `an_onexbet` (the last deregistered
2026-08-15 as a dropped tenant; see `action-network.md`) — so removing this one
alone is a judgement about *this brand*, not a policy about unstakeable
reference books.
Pinnacle in particular is the sharpest price on the board and is equally
untakeable from Illinois. If the rule ever becomes "US-stakeable only", it
should be applied to that whole set deliberately, and the cost of losing
Pinnacle's number priced in first.

Note also that this venue did **not** meet the existing bar for deregistration.
The `an_circa` / `an_fliff` / `an_superbook` removals of 2026-08-09 were for
feeds returning **zero rows**; this one returned 326 in its final screen. The
bar it meets is the operator's, not the record's.

Two tests were **retargeted rather than deleted**, because the behaviours they
pin are also `polymarket_us`'s and dropping them would have quietly lost
coverage: `TestPolymarketReadsTheLeagueFromThePayload` (the season-suffix strip
on `seriesSlug`, which once rejected 124 real events) and
`TestPolymarketAsksWhetherItTruncatedRatherThanAssuming` (the one-row probe past
the page cap). Both now exercise the US adapter against its own captures.

## Polymarket US registered: four traps, and the two that would have priced a favourite at 20:1 — 2026-08-13

`polymarket_us` is registered against `gateway.polymarket.us`, in
`NATIONWIDE_SOURCE_KEYS` beside `kalshi` — a CFTC-designated venue holding no
state sportsbook licence — while the offshore `polymarket` stays in
`US_UNAVAILABLE_SOURCE_KEYS`. One brand, two legal entities, opposite answers.

**Four things in this payload produce a plausible wrong number rather than an
error.** All four were measured on a live board, and each has a guard.

1. **`outcomes` is not index-aligned with `marketSides`.** Over 544 NFL markets,
   `outcomes[0]` disagreed with `marketSides[0].description` on **198, or 36%**.
   On KC @ LAD, `outcomes` reads `["Los Angeles Dodgers","Kansas City Royals"]`
   while `marketSides[0]` is Kansas City — so indexing prices by `outcomes` puts
   the Dodgers at 0.05, a 20:1 line on a heavy favourite, on a third of all rows.
   The adapter reads `marketSides` and never `outcomes` or `outcomePrices`.
2. **The takeable price is `marketSides[i].quote.value`.** Verified 544/544:
   side 0's quote equals `bestAskQuote`, side 1's equals `1 − bestBidQuote`. The
   venue performs the `1 − bid` transform the offshore adapter does by hand and
   publishes it per side.
3. **`team_` versus `game_` in `sportsMarketType` does not mean what it says.**
   `football_team_full_game_total` is a **game** total ("Will the total in GB vs
   PIT be more than 17.5"); `football_team_first_half_total` is a **team** total
   ("Will GB score more than 9.5 in the first half"). One token apart, opposite
   scopes, and the vendor's own schema page confirms it. What decides it is
   `metadata`, present with a `teamId` both sides repeat exactly on the
   team-scoped markets. Every mapped type declares a scope and the payload is
   **asserted** against it — a disagreement is `market_scope_disagrees_with_its_type`,
   never a reinterpretation.
4. **Home and away come from `league.ordering`, never from the team blob.** On
   market `390522` both sides carry `teamId: 74` while their embedded team
   objects say `ordering: "away"` and `"home"` — the same team, two answers,
   because that field is stamped by side index. `league.ordering` is `"away"`
   (mlb, nfl, nba, nhl, wnba) or `"home"` (epl, mls, ucl); each route declares
   what it expects and a payload that disagrees is rejected.

Two smaller shape facts: a spread's line is on its own side
(`description: "+20.50"`, already signed for that side's team) and the
market-level `line` is *not* per-side — its sign is inconsistent across families
(`2.5` on one full-game spread, `-1.5` on a first-five) — so it is read only for
totals. And soccer is the mirror image of offshore: each contract carries a
`teamId`, or `None` for the draw, so the three-way **is** attributable and is
collected, while this venue prices no soccer spread or total at all. The three
contracts share one synthesized `source_market_id` so the completeness check sees
one three-way market; measured, they price 0.43 / 0.27 / 0.31, summing to 1.01.

**Charge and settlement are both different from the offshore venue, and copying
either across would have been wrong.** `docs.polymarket.us/fees` states
`Fee = Θ × C × p × (1 − p)` with `Θ = 0.06` — Kalshi's shape at a lower rate, not
Polymarket's `min(p, 1−p)` — and every collected market carries
`feeCoefficient: 0.06` inline, so the page and the payload agree. Settlement is
`SETTLE_MAKE_UP_GAME`: the vendor settles a postponed game from the make-up
contest within the contract's expiry and a cancelled one "at last fair market
prices as of the time the cancellation was officially announced" — a
venue-determined price, not the offshore 0.50 rule and not a refund.

**Distinctness screen — run, and clear.** `screen_candidate` has no production
caller, so it was run by hand against a **same-session** capture of both venues;
screening against the committed 2026-07-28 offshore fixture would have compared
disjoint game dates and returned `UNDECIDED`, which is not an answer. Result:
**96 shared moneyline selections** (the bar is 20), **12/96 identical (12.5%) —
DISTINCT**, non-blocking.

That verdict is read as *weak* evidence, per the caveat further down this file: a
front-end offset by a constant also reads `DISTINCT`. The examples rule that out
independently — the gaps are wildly uneven (`2.9412` vs `1.6393` on one
selection, `1.5038` vs `1.4925` on another), and on the same fixture the offshore
book quotes a **128% overround** against the US venue's **100.5%**. Two real
order books of very different depth, not one feed twice.

**The committed fixture omits NFL, deliberately.** With NFL included, the
cross-book identity check refused the pool: `NFL-GB@NFL-PIT` and `NFL-IND@NFL-NE`
had `vi_fanduel` and `vi_betrivers` (captured 2026-08-03) favouring the home side
while this game-day capture favours the away side. Not an adapter fault — all
three sources agree on *who* is home, and GB is `teamId 59` priced 0.59 against
PIT's 0.42 — but ten days of **preseason** line movement, which is the same shape
as the `an_parx` retirement recorded in `action-network.md`. Narrowing the new
capture was chosen over retiring a working fixture.

The cost is stated rather than hidden: `TEAM_TOTAL` and `FIRST_HALF` rows were
NFL-only, so **the committed fixture exercises neither**, even though the live
adapter emits both (measured: 312 full-game team totals, 192 first-half team
totals, 200 first-half spread/total rows in one pass). Re-capture NFL against a
fresh `vi_*` vintage to close that gap.

**Still inferred, never observed:** the three `hockey_team_full_game_*` types.
NHL was empty at every capture. They are safe to declare because both guards
stand behind them — a type that does not exist never matches, and a wrong scope
is rejected — and a 60-minute three-way would arrive with three sides and be
skipped rather than published as a two-way. Re-probe before the season.

**Deferred, with the reason in the module docstring:** UFC (`src.vocab` has no
MMA sport), NCAA basketball (no cbb/wcbb league), quarter and second-half markets
(no such `Period`). These are vocabulary changes, not adapter work.

## Polymarket US is not credential-gated — the 2026-08-09 probe asked the wrong host — 2026-08-12

**Measured from the operator's Illinois egress** (`data/egress_state.json` state
`IL`, fingerprint `34e56847c7ec…`), Chrome-impersonated `curl_cffi`, three
requests paced ~1.5 s apart:

| host + path | status | bytes | what came back |
| --- | --- | --- | --- |
| `gateway.polymarket.us/v2/leagues` | **200** | 11,343 | **50** leagues, every one `isOperational` — `mlb`, `nba`, `nfl`, `nhl`, `wnba`, `epl`, `ucl`, `ufc`, `f1`, plus esports (`cs2`, `lol`, `dota2`, `valorant`) |
| `gateway.polymarket.us/v2/leagues/mlb/events?limit=3` | **200** | 162,727 | events carrying **15 markets each**, with `sportradarGameId`, `gameId`, `teams`, `participants`, `startTime`, `score`, `period`, `live` |
| `gateway.polymarket.us/v1/markets?limit=3&active=true&closed=false` | **200** | 12,727 | markets with `outcomes`, `outcomePrices`, `marketSides`, `feeCoefficient` |
| `api.polymarket.us/v1/markets?limit=3` | 401 | 33 | `Missing required API key headers` — **unchanged**, in the same session |

**The finding is the hostname.** The 2026-08-09 entry below recorded this venue
as reachable only after iOS-app KYC, on the strength of that 401. But
`api.polymarket.us` is the vendor's *authenticated* host; `gateway.polymarket.us`
is the public keyless one, and it answers anonymously. Both were asked in the
same session from the same egress, so the difference is the host and nothing
else. The venue was never credential-gated for **reading**; it was asked the
wrong question for three days. Placing a wager there is a separate matter and
still needs the KYC'd account.

Two things a future adapter must not assume, both measured here rather than
guessed:

* **`/v1/markets` is futures, not game lines.** Its default page returned 196
  `sportsMarketType: futures` and 4 `election` out of 200 — "World Series
  Champion", "National League Champion" — and **no** pregame moneylines.
  Narrowing it with `sportsMarketTypes=MONEYLINE` is refused with **HTTP 400**.
  The game-level board is under `/v2/leagues/{slug}/events`, which is where an
  adapter reads. An adapter written against `/v1/markets` would collect a
  plausible, parseable, entirely wrong shelf.
* **~~The price semantics are unresolved and must not be inferred.~~ Settled
  2026-08-13 — see the section above.** This paragraph read: on
  `baseball_team_first_five_total` for BAL vs MIN, `outcomes` was
  `["Over","Under"]` and `outcomePrices` `["0.9300","0.9400"]`, which does not
  read as two complementary contract probabilities (they sum to 1.87, not ~1.00),
  so whether they were two Yes legs, a bid/ask pair or something else was left
  open. They are a **bid/ask pair of one token** — `outcomePrices` is
  `[bestBid, bestAsk]` — so the two numbers were never meant to sum to 1.00 and
  the puzzle was of this file's own making. The instruction to settle it before
  writing a parser was still right, and settling it is what found the larger
  trap: `outcomes` is not index-aligned with `marketSides` at all, so the whole
  array is unreadable and the adapter ignores it.

Still open, and not answered by any of the above: whether Illinois permits
**staking** these contracts. The IGB issued Polymarket a cease-and-desist dated
2026-01-27, the operator confirmed on 2026-08-12 that sports contracts are
visible in the app from Illinois, and `[[docs/MULTI_STATE.md]]`'s takeability
question is decided per state, not per reachability. Reachable is not the same
as stakeable, and `src/coverage.py`'s locality marking is where that distinction
gets rendered.

Distinctness is also unscreened: the repository already registers an offshore
`polymarket` (`gamma-api.polymarket.com`). Same brand, same parent, and
`src/distinctness.py`'s `screen_candidate` has **no production caller** — so
nothing catches a mirror automatically. Registering `polymarket_us` without
screening it against `polymarket` ~~, and without declaring the pair in
`src/redundancy.py`,~~ is how one venue's two feeds become the two opposite legs
of a "guaranteed" position.

**Correction, 2026-08-13: the `src/redundancy.py` half of that sentence was
wrong, and acting on it would have removed the check it was asking for.**
`REDUNDANT_PAIRS` is for a first-party book and its *republisher* —
`tests/test_redundancy.py` asserts every secondary begins `an_`, `vi_` or
`vsin_`, so the pair fails outright in either order. Worse, `is_redundant_pair`
is consulted at `src/collector.py:1155` to **suppress**
`sources_are_one_counterparty`, so declaring the pair would have silenced the
runtime mirror gate on exactly the two venues it was meant to guard. The screen
was the right half of the instruction and it was run; see the section above.

## ProphetX and Novig: credentialed exchanges, API read — 2026-08-09 (adapters removed 2026-08-11)

Both venues' official documentation was read (not probed — neither has an
anonymous surface to probe) and both adapters were written against it, then
**removed on 2026-08-11** on the operator's decision: keys had not arrived, and
2,106 lines reachable from no production path were carrying maintenance cost
against a session that had not been scheduled. `git log -- src/sources/novig.py
src/sources/prophetx.py` restores them; the last version to hold both is
`9ba9c9a`. Nothing else about the venues changed, which is why the reading below
stays: it is what the documentation said, and re-reading it is the expensive
part, not retyping the adapter.

Whoever writes the next version starts from the notes below plus
`docs/INPUT_CONTRACT.md`, and should keep the properties the removed adapters
had established, none of which are obvious from the venue's own docs:
credentials environment-only via `src.settings` (`ODDS_PROPHETX_ACCESS_KEY` /
`ODDS_PROPHETX_SECRET_KEY`, `ODDS_NOVIG_CLIENT_ID` /
`ODDS_NOVIG_CLIENT_SECRET`), never in a committed `SourceDescriptor.config` and
never as a query parameter; construction succeeding without them so an
unconfigured venue is importable, with `fetch_raw` refusing as `login_required`
before any socket; and login/token responses never captured — the body is the
secret — with the bearer token travelling in a request header, which envelopes
do not persist.

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
  text screen is the secondary one. Both adapters screened the market's
  **label-bearing** fields (a curated `MARKET_LABEL_KEYS` set — `name`, `label`,
  `description`, `group_name`, `sub_type`, …) *and* each outcome's own label
  against one shared vocabulary (`_common.PERIOD_MARKERS`, compact spellings
  in both orders). These two adapters were that key set's only
  callers; it outlived them by a week and was removed on 2026-08-18. What it
  encoded is kept in
  `docs/evidence/adapter-lessons.md` § "Three shared label-parsing guards
  outlived their only callers by a week". Not every string field: settlement prose is prose, and a
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

### Registration checklist

This list used to live twice — here and on `registry.CREDENTIALED_SOURCE_KEYS` —
with a note on each copy that a step missing from either was a defect. The
constant went with the adapters, so this is now the only copy, and
`AGENTS.md` routes new-venue work here. It is written for a **credentialed
exchange**, which is the hardest case; a public sportsbook skips the
credential-shaped steps and needs everything else. The suite enforces most of
it and the report renders the rest, so a step skipped shows up as a red test or
a wrong page rather than as nothing:

1. descriptor in `_BASE_SOURCES`, classified in the four reachability sets —
   `_check_reachability_is_declared` refuses anything less, because a source
   left unclassified used to inherit nationwide reach by omission;
2. `COMMISSIONS` and `SETTLEMENT` entries — `_check_registry` refuses their
   absence (Novig's commission must encode pregame-zero, per the fees
   measurement above);
3. a genuine capture committed under `tests/fixtures/raw/` — `conftest` demands
   one per registered key;
4. distinctness against every registered source, plus `src/redundancy.py`
   for any intentional failover pair;
5. a `src/betlinks.py` entry — `tests/test_betlinks.py` asserts every
   registered non-consensus key resolves to a link;
6. a `SOURCE_NOTES` entry in `src/report_copy.py` with the right `kind` — for an
   exchange, `kind: exchange`; without one the sources page renders the venue as
   an unknown sportsbook, which for an exchange is affirmatively wrong;
7. a `SKIP_NOTES` entry there for every skip reason the adapter emits
   that no existing note covers —
   `test_every_real_skip_reason_has_an_explanation` is parametrised off the
   registry and goes red on an unexplained reason. Prefer an existing
   spelling to a new one: both removed adapters were rewritten onto the
   established vocabulary rather than keeping the names they were born with;
8. the four per-venue lists in `tests/test_adversarial_findings.py` that
   enumerate every registered key by hand — they are test-local constants with
   no production symbol to search for, so `grep '"vsin_circa"'` is how to find
   them;
9. the full acceptance bar (healthy collect, replay PASS, counted in
   `comparable_group_count`).

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

## Polymarket: two venues share the brand — 2026-08-08

The offshore adapter (deleted 2026-08-13, restore with
`git log -- src/sources/polymarket.py`) read `gamma-api.polymarket.com`, the
offshore platform. The US venue is a different legal entity: QCX LLC, doing
business as Polymarket US, a CFTC-designated contract market Polymarket acquired
in July 2025, with its own order book, fee schedule and settlement rules. The
offshore key was therefore in `US_UNAVAILABLE_SOURCE_KEYS` and its legs were
labelled "not reachable from {ST}" like any other unreachable venue. Polymarket
US belongs here as its own source key — `COMMISSIONS` and `SETTLEMENT` are keyed
by source key, so a config variant would read a fee schedule off the wrong
venue's bytes.

**This is no longer a live distinction: the offshore venue was deregistered on
2026-08-13** and the brand now names exactly one source in this repository. See
the deregistration section at the top of this file.

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
| `parxuspa` (betPARX PA), `parxusnj` (NJ) | **distinct on US sports, one feed on Kambi-managed competitions** | run 33, 2026-08-23: 61.1% identical overall; MLB/NFL/WNBA priced independently, MLS/ITF/Bundesliga/La Liga/ATP byte-for-byte BetRivers' |

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

**betPARX got that first-party Kambi route on 2026-08-23, and the gate was run
on a full slate before it was promoted.** Its own web app names the tenant —
`pa.betparx.com/kambi` puts `offering: "parxuspa"`, `market: "US-PA"` in
`window._kc` (and `nj.betparx.com` says `parxusnj`), which is why none of the
guessed tokens above ever answered. Registered as `betparx_kambi`. Run 33 (PA,
2026-08-23, Philadelphia egress) measured it against `betrivers_kambi` at
**290/475 shared moneylines identical (61.1%) — MIRROR, one feed in MLS 51/51,
ITF 38/38, Bundesliga 27/27, La Liga 27/27, ATP 26/26**, and its own prices on
MLB, NFL and WNBA (a direct `listView/baseball/mlb` comparison the same evening
had 22 of 30 shared MLB moneylines differing: Tigers 1900 vs 1910, Pirates 3400
vs 3500). That is the LeoVegas shape exactly — run 32 reads `betrivers_kambi vs
leovegas_kambi` at 25.6%, one feed in ITF and ATP — and the gate is built for
it: `src.arb.counterparty_groups` folds the pair into one counterparty **per
competition** where they are one feed and leaves them two where they are not,
so a betPARX/BetRivers MLB position is real and an MLS one is refused. The
`an_parx`/`an_betrivers` NFL MIRROR above was the managed-feed half of the
same picture seen through one thin republished slate. Declared failover pair:
`("betparx_kambi", "an_parx")`.

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
