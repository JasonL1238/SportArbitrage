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
