# Architecture

## System shape

SportArbitrage is a local Python application with two collection domains, a
hand-entered bet ledger, and a primarily read-only presentation layer:

- The odds pipeline collects venue data, normalizes quotes, reconciles event identity, validates the slate, detects arbitrage, and stores results.
- The promotions pipeline collects and normalizes offers into a separate database, then builds plans against current odds at report time.
- The bet ledger stores placed positions and settlement progress in its own SQLite database, separate from data that can be re-scraped.
- The report layer reads stored odds, promotions, and bets and produces a self-contained dashboard; its local server optionally invokes the collectors and is the only dashboard mode that may change the bet ledger.
- Operational scripts are thin manual entry points over reusable code in `src/`; application modules never import from `scripts/`.

## Odds data flow

```text
source registry -> adapter fetch -> raw envelope on disk -> pure adapter parse
    -> event reconciliation -> validation/distinctness -> arbitrage analysis
    -> SQLite persistence -> report/dashboard
```

`src/collector.py` orchestrates this flow. Raw responses are persisted by `src/raw_store.py` before interpretation. Adapters return normalized `src/schema.py` models. `src/events.py` reconciles identities across the complete slate before `src/validation.py` and `src/arb.py` evaluate it. `src/store.py` owns the odds SQLite schema, migrations, and queries. `src/state_selection.py` detects the current state and unions additional requested states; `src/jurisdictions.py` resolves exact routes. One batch fetches globals once, stores a `GLOBAL` run, and stores one isolated state run per selection.

## Promotions data flow

```text
promo registry -> promo fetch -> shared raw envelope -> promo parse/enrichment
    -> deduplication/strategy -> separate promo SQLite database
    -> report-time planning against stored odds
```

`src/promos/collector.py` orchestrates collection. `src/promos/store.py` deliberately owns a separate database and lifecycle. `src/promos/planner.py` is a pure computation over offers and quotes; plans are not persisted because prices move.

## Bet ledger data flow

```text
scraped arb/price or manual form -> validated position snapshot
    -> separate bet SQLite database -> dashboard totals and editable history
```

`src/betlog.py` owns this database and its arithmetic. Money is stored as integer
cents, and logged event/market/price fields are snapshots rather than foreign
keys into collected odds, so pruning or re-scraping odds cannot rewrite history.
A static dashboard embeds the current ledger read-only; the localhost report
server exposes the same-origin endpoints used to log, edit, settle, and delete.

## Major components

| Component | Responsibility |
| --- | --- |
| `src/schema.py`, `src/vocab.py` | Closed normalized quote model, sports, markets, periods, and settlement facts |
| `src/leagues.py`, `src/rosters.py`, `src/participants.py` | Competition metadata and participant identity |
| `src/sources/` | Venue-specific fetch and parse adapters plus shared transport guards |
| `src/sources/research.py` | Sanitized, ignored first-party browser/XHR/WebSocket discovery artifacts; never runtime odds |
| `src/raw_store.py` | Versioned raw-response envelopes and offline replay inputs |
| `src/events.py` | Cross-source event reconciliation |
| `src/validation.py`, `src/distinctness.py`, `src/redundancy.py` | Slate correctness and counterparty independence |
| `src/commission.py`, `src/settlement.py`, `src/arb.py` | Net pricing, settlement compatibility, and opportunity detection |
| `src/store.py` | Odds persistence and migrations |
| `src/jurisdictions.py`, `src/state_selection.py` | Typed IL/PA/NJ/DC routing and detected batch selection |
| `src/egress.py`, `src/probe_cache.py` | Privacy-reduced explicit detection and separate live-probe cache |
| `src/promos/` | Promo collection, normalization, persistence, and planning |
| `src/betlog.py` | Placed-bet persistence, validation, settlement, and bankroll totals |
| `src/report.py`, `src/report_assets.py` | Dashboard data construction, serving, and handwritten inline assets |
| `scripts/` | Manual diagnostics and repository checks; not an application dependency |
| `tests/fixtures/raw/` | Versioned captured inputs for deterministic parser and replay tests |
| `tests/fixtures/live_regressions/` | Small dated genuine captures for current live-shape regressions |

## Entry points

- `python -m src` and `python -m src.collector`: odds collector and operational CLI;
  live collection always detects the current state and accepts repeatable additional states.
- `python -m src.promos`: promotions CLI.
- `python -m src.report`: static dashboard generation or local dashboard server.
- `python scripts/probe_sources.py`: manual reachability diagnostics; it is not a normal test.
- `python scripts/verify_betlinks.py`: manual probe of each book's candidate event-URL
  grammar. It is the only thing that may set `verified=True` in `src/betlinks.py`;
  an unverified grammar is a guess and is never linked to.
- `python scripts/detect_state.py`: manual egress detection using the same provider-fallback path.
- `python scripts/recon_sources.py`: manual anonymous first-party traffic research;
  output is ignored until a payload becomes a normal adapter capture and replay test.

## Dependency boundaries

- Source adapters depend on shared schemas, raw envelopes, and source utilities. Core domain modules must not depend on individual adapters.
- Transport, pacing, retry, and HTTP-response guards are shared; venue vocabulary and payload interpretation remain adapter-local.
- Parsing must remain deterministic and must not perform network or database I/O.
- Event reconciliation operates on the full parsed slate before filtering-dependent validation.
- Validation and arbitrage consume normalized models, never raw venue payloads.
- Storage owns SQLite details. Domain calculations should accept models and collections rather than database rows.
- One run has one jurisdiction and route scope; related runs share a batch id.
  State tenants are constructor configuration, never new source/counterparty identities.
- Global sources are fetched once per batch. Direct global venues remain
  actionable; republished global observations are diagnostic-only.
- Whether a venue can be *bet from the US* is a separate axis from view-only, held
  in `registry.US_UNAVAILABLE_SOURCE_KEYS`. Those venues stay collected and stored
  — they carry the sharpest lines — but the dashboard defaults to a US-only view
  and the switch is opt-in. Because detection is Python and the page is a static
  file, `_arb_payload` precomputes both bundles per run (`with_offshore` alongside
  the US-only default); the client selects, it never recomputes.
- A republisher's host is state-neutral; the book it republishes is not. Action
  Network files one book id per state licence, so a state run resolves ids
  through `Jurisdiction.republished` rather than reusing the base registry
  config. A book with no licence in that state is `UNAVAILABLE` and is not
  built there — never pointed at another state's book.
- No required book is trusted on one unconfirmed feed. `src/coverage.py`
  declares, per jurisdiction, the books that must be observed either
  first-party or by at least two independent republishers **of that state's own
  licence** that agree. A state with no declaration is reported as unchecked,
  not as clean.
- Republishers that watch a different licence than the state's own book
  (VegasInsider's Las Vegas column against an Action Network state id) are
  excluded from the two-feed count **and** from the price-agreement test. They
  are recorded and named in the finding as context, so a legitimate
  cross-licence difference cannot masquerade as drift and an out-of-state number
  cannot stand in for the state licence. A book watched only from out of state
  is `NON_LOCAL_ONLY`, an error rather than a thin feed.
- An import-time invariant refuses a required-book table whose locality labels
  cannot be true: a feed declared same-licence must file a book id for that
  exact state, checked against the jurisdiction's own republished routes.
- Two local feeds that were never compared do not satisfy the rule. Below the
  distinctness module's shared-selection floor the comparison is undecided, which
  is not agreement, so the book is `AGREEMENT_UNPROVEN` at `WARNING` rather than
  ticked as cross-checked.
- Whether a position is takeable from a state is one predicate,
  `coverage.withhold_non_local`, used by the collector, `collector arb`, `collector
  lines` and the dashboard alike — and *whether it applies to a given run* is one
  more, `coverage.locality_applies`, for the same reason. It reads
  `registry.takeable_from_state`: the state's retail licences **plus** the
  nationwide first-party venues that hold no state sportsbook licence to begin
  with. Not `state_licensed_keys` — building it on the licences alone called Kalshi
  and Polymarket out-of-state in every jurisdiction and withheld a legal position
  from the report, the dashboard and the SMS. Not the keys a run happened to build,
  and not the jurisdiction's whole route table, which still lists books the
  operator holds no licence for. Say "reachable", not "licensed", on every surface
  that reports it. The count withheld is reported everywhere it is applied; a
  silently shorter list reads as a quiet market and disagrees with every other
  surface describing the same run.
- Only a scope the operator widened on purpose (`global`, `all`) is exempt.
  `legacy` — what the store backfills onto rows predating the `route_scope` column
  — is governed, because whether a leg is reachable from a state is a fact about
  the book and the state, not about what the run claimed to collect. The deny-list
  direction is deliberate: an unrecognised scope gets the filter, so the cost of
  being wrong is a withheld position with its count printed rather than an SMS
  naming a book nobody can reach.
- Promotions may reuse settings, raw storage, and transport guards, but its schema, registry, and database remain separate.
- The bet ledger has a separate schema and lifecycle from both collected odds and promotions; collection never writes or deletes it.
- The report layer may read and combine all three domains. Only its localhost control plane writes the bet ledger; collection/domain modules must not depend on report rendering.
- `src/report_assets.py` is handwritten presentation source (`CSS`, `BODY`, and `JS`), not generated output. Generated dashboards and runtime captures belong under ignored `data/` paths.
- The dashboard builds one panel at a time. A panel is rendered on arrival, marked
  stale when the run or sport changes, and unloaded when it leaves the screen; only
  the masthead chrome and the nav counts are computed for panels that are not on
  screen. Rendering every panel up front cost 577k DOM nodes and about a second of
  blocked main thread per interaction, and never reclaimed any of it. Long lists are
  appended in chunks, and rows held back are stated on screen and reachable by
  click, never only by scrolling.
- Routing unloads the panel being left *before* building the one being arrived at.
  Two panel names may share a renderer and therefore a set of regions, so evicting
  afterwards blanks the panel just revealed and nothing rebuilds it.
- Filter state (league, book) is reconciled on the run or sport change itself, not
  inside a panel renderer, because only one panel renders. The nav counts read that
  state, so a choice left impossible by the new scope has to be cleared before them.
- The US-only switch filters at `currentRows()`, the one place every panel reads its
  rows from, and therefore goes through the same reconcile-then-rebuild path as a
  sport change rather than repainting the panel on screen. Books is the deliberate
  exception: it lists every venue and marks the unbettable ones, because hiding one
  there would make its own health count disagree with the run it describes.
- A control shared by two panels renders only the panel on screen, so it also has to
  reschedule the nav counts: the panel left unbuilt has no renderer to write its own
  count, and would otherwise keep the number from before the filter.
- Typed inputs are debounced and deliberate picks are not. A panel that cannot show a
  requested subject says so specifically rather than reusing the nothing-selected
  wording, because the breadcrumb still names what the reader opened.
- The embedded payload's text is dropped once parsed, and price-movement analysis is
  memoized on the sport alone — it answers a question about every embedded run, so
  the run being viewed cannot change it.
- Reusable detection, probe-cache, and validation behavior belongs in `src/`; scripts should only parse arguments, call it, and present results.
- Exact-state retail descriptors pass their state into transport selection.
  `ODDS_HTTP_PROXY_<STATE>` takes precedence over the legacy global proxy, and
  proxy credentials never enter raw envelopes, research manifests, or logs.
- Runtime dependencies are declared in `requirements.txt`; test-only dependencies are in `requirements-dev.txt`.
