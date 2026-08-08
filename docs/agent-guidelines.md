# Agent guidelines

This is the canonical instruction set for coding agents in this repository. Tool-specific instruction files are adapters, not separate rule sets.

## Explore

- Start with the smallest relevant module and read the nearest applicable `AGENTS.md` or `CLAUDE.md`.
- Use `docs/repository-map.md` to choose a directory and `docs/architecture.md` to understand boundaries.
- Search for symbols, callers, and tests before opening large files. Read only the relevant ranges.
- For files listed as context hotspots in the repository map, locate the named section or symbol first; do not read the whole file by default.
- Avoid repo-wide scans unless the change crosses boundaries or local searches fail.
- Ignore generated files, logs, coverage, build output, caches, vendored code, virtual environments, and raw datasets unless the task requires them. Tracked fixtures under `tests/fixtures/` are test inputs, not general exploration targets.

## Edit

- Reuse existing types, utilities, services, adapters, and patterns before adding abstractions.
- Keep edits limited to the requested scope. Do not rewrite unrelated working code or change public behavior without justification.
- Preserve the pipeline contracts: fetch and store raw bytes before parsing; keep parsing deterministic and free of I/O; reconcile events before validation and arbitrage detection.
- Keep source-specific vocabulary in its adapter and shared transport mechanics in `src/sources/_common.py`, `transport.py`, or `guards.py`.
- Keep promotions isolated from the odds database and collection lifecycle.
- Separate generated and handwritten code. Do not hand-edit runtime data under `data/` or caches.
- Update architecture, repository-map, testing, and contract documentation when the corresponding behavior or boundary changes.
- Keep `AGENTS.md` and `CLAUDE.md` as short boundary adapters. Put shared rules here and run `scripts/check_agent_docs.py` after changing the instruction hierarchy.

## Scrape

These three rules govern every new or modified source. They exist because the same brand prices differently under different licences, so a price is only meaningful once you know which licence produced it. Breaking any of them is silent: the wrong-state feed answers with a 200 and plausible numbers.

- **Odds must come from a feed that reports that state's own odds.** A national page, a Las Vegas column, or another state's licence is never a substitute. When only an out-of-state number exists, record it as context and say so; do not present it as the state's price. On the output side the same rule is `coverage.withhold_non_local` with `coverage.locality_applies` deciding which runs it governs — every surface asks those two rather than spelling the condition itself, and each reports the count it withheld.
- **A first-party route must be pinned to the exact state.** Set the state's own host, tenant, subdivision, site path, or segment, and send the request through that state's egress (`ODDS_HTTP_PROXY_<ST>`). A failed exact-state request stays failed. `src/sources/registry.py` has **two** lookups and the difference is load bearing: `descriptor_for_state` refuses a route tagged for another state and is the only one a caller about to open a socket may use, while `replay_descriptor_for_state` / `sources_for_state` deliberately fall back to the base configuration so replay and coverage can enumerate the stable key set — which for an unlicensed state means *another state's* config (`replay_descriptor_for_state("IL", "an_parx")` returns New Jersey's book id 1929). The names carry that warning now — the fallback says `replay_` and the strict one has the plain name — because the pair used to be spelled the other way round and two fetch paths had already reached for the wrong one. No new source may reintroduce a fallback into the strict lookup.
- **Every bettable book needs one direct route or two local third-party feeds that cross-check each other.** The two must both carry the collected state's licence; a cross-licence feed is recorded, named in the finding's detail, and excluded from both the count and the price comparison. `src/coverage.py` enforces this through `check_book_coverage` (named apart from `src/validation.py`'s older per-sport coverage grid), and its import-time invariant refuses a table that labels a national feed local. If a book cannot reach either bar, leave it failing loudly and write down what is missing — do not weaken the rule so the report looks clean.

- Record what a candidate feed actually answered in `docs/SOURCE_FEASIBILITY.md`, including refusals, so a wall is documented once rather than rediscovered.
- Add a state to `src/jurisdictions.py` with real routes only. `TEMPLATE` means structurally known and unproven; `UNAVAILABLE` means the operator holds no licence there. Never invent an endpoint to fill a gap.

## Validate

- Follow `docs/testing.md`: run the smallest relevant tests first, then package-level checks, then full validation when warranted.
- Run targeted tests, linting, type checks, and build/smoke checks before broader validation when those checks are configured.
- Keep command output concise (`pytest -q`, focused paths, quiet syntax checks).
- Do not use live sportsbook collection as routine validation; tests use captured fixtures because live endpoints are regional and unstable.
- Never claim a check passed unless it actually ran and passed. Report skipped, unavailable, and failing checks explicitly.

## Parallel work

- Do not use subagents for simple work.
- When parallel work is justified, give each subagent a narrow, non-overlapping scope with an explicit output and validation responsibility.
- Do not let multiple agents edit the same files. Integrate and validate all parallel results before completion.

## Complete

- Review the final diff for unrelated edits, generated artifacts, secrets, and accidental behavior changes.
- Confirm documentation matches the implemented architecture and commands.
- Report files changed, structural changes, validation commands and results, deferred work, and remaining uncertainty.
- Stop only when the requested outcome is implemented or a concrete blocker is reported.
