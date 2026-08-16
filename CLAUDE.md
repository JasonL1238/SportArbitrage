# AGENTS.md

Canonical instructions for coding agents in this repository. This file is the whole
entry point: a routine change needs nothing else. Deeper documents exist for depth,
and the routing table below says when each one is worth its tokens.

**Vendor neutrality.** `AGENTS.md` is the only file anyone edits. Every other vendor
filename — `CLAUDE.md` today, any future one — is a byte-identical **generated copy**
of the `AGENTS.md` beside it, so there is one rule set for every tool. Never edit a
copy: run `python scripts/check_agent_docs.py --fix` to regenerate them, and add a new
vendor by adding one name to `VENDOR_ALIASES` in that script. CI runs the same check
without `--fix`, so drift fails the build rather than splitting the rules in two.
Nested `AGENTS.md` files (`src/sources/`, `src/promos/`, `scripts/`, `tests/`) add
boundary rules and never replace these.

## What this repository is

A local Python application that collects sportsbook odds and promotions, normalizes
them into one closed schema, reconciles event identity across venues, validates the
slate, detects arbitrage, stores everything in SQLite, and renders a self-contained
dashboard. No live network in tests; parsers run against tracked captures.

## Read only what the task needs

| If you are… | Open | Cost |
| --- | --- | --- |
| making a routine change | nothing — this file is enough | — |
| crossing a boundary, or unsure which module owns a behavior | `docs/architecture.md` | ~175 lines |
| deciding what to run, or reporting a result | `docs/testing.md` | ~120 lines |
| asking "did we already try this venue?" | `docs/SOURCE_FEASIBILITY.md` — an index — then the one `docs/evidence/` file it names | ~60-line index, then 90-420 lines |
| adding a source, or changing the normalized quote contract or a field's meaning | `docs/INPUT_CONTRACT.md` — it states what a new adapter must deliver, not only what the fields mean | ~390 lines |
| changing state routing, licences, or which state a run may fetch | `docs/MULTI_STATE.md` | ~260 lines |

Search for the symbol and its closest test before opening any large file, and read
only the range you need. Do not read a file listed as a hotspot end to end.

## Rules

### Explore

- Start at the smallest relevant module and read the nearest `AGENTS.md`.
- Search for symbols, callers, and tests before opening large files.
- Avoid repo-wide scans unless the change crosses boundaries or local searches fail.
- Ignore generated files, logs, caches, build output, virtual environments, and raw
  datasets. Tracked fixtures under `tests/fixtures/` are test inputs, not exploration
  targets — find the fixture a parser uses through its focused test.

### Edit

- Reuse existing types, utilities, adapters, and patterns before adding abstractions.
- Keep edits inside the requested scope. Do not rewrite working code or change public
  behavior without justification.
- Preserve the pipeline contracts: fetch and store raw bytes before parsing; keep
  parsing deterministic and free of I/O; reconcile events before validation and
  arbitrage.
- Keep venue vocabulary in its adapter and shared transport in `src/sources/_common.py`,
  `src/sources/transport.py`, or `src/sources/guards.py`.
- A row's *identity* is shared and its *price* is not: build rows with
  `_common.priced_quote(fixture, …)` and resolve events onto `_common.Fixture` rather
  than restating either. Which market a label means and how a line is signed stay in
  the venue adapter, where a per-book trap can be documented beside the code.
- Keep promotions isolated from the odds database and collection lifecycle.
- Never hand-edit runtime data under `data/`, caches, or generated output.
- Update `docs/architecture.md`, `docs/testing.md`, this file, and the contract docs
  when the behavior or boundary they describe changes. `.github/workflows/` and
  `docs/testing.md` state the same commands and change together.
- Add a dependency to `requirements.txt` / `requirements-dev.txt` only when it is a
  justified direct dependency, with a bounded version.
- `tests/fixtures/live_regressions/` stays narrow: dated genuine samples, asserted
  offline, never carrying credentials.

### Scrape

These three rules govern every new or modified source. They exist because the same
brand prices differently under different licences, so a price is only meaningful once
you know which licence produced it. Breaking any of them is silent: the wrong-state
feed answers with a 200 and plausible numbers.

- **A state's own price must come from a feed that reports that state's own odds — but
  national and out-of-state odds may be collected and shown, so long as every reader
  can see what they are.** A national page, a Las Vegas column, or another state's
  licence is never a substitute for the state's price; it is context, and each surface
  must label it ("not reachable from {ST}") rather than presenting it as the state's
  own. On the output side the rule is `coverage.locality_marking` with
  `coverage.locality_applies` deciding which runs it governs — every surface asks those
  two rather than spelling the condition itself, labels each unreachable leg, and
  reports how many positions have no local leg at all.
- **A first-party route must be pinned to the exact state.** Set the state's own host,
  tenant, subdivision, site path, or segment, and send the request through that state's
  egress (`ODDS_HTTP_PROXY_<ST>`). A failed exact-state request stays failed.
  `src/sources/registry.py` has **two** lookups and the difference is load bearing:
  `descriptor_for_state` refuses a route tagged for another state and is the only one a
  caller about to open a socket may use, while `replay_descriptor_for_state` /
  `sources_for_state` deliberately fall back to the base configuration so replay and
  coverage can enumerate the stable key set — which for an unlicensed state means
  *another state's* config (`replay_descriptor_for_state("IL", "an_parx")` returns New
  Jersey's book id 1929). The names carry that warning because the pair used to be
  spelled the other way round and two fetch paths had already reached for the wrong one.
  No new source may reintroduce a fallback into the strict lookup.
- **Every bettable book needs one direct route or two local third-party feeds that
  cross-check each other.** Both must carry the collected state's licence; a
  cross-licence feed is recorded, named in the finding's detail, and excluded from both
  the count and the price comparison. `src/coverage.py` enforces this through
  `check_book_coverage` (named apart from `src/validation.py`'s older per-sport coverage
  grid), and its import-time invariant refuses a table that labels a national feed
  local. If a book cannot reach either bar, leave it failing loudly and write down what
  is missing — never weaken the rule so the report looks clean.

Also: record what a candidate feed actually answered — including refusals — in the
`docs/evidence/` file that `docs/SOURCE_FEASIBILITY.md` names for the subject, so a
wall is documented once rather than rediscovered.
Add a state to `src/jurisdictions.py` with real routes only: `TEMPLATE` means
structurally known and unproven, `UNAVAILABLE` means the operator holds no licence
there. Never invent an endpoint to fill a gap. Credentials live only in `ODDS_*`
environment variables read through `settings._lookup` — never in a committed
`SourceDescriptor.config`, never as a query parameter.

### Validate

- Run the smallest relevant tests first, then package-level checks, then the full suite
  when the change crosses boundaries or touches persistence. `docs/testing.md` has the
  escalation table.
- Live collection is never routine validation; tests use captured fixtures because live
  endpoints are regional and unstable.
- **`collector arb` and `collector collect` send a real SMS to the operator's phone
  unless you pass `--no-alert`.** They are not a way to look at the data — the
  dashboard and the replay path are. Alert code is exercised only inside pytest with a
  monkeypatched send callable; `notify_opportunities` and `AlertBook.notify` are never
  called against real settings. A review agent doing exactly this texted the operator.
- Never claim a check passed unless it ran and passed. Report skipped, unavailable, and
  failing checks explicitly, with their exact output.

### Complete

- Review the final diff for unrelated edits, generated artifacts, secrets, and
  accidental behavior changes.
- Confirm the documentation still matches the implemented architecture and commands —
  including this file's own map and routing table.
- Report files changed, validation commands and their results, deferred work, and
  remaining uncertainty.

### Parallel work

Do not use subagents for simple work. When justified, give each a narrow,
non-overlapping scope, an explicit output, and its own validation responsibility. Never
let two agents edit the same file; integrate and validate before completion.

## Where things live

| Path | Purpose |
| --- | --- |
| `src/collector.py` | Odds orchestration, replay, and the operational CLI |
| `src/sources/` | Venue adapters; `src/sources/registry.py` registers them, `src/sources/_common.py`, `src/sources/transport.py` and `src/sources/guards.py` are shared |
| `src/sources/base.py` | The source protocol every adapter implements: `fetch_raw()` plus a pure `parse()` |
| `src/sources/research.py` | First-party discovery boundary: profiles, sanitization, ignored manifests |
| `src/schema.py`, `src/vocab.py` | Normalized quote model and closed vocabulary |
| `src/normalize.py` | Odds conversions between decimal, American, and implied |
| `src/settings.py` | Environment settings; `ODDS_*` credentials via `_lookup`, refused at the entry point when missing |
| `src/leagues.py`, `src/participants.py`, `src/rosters.py` | Competition and participant identity |
| `src/events.py` | Event keys, orientation, time clustering |
| `src/validation.py` | Row, market, cross-source, and completeness findings |
| `src/arb.py`, `src/commission.py`, `src/settlement.py` | Net pricing, settlement compatibility, opportunity detection |
| `src/distinctness.py`, `src/redundancy.py` | Counterparty independence and feed-pair completeness |
| `src/betlinks.py` | Where a leg gets placed; `verified=True` is set only on evidence from `scripts/verify_betlinks.py`, never on a guess |
| `src/alerts.py` | ROI floor, dedupe key, one wall-clock, Messages/Twilio transports |
| `src/store.py`, `src/raw_store.py` | Odds SQLite schema and migrations; raw replay envelopes |
| `src/jurisdictions.py`, `src/state_selection.py` | IL/PA/NJ/DC routes, statuses, detection, batch selection |
| `src/coverage.py` | Required-book rule per state plus `locality_marking` / `locality_applies` |
| `src/egress.py`, `src/probe_cache.py` | Hashed egress record and TTL probe cache |
| `src/promos/` | Promotions: adapters, enrichment, dedupe, separate database, planning |
| `src/betlog.py` | Placed-bet ledger, settlement edits, integer-cent bankroll |
| `src/report.py`, `src/report_copy.py`, `src/report_assets.py` | Dashboard payload/server; the page's literals (venue notes, skip reasons, the field table, the glossary); its handwritten CSS/HTML/JS |
| `scripts/` | Manual diagnostics and repository checks; never imported by `src/` |
| `tests/fixtures/raw/` | Genuine captured payloads — never synthesized or hand-edited |
| `data/` | Ignored runtime output; inspect only for an explicit diagnosis |

**Hotspots — search by symbol, never read whole.** Every one of these carries `── `
section banners, so `grep -n '── ' <file>` prints its table of contents and costs one
call. Do that first; the banner names below are only what they were when this was
written, and the grep is what is true now.

| File | Lines | Where to land |
| --- | --- | --- |
| `tests/test_adversarial_findings.py` | 14.9k | 17 banners, one per review round — they say nothing about subject. Search the production symbol or the exact test name instead |
| `src/report_assets.py` | 7.2k | 42 banners, in `/* ── … */` form. `frame`, `panels and routing`, `venues`, `arbitrage`, `overview`. Four handwritten constants — `CSS`, `BODY`, `JS`, and `EMPTY_SHELL`, the nothing-collected-yet page. Every panel count and filter decision is here, not in `src/report.py` |
| `tests/test_promo_planner.py` | 4.0k | 8 banners |
| `src/collector.py` | 3.7k | `coverage`, `one run`, `replay`, `CLI` |
| `src/arb.py` | 2.7k | `the arithmetic`, `settlement model`, `grouping`, `detection`, `best-price surface` |
| `tests/test_report.py` | 2.7k | 8 banners |
| `src/report.py` | 2.3k | `build_report` and `_serve` are the two entry points, and `build_report`'s own 1.4k-line body has no banner inside it. The only two, `rendering` and `CLI`, start at line 1577 |
| `src/validation.py` | 2.5k | `consensus`, `row-level`, `market-level`, `cross-source` |
| `src/promos/planner.py` | 2.2k | `offer-text parsing`, `brand → stakeable odds feeds`, `slate context`, `stake solving and outcome evaluation`, `the scan`, `plan payloads` |
| `src/store.py` | 1.7k | `schema compatibility`, `the store`, then `runs`, `writes`, `reads` inside it |
| `src/sources/registry.py` | 1.3k | mostly one long descriptor list — grep the source key. Three banners group it: `exchanges`, `Action Network multi-book scoreboard`, `prediction markets` |

## Where a change belongs

- **Fixing an existing venue** → its adapter in `src/sources/` and that adapter's own
  tests. Nothing else. Which market a label means, how a line is signed, and which side
  the book called home are all adapter-local, so a wrong price is almost always one
  file. None of the registration bar below applies.
- **Adding a new venue** → adapter in `src/sources/`, registration in `src/sources/registry.py`,
  focused adapter tests, an evidence entry, and a committed genuine capture. The
  full bar is longer than these five: `docs/INPUT_CONTRACT.md` states what the adapter
  must deliver and what makes a venue a *distinct* counterparty rather than a second
  name for one already registered, and the **Registration checklist** in
  `docs/evidence/exchanges-and-mirrors.md` enumerates the nine places a new key has
  to appear — it is written for credentialed exchanges, so read past the
  credential-shaped steps. Its step 8 is the one with no production symbol to search
  for: four per-venue lists in `tests/test_adversarial_findings.py` enumerate every
  registered key by hand, and `grep '"vsin_circa"'` finds exactly those four.
- **Shared fetching behavior** → `src/sources/_common.py`, `src/sources/transport.py`, or
  `src/sources/guards.py`.
- **Anonymous first-party investigation** → reusable capture/redaction in
  `src/sources/browser.py` and `src/sources/research.py`; thin invocation in
  `scripts/recon_sources.py`.
  Ignored research output is never a fixture.
- **New normalized concept** → `src/schema.py` or `src/vocab.py` first, then validation,
  storage, report serialization, adapters, and contract tests.
- **Arbitrage behavior** → `src/arb.py` and the commission/settlement/distinctness
  modules, with focused mathematical and adversarial tests.
- **Event matching** → `src/events.py` and the participant/league modules; validate
  with event, participant, and integration tests.
- **Promotions** → stay inside `src/promos/`; never add promo tables to the odds store.
- **Placed bets and bankroll** → `src/betlog.py`, out of the replaceable odds/promo databases.
- **Dashboard** → the payload and the localhost server are in `src/report.py`, the
  literal copy in `src/report_copy.py`, and everything the reader sees — layout,
  interaction, **and every count, filter and total on the page** — in
  `src/report_assets.py`. A number that disagrees with the run is computed client-side;
  `src/report.py` serializes rows and does not count them. Validate with
  `tests/test_report.py`.
- **State routing** → `src/jurisdictions.py`, `src/state_selection.py`, scoped registry
  builders, and focused jurisdiction/probe tests. Two more places hardcode the states
  and neither is reachable by searching `JURISDICTIONS`: the scrape form's checkboxes
  in `src/report_assets.py` (`id="scrape-states"`) — the server accepts any jurisdiction
  the enum holds, so a state missing here is one the dashboard can never ask for — and
  `betlinks.STATE_SITE`, which fails soft to the stateless door rather than wrong.
- **Which books a state must see, through how many feeds** → `src/coverage.py` plus the
  republished id table in `src/jurisdictions.py`; validate with `tests/test_coverage.py`.

## Commands

```bash
python -m pytest tests/test_<area>.py -q      # focused first — see docs/testing.md
python -m pytest tests/ -q                    # full suite; ~4 minutes, expected green
python -m compileall -q src scripts           # syntax/import check
ruff check src scripts tests                  # lint; configured in pyproject.toml
python scripts/check_agent_docs.py            # vendor-alias and doc-reference check
```
