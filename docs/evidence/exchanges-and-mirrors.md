# Exchanges, prediction markets, and the mirrors that look like progress

Credentialed exchanges, prediction markets, and the venues that turned out to
be front-ends of an order book already registered.

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
6. a `SOURCE_NOTES` entry in `src/report_copy.py` with `kind: exchange` — without
   one the sources page renders the venue as an unknown sportsbook;
7. a `SKIP_NOTES` entry there for every skip reason the adapter emits
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

## Polymarket: two venues share the brand — 2026-08-08

`src/sources/polymarket.py` reads `gamma-api.polymarket.com`, the offshore
platform. The US venue is a different legal entity: QCX LLC, doing business as
Polymarket US, a CFTC-designated contract market Polymarket acquired in July
2025, with its own order book, fee schedule and settlement rules. The offshore
key is therefore in `US_UNAVAILABLE_SOURCE_KEYS` and its legs are labelled "not
reachable from {ST}" like any other unreachable venue. Polymarket US belongs
here as its own source key — `COMMISSIONS` and `SETTLEMENT` are keyed by source
key, so a config variant would read a fee schedule off the wrong venue's bytes.

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
