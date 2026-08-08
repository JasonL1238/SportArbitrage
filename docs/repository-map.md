# Repository map

Use this map before exploring. Search for the relevant symbol and its tests, then open only the required ranges.

| Path | Purpose | Common changes |
| --- | --- | --- |
| `src/collector.py` | Odds orchestration and CLI | Collection lifecycle, replay, CLI commands, coverage reporting |
| `src/sources/` | Venue integrations | Endpoint, payload mapping, source health, shared HTTP behavior |
| `src/sources/research.py` | First-party discovery boundary | Research profiles, sanitization, resumable ignored manifests |
| `src/schema.py`, `src/vocab.py` | Normalized contracts | Quote fields, closed vocabulary, period and outcome rules |
| `src/leagues.py`, `src/participants.py`, `src/rosters.py` | Identity metadata | League support, aliases, participant resolution |
| `src/events.py` | Fixture joins | Event keys, orientation, time clustering |
| `src/validation.py` | Cross-row correctness | Findings, thresholds, completeness and plausibility checks |
| `src/arb.py` | Opportunity calculation | Grouping, stakes, pushes, liquidity, rejection reasons |
| `src/betlinks.py` | Where a leg gets placed | Mirror→book resolution, live-verified event grammars, league fallback |
| `src/alerts.py` | Arb notification | ROI floor, dedupe, Messages/Twilio transports, fail-soft send |
| `src/store.py`, `src/raw_store.py` | Persistence | SQLite schema/migrations and raw replay envelopes |
| `src/jurisdictions.py`, `src/state_selection.py` | Retail routing and batch selection | IL/PA/NJ/DC routes, status, detection, selection |
| `src/coverage.py` | State locality: required-book rule and output labelling | `check_book_coverage` (per-state book list, first-party vs two-agreeing-republisher) plus `locality_marking` / `locality_applies` |
| `src/egress.py`, `src/probe_cache.py` | Jurisdiction diagnostics | Hashed egress record and TTL/status cache |
| `src/promos/` | Promotions subsystem | Promo adapters, enrichment, deduplication, storage, planning |
| `src/betlog.py` | Placed-bet ledger | Position snapshots, settlement edits, exact-money bankroll totals |
| `src/report.py`, `src/report_assets.py` | Dashboard | Report payload/server and handwritten CSS/HTML/JavaScript |
| `tests/` | Automated validation | Unit, contract, integration, replay, and dashboard tests |
| `tests/test_adversarial_findings.py` | Regression archive | Search for the affected symbol or finding; never open the full file by default |
| `tests/fixtures/raw/` | Captured payloads | Add only when a parser regression needs a representative fixture |
| `tests/fixtures/live_regressions/` | Dated genuine live-shape samples | Keep narrow; require offline parser assertions and no credentials |
| `docs/` | Contracts and operating knowledge | Architecture, inputs, source evidence, testing, agent guidance |
| `scripts/` | Manual maintenance/diagnostics | Source probes and repository checks; not application imports |
| `.github/workflows/` | CI entry points | Keep commands synchronized with `docs/testing.md` |
| `requirements*.txt` | Runtime/dev dependencies | Add only justified direct dependencies with bounded versions |
| `data/` | Ignored runtime output | Never edit as source; inspect only for an explicit runtime diagnosis |

## Context hotspots

Search these files by section or symbol before reading ranges:

| File | Internal boundaries / useful search |
| --- | --- |
| `src/collector.py` | `# ── one run`, `# ── replay`, `# ── CLI`, or the command handler name |
| `src/report.py` | `build_report`, payload helpers, `# ── rendering`, `# ── CLI`, `_serve` |
| `src/report_assets.py` | Exactly three handwritten constants: `CSS`, `BODY`, `JS`; for render scheduling search `PANEL_RENDERERS`, `ensurePanel`, `fillInChunks` |
| `src/arb.py` | Arithmetic, settlement model, grouping, detection, best-price surface |
| `src/validation.py` | Row-level, market-level, cross-source, coverage, availability |
| `src/promos/planner.py` | Text parsing, slate context, solving, payloads, per-offer strategies |
| `tests/test_adversarial_findings.py` | Search the production symbol, regression phrase, or exact test name |

## Where changes belong

- New or changed venue: adapter in `src/sources/`, registration in `src/sources/registry.py`, focused adapter tests, and source/contract documentation.
- Shared fetching behavior: `src/sources/_common.py`, `transport.py`, or `guards.py`, plus client/guard tests.
- Anonymous application/XHR/WebSocket investigation: reusable capture and
  redaction in `src/sources/browser.py` / `research.py`; thin manual invocation
  in `scripts/recon_sources.py`. Ignored research output is not a fixture.
- New normalized concept: begin in `src/schema.py` or `src/vocab.py`, then update validation, storage, report serialization, adapters, and contract tests.
- Event matching: `src/events.py` and participant/league modules; validate with event, participant, and integration tests.
- Arbitrage behavior: `src/arb.py`, commission, settlement, distinctness, or redundancy modules; add focused mathematical and adversarial tests.
- Promo source or offer logic: remain under `src/promos/`; do not add promo tables to the odds store.
- Placed-bet persistence or bankroll arithmetic: `src/betlog.py`; keep it out of the replaceable odds and promo databases.
- Dashboard data and server behavior: `src/report.py`; visual/interaction assets: `src/report_assets.py`; validate through `tests/test_report.py`.
- Retail state routing: `src/jurisdictions.py`, `src/state_selection.py`, scoped registry builders, batch/run persistence, and focused jurisdiction/probe tests.
- Which books a state must be able to see, and through how many feeds: `src/coverage.py` plus the republished id table in `src/jurisdictions.py`; validate with `tests/test_coverage.py`.

## Validation shortcuts

See `docs/testing.md` for setup and escalation. Typical focused commands:

```bash
python -m pytest tests/test_events.py -q
python -m pytest tests/test_arb.py tests/test_arb_scaling.py -q
python -m pytest tests/test_source_contract.py tests/test_<venue>_adapter.py -q
python -m pytest tests/test_promos.py tests/test_promo_planner.py -q
python -m pytest tests/test_report.py -q
python -m pytest tests/test_jurisdictions.py tests/test_probe_sources.py -q
python -m pytest tests/test_coverage.py tests/test_redundancy.py -q
python -m pytest tests/test_source_research.py tests/test_draftkings_hardrock.py -q
python -m pytest tests/test_agent_docs.py -q
python scripts/check_agent_docs.py
```

Do not enumerate or open all raw fixtures to understand a parser. Find the fixture references in its focused test first.
