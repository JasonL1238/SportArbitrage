# First-party sportsbook acquisition handoff

Last updated: 2026-08-03 (Hard Rock IL complete; other three diagnosed)

## Active objective

Establish anonymous first-party odds acquisition for Caesars, Hard Rock,
Fanatics, and bet365. Work state-by-state against exact licensed routes, keep
all research artifacts sanitized, and do not count Action Network or
VegasInsider as first-party success.

## Active completion contract

- Caesars: IL, PA, NJ, and DC.
- Hard Rock: IL and NJ; PA/DC remain explicitly unavailable.
- Fanatics: IL, PA, NJ, and DC.
- bet365: IL, PA, and NJ; DC remains explicitly unavailable.
- Each validated route needs two matching-egress runs with active pregame core
  markets, parser-clean genuine captures, and offline replay coverage.
- No account/login automation, CAPTCHA bypass, synthetic fixtures, or betting.

## Current implementation checkpoint

- [x] Read canonical, source-boundary, test-boundary, and existing handoff rules.
- [x] Confirmed clean `main` baseline at `369a622`.
- [x] Add privacy-safe state-specific proxy selection and egress validation.
- [x] Add reusable browser page-response/WebSocket research capture.
- [x] Add the narrow source/transport/state research CLI.
- [x] Re-discover and validate Hard Rock IL live application traffic.
- [ ] Validate Caesars first-party core odds (current IL calls are WAF-blocked).
- [ ] Implement Fanatics and bet365 adapters from genuine first-party payloads.
- [x] Commit-ready genuine Hard Rock IL RawResponse captures and offline replay.
- [x] Run focused, package-level, and full validation; the documented missing
  Action Network capture baseline remains red.

## Immediate resume point

For bet365, find why a normal persistent browser receives the full anonymous IL
MLB slate while every isolated Playwright profile opens the correct IL shell but
never completes its catalog subscription. Bundled Chromium, installed stable
Chrome, headed, mobile, and persistent variants have all been tried; do not copy
user profile state or bypass a challenge. Fanatics requires legitimate native-
client research; no Android tooling is installed and the local Xcode simulator
runtime is incomplete. Caesars needs operator-approved access or a naturally
successful anonymous session before more parser work is useful.

## Experiment log

All experiments used detected IL egress fingerprint
`34e56847c7eca6389c6c105ffa82e4ac5da03edebf3a87e0eca0f481921a9561`.
Ignored sanitized artifacts are under `data/research/`.

- 2026-08-04 00:29Z-00:45Z, Caesars IL, explicit
  `sportsbook.caesars.com/us/il/bet/`: exact IL config, v3 sports menu, team
  metadata, and IL Diffusion sockets returned 200. Current v4 navigation/home/
  quick-picks returned CloudFront 403. Direct v3 MLB highlights also returned
  403. The sports menu proved the old hard-coded competition list is incomplete
  (current MLB id `04f90892-3afa-4e84-acce-5b89f151063d`). Headed and persistent
  browser trials returned the same 403. Next: operator-approved access or a
  naturally successful anonymous payload; do not bypass AWS WAF.
- 2026-08-04 00:34Z, Hard Rock IL, headless discovery plus two adapter runs:
  app traffic proved segment `il`, channel `ILLINOIS_ONLINE`, millisecond
  `eventTime`, and selection-name handicaps. Adapter updated. Two separate
  matching-egress runs each produced 202 quotes with zero rejections. A later
  probe produced 172 quotes with zero rejections; the exact IL route is now
  validated. Genuine current envelopes replay to at least 100 quotes without
  network. NJ still needs matching-NJ egress.
- 2026-08-04 00:35Z, Fanatics IL, headless page: `sportsbook.fanatics.com`
  redirected to the `betfanatics.com` marketing site and produced no first-party
  odds traffic. Official operator material says the sportsbook is a native iOS/
  Android experience. Next: mobile-client research without login; do not
  register an adapter until a genuine anonymous odds payload exists. Android
  platform tools are absent and the local iOS simulator runtime is incomplete.
- 2026-08-04 00:36Z-01:03Z, bet365 IL: an initial challenge later cleared
  naturally. The exact `www.il.bet365.com` application, IL state code 28, IL
  locale, compact pull-pod protocol, and `365lpodds.com` sockets were confirmed.
  A normal persistent browser displayed a complete anonymous MLB slate with
  moneyline, run line, and total prices. Fresh isolated Playwright sessions—
  mobile, headed, persistent, and installed stable Chrome—load the exact IL
  navigation but stay behind the application preloader; their catalog sockets
  return handshake/time frames without a slate. bet365 is viable but is not yet
  safe to register as an unattended adapter. Generated Caesars/bet365 browser
  profiles were deleted after diagnosis so anonymous session cookies are not
  retained; only sanitized ignored manifests remain.

## Validation evidence for this task

- Focused research/transport/Hard Rock/jurisdiction/client/probe tests:
  **53 passed**.
- Focused promotions/report group: **167 passed, 4 failed**. Three failures are
  the documented missing `an_fliff`, `an_circa`, and `an_superbook` captures;
  the localhost server case is sandbox-only and passed separately with loopback
  permission (**1 passed**).
- Source contract/pipeline/integration group: **135 passed, 1,373 errors**;
  every error is the known `registered_raws` cascade beginning at missing
  `an_fliff`.
- Full `tests/`: **2,565 passed, 4 failed, 1,395 errors**. The four direct
  failures are the same three missing-capture guards and sandboxed localhost
  bind; all errors are the same registered-fixture cascade.
- `python -m compileall -q src scripts`, agent-document drift check, agent-doc
  test, and `git diff --check`: passed.

## Historical multi-state delivery

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
