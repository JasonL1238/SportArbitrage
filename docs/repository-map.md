# Repository map

Use this map before exploring. Search for the relevant symbol and its tests, then open only the required ranges.

| Path | Purpose | Common changes |
| --- | --- | --- |
| `src/collector.py` | Odds orchestration and CLI | Collection lifecycle, replay, CLI commands, coverage reporting |
| `src/sources/` | Venue integrations | Endpoint, payload mapping, source health, shared HTTP behavior |
| `src/schema.py`, `src/vocab.py` | Normalized contracts | Quote fields, closed vocabulary, period and outcome rules |
| `src/leagues.py`, `src/participants.py`, `src/rosters.py` | Identity metadata | League support, aliases, participant resolution |
| `src/events.py` | Fixture joins | Event keys, orientation, time clustering |
| `src/validation.py` | Cross-row correctness | Findings, thresholds, completeness and plausibility checks |
| `src/arb.py` | Opportunity calculation | Grouping, stakes, pushes, liquidity, rejection reasons |
| `src/store.py`, `src/raw_store.py` | Persistence | SQLite schema/migrations and raw replay envelopes |
| `src/promos/` | Promotions subsystem | Promo adapters, enrichment, deduplication, storage, planning |
| `src/report.py`, `src/report_assets.py` | Dashboard | Report payload/server and handwritten CSS/HTML/JavaScript |
| `tests/` | Automated validation | Unit, contract, integration, replay, and dashboard tests |
| `tests/fixtures/raw/` | Captured payloads | Add only when a parser regression needs a representative fixture |
| `docs/` | Contracts and operating knowledge | Architecture, inputs, source evidence, testing, agent guidance |
| `scripts/` | Manual maintenance/diagnostics | Source probes and repository checks; not application imports |
| `data/` | Ignored runtime output | Never edit as source; inspect only for an explicit runtime diagnosis |

## Where changes belong

- New or changed venue: adapter in `src/sources/`, registration in `src/sources/registry.py`, focused adapter tests, and source/contract documentation.
- Shared fetching behavior: `src/sources/_common.py`, `transport.py`, or `guards.py`, plus client/guard tests.
- New normalized concept: begin in `src/schema.py` or `src/vocab.py`, then update validation, storage, report serialization, adapters, and contract tests.
- Event matching: `src/events.py` and participant/league modules; validate with event, participant, and integration tests.
- Arbitrage behavior: `src/arb.py`, commission, settlement, distinctness, or redundancy modules; add focused mathematical and adversarial tests.
- Promo source or offer logic: remain under `src/promos/`; do not add promo tables to the odds store.
- Dashboard data and server behavior: `src/report.py`; visual/interaction assets: `src/report_assets.py`; validate through `tests/test_report.py`.

## Validation shortcuts

See `docs/testing.md` for setup and escalation. Typical focused commands:

```bash
python -m pytest tests/test_events.py -q
python -m pytest tests/test_arb.py tests/test_arb_scaling.py -q
python -m pytest tests/test_source_contract.py tests/test_<venue>_adapter.py -q
python -m pytest tests/test_promos.py tests/test_promo_planner.py -q
python -m pytest tests/test_report.py -q
python scripts/check_agent_docs.py
```

Do not enumerate or open all raw fixtures to understand a parser. Find the fixture references in its focused test first.
