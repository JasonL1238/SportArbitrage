# Testing and validation

## Environment

CI and local development target Python 3.13.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Install Chromium only for browser-mode collection or manual browser diagnostics:

```bash
.venv/bin/python -m playwright install chromium
```

Live collection can require a licensed-region exit. Use
`ODDS_HTTP_PROXY_<STATE>` for a state-specific exit; the legacy
`ODDS_HTTP_PROXY` remains a fallback. Live calls are intentionally excluded from
automated validation. Tests use tracked captures under `tests/fixtures/raw/`
and narrow current-shape captures under `tests/fixtures/live_regressions/`.
Node.js is **required in CI and expected locally**: every executable check over
`src/report_assets.py` runs through `tests/dashboard_smoke.mjs`, and
`tests/test_report.py` *skips* those cases when Node is missing rather than
failing — so a machine without it reports a green suite that never executed the
dashboard at all. `.github/workflows/tests.yml` pins it with `actions/setup-node`
for that reason. Locally, install Node 20+ before claiming the suite passed, and
say so explicitly if you ran without it.

Manual browser research requires Playwright. `ODDS_BROWSER_CHANNEL=chrome` may
select an installed stable Chrome channel when the bundled Chromium differs,
but research must remain anonymous and must not automate CAPTCHA, login, or
account state. Sanitized output under `data/research/` is ignored and does not
count as a test fixture.

Use `.venv/bin/python` below when the virtual environment is not activated.

## Tool availability

| Check | Status | Command |
| --- | --- | --- |
| Tests | Configured | `python -m pytest <focused paths> -q` |
| Syntax/import compilation | Configured | `python -m compileall -q src scripts` |
| Agent-document drift | Configured and run in CI | `python scripts/check_agent_docs.py` |
| Lint | Configured and run in CI | `ruff check src scripts tests` |
| Static types | Not configured | Do not claim type-check success or invent a command |
| Package build | No build artifact | Use focused tests and `compileall`; the dashboard is runtime output |

## Targeted tests

Start with the test containing the changed symbol or the closest domain group:

| Change | Command |
| --- | --- |
| Schema/vocabulary | `python -m pytest tests/test_schema.py tests/test_vocab.py -q` |
| Participant/event identity | `python -m pytest tests/test_participants.py tests/test_events.py -q` |
| Validation | `python -m pytest tests/test_validation.py tests/test_validation_adversarial.py -q` |
| Arbitrage/settlement | `python -m pytest tests/test_arb.py tests/test_arb_scaling.py -q` |
| Source shared contract | `python -m pytest tests/test_source_contract.py tests/test_source_client.py tests/test_guards.py -q` |
| One source adapter | `python -m pytest tests/test_<venue>_adapter.py -q` when present; otherwise select its tests with `pytest -k <venue>` |
| Collection/replay | `python -m pytest tests/test_pipeline.py tests/test_fixture_replay.py tests/test_integration.py -q` |
| Promotions | `python -m pytest tests/test_promos.py tests/test_promo_planner.py -q` |
| Placed-bet ledger | `python -m pytest tests/test_betlog.py -q` |
| Dashboard | `python -m pytest tests/test_report.py -q` |
| Jurisdiction/detection/cache/multi-state batch | `python -m pytest tests/test_jurisdictions.py tests/test_probe_sources.py -q` |
| Required-book observation rule (per-state book ids, two-feed cross-check) | `python -m pytest tests/test_coverage.py -q` |
| First-party research transport / current Hard Rock shape | `python -m pytest tests/test_source_research.py tests/test_draftkings_hardrock.py -q` |
| Agent documentation | `python -m pytest tests/test_agent_docs.py -q` then `python scripts/check_agent_docs.py` |

For one failing case, use its node id: `python -m pytest path/to/test.py::TestClass::test_case -q`.

## Package-level checks

After focused tests pass, run checks for every affected boundary:

```bash
python -m pytest tests/test_source_contract.py tests/test_pipeline.py tests/test_integration.py -q
python -m pytest tests/test_promos.py tests/test_promo_planner.py -q
python -m pytest tests/test_report.py -q
ruff check src scripts tests
python -m compileall -q src scripts
python scripts/check_agent_docs.py
```

`compileall` is a syntax/import-compilation check, not a static type checker. Lint is
Ruff, configured in `pyproject.toml` and expected to report **All checks passed**; the
selected rules are correctness-only, and every exemption there carries the reason it is
exempt. No static type checker is configured; do not claim type-check success. If one is
introduced, add it to `requirements-dev.txt`, configure it explicitly, make the baseline
pass, and update this document and CI in the same change.

## Full validation

Run before completion when a change crosses boundaries, changes shared contracts, touches persistence or migrations, or is otherwise high risk:

```bash
python -m pytest tests/ -q
ruff check src scripts tests
python -m compileall -q src scripts
python scripts/check_agent_docs.py
```

The full suite currently contains thousands of cases and exercises real captured payloads offline. Do not substitute a live collection run for it.

### Baseline

The suite is expected fully green, so any failure is a regression rather than
baseline. Report the full suite's exact result.

Two rules survive the blocker that used to sit here (three Action Network ids
with no genuine captures, deregistered 2026-08-09 — the measurements and the
re-registration condition are in `docs/evidence/action-network.md`
§ "Three registered ids carry no odds on either endpoint"): **never synthesize a
fixture**, and **never unregister a source to hide a failure**. That removal was
a recorded operator decision about feeds Action Network had dropped, which is
what makes it different from clearing red tests.

## Build and smoke checks

There is no package build step. The deliverable dashboard is generated by `python -m src.report`; use `tests/test_report.py` for routine validation because it creates isolated data and invokes `tests/dashboard_smoke.mjs` when Node is installed. Generate or serve a real dashboard only when the task specifically concerns local runtime behavior.

Report each command exactly as passed, failed, skipped, or unavailable. Never infer success from a related check.
