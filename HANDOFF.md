# Multi-state collection implementation handoff

Last updated: 2026-08-03

## Objective

Implement one scrape batch for `IL`, `PA`, `NJ`, and `DC`: automatically include
the detected egress state, accept additional states in the CLI and dashboard,
use only exact-state retail odds and promo routes, fetch global sources once,
and keep republished fallback feeds diagnostic-only.

## Non-negotiable behavior

- Never substitute another state's endpoint.
- Direct global venues remain actionable; republished feeds remain visible but
  cannot form arbitrage legs or alerts.
- Promotions require affirmative eligibility for the target state.
- Replay remains offline and resolves the stored run jurisdiction.
- No source keys are removed.
- No geolocation bypass or automatic betting.

## Progress

- [x] Read canonical and nearest repository instructions.
- [x] Confirm clean `main` worktree and documented full-suite fixture blocker.
- [x] Add batch/state selection and exact IL/PA/NJ/DC jurisdiction records.
- [x] Refactor odds registry/collector to explicit state plus one global run.
- [x] Refactor promo registry/collector and enforce confirmed eligibility.
- [x] Add persistence for batch/scope and state-matched promo planning.
- [x] Add dashboard state selector and per-state batch results.
- [x] Update probes, reports, documentation, and tests.
- [x] Run focused/package/full validation and review the final diff.

## Current baseline and known blocker

- Branch: `main`; initially clean and aligned with `origin/main`.
- Illinois is the only live-validated registry state today.
- Pennsylvania routes are candidate-only; template probes from Illinois are not
  live validation.
- Existing Illinois Caesars and Hard Rock configuration incorrectly retains NJ
  legacy endpoints. This implementation must move those routes to NJ and never
  use them for an IL run.
- Full `tests/` is documented red because genuine non-empty captures are missing
  for `an_fliff`, `an_circa`, and `an_superbook`. Do not synthesize captures or
  unregister the sources.

## Completed implementation

`src/jurisdictions.py` now contains exact IL/PA/NJ/DC route records. The old NJ
Caesars/Hard Rock values were moved out of IL; IL now has exact-state template
routes. `src/sources/registry.py` now separates exact-state retail descriptors,
state-neutral sources, and diagnostic-only republished feeds. Compilation and a
small registry construction smoke check pass.

Additive odds/promo run columns now persist `batch_id` and `route_scope`.
`src/collector.py` has explicit jurisdiction inputs, a global cached-fetch
wrapper, and `collect_batch_once`; state runs include the cached global bytes
but must have two producing exact-state first-party sources. Pure-global arbs
are removed from state results and republished sources are view-only.
`src/promos/collector.py` has equivalent per-batch global caching and now keeps
only offers whose normalized eligibility affirmatively includes the run state.
Promo planning resolves an odds run with the same stored jurisdiction.

Dashboard odds and promo requests now submit IL/PA/NJ/DC selections; server-side
detection always unions the actual current state. Report payloads include batch
and route scope, mark global/diagnostic sources, suppress pure-global arbs from
state views, and pair promos only with odds of the same stored jurisdiction.
README and canonical architecture/repository/testing docs are updated; the
multi-state runbook and source-feasibility record now describe strict routing.

Republished Action Network, VegasInsider, and VSiN rows are retained for drift
diagnostics but are never executable arb or promotion legs. Their older
stakeable-failover tests were updated to pin this safer contract.

## Validation evidence

- `python -m pytest -q tests/test_jurisdictions.py tests/test_probe_sources.py tests/test_promos.py tests/test_promo_planner.py tests/test_pipeline.py tests/test_report.py tests/test_redundancy.py tests/test_view_only.py tests/test_adversarial_findings.py -k 'not every_real_skip_reason_has_an_explanation and not the_serve_promo_endpoint_answers_with_plans'`
  → **1,341 passed, 38 deselected**.
- `python -m pytest tests/test_report.py::test_the_serve_promo_endpoint_answers_with_plans -q`
  with localhost socket permission → **1 passed**.
- `python -m pytest tests/ -q --tb=short` → **2,555 passed, 4 failed,
  1,395 errors**. The four direct failures are the three missing-capture report
  cases plus the sandboxed localhost socket case; every error is the single
  `registered_raws` setup cascade caused by missing `an_fliff`.
- `python -m compileall -q src scripts` → passed.
- `python scripts/check_agent_docs.py` → passed (5 adapter pairs).
- `git diff --check` → passed.

## Remaining blockers and resume point

No live route was promoted in this implementation session. PA/NJ/DC remain
templates until their registered adapters return usable quotes from matching
detected egress. The current documented structural PA probe was from Illinois
and is not PA validation. Run `python scripts/detect_state.py` and then
`python scripts/probe_sources.py --state PA --force` from a legitimate PA exit
before changing PA route statuses.

The offline suite cannot turn green until genuine, non-empty captured envelopes
are committed for `an_fliff`, `an_circa`, and `an_superbook`. Do not synthesize
fixtures or remove those source keys. If another session continues, start with
those captures or matching-egress live probes; the code and focused tests for
the multi-state batch are complete.

## Final dead-code cleanup

Before commit, the changed modules were checked with Ruff's unused
import/local rules and Vulture. Removed the unused `source_config()` helper and
export, plus the unreachable `RouteStatus.LEGACY` enum/diagnostic branch after
exact-state routing eliminated legacy endpoint fallback. Historical blank-state
database runs still retain their separate `legacy` presentation label.

Post-cleanup validation: **563 passed, 38 deselected** across jurisdiction,
probe, report, pipeline, promo, redundancy, and view-only tests; Ruff
`F401/F811/F841`, Vulture at 90% confidence, compilation, agent-doc drift,
agent-doc tests, and `git diff --check` all passed.
