# Agent instructions

**`AGENTS.md` and `CLAUDE.md` in this repository must be byte-identical.** Claude
Code reads only `CLAUDE.md`; Codex and most other agents read only `AGENTS.md`.
Any edit to either file must be written to both in the same change, or one agent
will be working from stale instructions. `tests/test_agent_docs.py` fails when
they diverge.

## Priorities

- Ignore development labor costs when evaluating or prioritizing implementation
  choices; optimize for product quality, maintainability, and runtime/hosting
  cost.
- **The product must cost nothing to run.** Only permanently free, locally
  runnable, open-source tools. No paid APIs, no trials, no SaaS, no hosted
  browsers, no proxies, no accounts or API keys, and nothing that could later
  require payment. Dependencies are `httpx` and `pydantic`; storage is stdlib
  `sqlite3`.
- Every source is a public endpoint of a sportsbook's own website. Nothing logs
  in, defeats a CAPTCHA, or works around a geo-block or bot protection. A
  challenge, login page, or block is recorded as a failure for that source and
  the run moves on.

## What this repository is

A local pipeline that collects betting odds from sportsbooks' own public
endpoints, normalizes them into one validated schema, and identifies arbitrage
in the collected data. It does not place bets, serve a UI, or send alerts.

```
fetch_raw() → store raw bytes → parse() → reconcile → validate() → arb → persist
```

## Non-negotiable invariants

Each of these exists because violating it produces a *silent* wrong answer.

- **Raw bytes hit disk before anything interprets them,** and `parse()` is a pure
  function of those bytes with no I/O, clock, or state. Replay depends on this
  and nothing else. `collector replay` asserts a re-parse reproduces stored rows
  exactly.
- **`market`, `period`, `selection` and `sport` are closed enums** defined in
  `src/vocab.py`. A source value that cannot be mapped to one is rejected or
  counted out of scope — never passed through as free text. A free-text fallback
  makes unnormalized data look normalized, which is the failure the schema
  exists to prevent.
- **`fetch_raw()` raises rather than returning empty.** An empty return makes a
  Cloudflare block indistinguishable from a day with no games.
- **An in-scope market is emitted whole.** Dropping an unpriced leg makes a
  three-way market byte-identical to a two-way one, and that difference is a tie
  voiding both legs versus losing both.
- **Event identity is reconciled across all sources at once,** before validation.
  An adapter can only number a doubleheader over its own slate, so `#2` is a
  per-source ordinal rather than an identity. `src/events.py` re-derives every
  key by clustering start times globally.
- **Synthetic fixtures, mocks and injected edges are never presented as live
  data,** in output, docs, or commit messages. Label them at the point of use.

## Settlement semantics are sport-dependent

`src/vocab.py` `PERIOD_RULES` is the single authority on what can happen at the
end of a scoring window. **Never default or infer a settlement fact** —
`period_rules()` raises on an unknown `(sport, period)` deliberately, because a
default is a guess about whether a tie voids the bet and that guess is worth the
whole stake.

Facts that break code written against baseball alone:

- `football/full_game` has `tie_possible=True, draw_is_priced=False` — an NFL
  moneyline is two-way and can still tie, so a tie **voids**. Push risk is not
  confined to partial periods.
- `hockey/regulation` is three-way, `hockey/full_game` is two-way. Both are
  "moneyline, full contest" to a careless reader. Pairing them looks like a
  large edge on two fair prices.
- `soccer/full_game` prices a draw and can tie, so a 0 handicap pushes. A
  baseball full-game spread at 0 cannot.
- `Market.SPREAD` covers run line, puck line, point spread and Asian handicap;
  `sport` is what distinguishes them. Do **not** add `sport` to the arb
  market-group key: `event_key` is built from namespaced participant keys
  (`MLB-PHI`, `SOCCER-arsenal`), so two sports cannot collide through legitimate
  data, and a row whose `sport` is *mislabelled* must produce a counted refusal
  rather than silently drop out of its own group. `_examine_group` in
  `src/arb.py` enforces that.

## Arbitrage rules

`src/arb.py` is mostly about refusing to call something an arbitrage when it is
not, because the costly failure is a false positive.

- Pair only on a canonical line, from the home perspective. Home −1.5 against
  away +2.5 is a *middle*, which risks the stake to win more — deliberately not
  reported.
- Enumerate every settlement outcome and report the **minimum**. Push risk must
  be the number, not a footnote.
- Combine only identical contracts. Differing outcome counts are never paired.
- Round stakes to whole units, then recompute the guarantee. An edge thinner
  than one betting unit is a rejection, not free money.
- Count and explain refusals. "No opportunities" is meaningless without how many
  markets were genuinely comparable across books.

## Working agreements

- Two agents may work this repository concurrently — one on live fetching and
  source-specific parsing, one on everything else (schema, validation, replay,
  normalization, event matching, arbitrage, health, tests, integration). Check
  file mtimes before editing shared files; do not overwrite work in flight.
- Branch rather than committing to `main`.
- A new source adapter is finished when `tests/test_source_contract.py` passes
  with it registered — not when it returns rows. `docs/INPUT_CONTRACT.md` states
  what an adapter must deliver.
- Delete temporary experiments, abandoned approaches, duplicate implementations
  and dead code when done. No orphaned scaffolding.

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest -q                      # full suite
python -m src.collector collect          # one pass over all books
python -m src.collector replay           # re-parse the last run's stored bytes
python -m src.collector arb --verbose    # incl. why anything was refused
python -m src.collector health           # per-source success rate over time
python -m src.report --open              # dashboard over everything stored
```

Data lands under `data/` (gitignored): raw responses in `data/raw/`, normalized
rows in `data/collector.sqlite3`.
