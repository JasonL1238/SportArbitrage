# Architecture

## System shape

SportArbitrage is a local Python application with two collection domains and one read-only presentation layer:

- The odds pipeline collects venue data, normalizes quotes, reconciles event identity, validates the slate, detects arbitrage, and stores results.
- The promotions pipeline collects and normalizes offers into a separate database, then builds plans against current odds at report time.
- The report layer reads stored odds and promotions and produces a self-contained dashboard; its local server optionally invokes the collectors.
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
- Promotions may reuse settings, raw storage, and transport guards, but its schema, registry, and database remain separate.
- The report layer may read and combine both domains; collection/domain modules must not depend on report rendering.
- `src/report_assets.py` is handwritten presentation source (`CSS`, `BODY`, and `JS`), not generated output. Generated dashboards and runtime captures belong under ignored `data/` paths.
- Reusable detection, probe-cache, and validation behavior belongs in `src/`; scripts should only parse arguments, call it, and present results.
- Exact-state retail descriptors pass their state into transport selection.
  `ODDS_HTTP_PROXY_<STATE>` takes precedence over the legacy global proxy, and
  proxy credentials never enter raw envelopes, research manifests, or logs.
- Runtime dependencies are declared in `requirements.txt`; test-only dependencies are in `requirements-dev.txt`.
