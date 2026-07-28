# MLB Odds Collector

A local pipeline that continuously collects real MLB betting data from three
sportsbooks and normalizes it into one validated schema.

Every source is a **public endpoint of the sportsbook's own website**, fetched
directly with `httpx`. There are no third-party odds APIs, data vendors,
scraping services, proxies, hosted browsers, accounts, API keys, or paid tiers —
and nothing that could later require payment. The only dependencies are `httpx`
and `pydantic`; storage is stdlib `sqlite3`.

Nothing here logs in, defeats a CAPTCHA, or works around a geo-block or bot
protection. When a source returns a challenge, login page, or block, the run
records it as a failure for that source and moves on.

## Sources

| Source | Endpoint | Markets collected |
|---|---|---|
| `fanduel` | `sbapi.il.sportsbook.fanduel.com/api/content-managed-page` | full-game moneyline, run line, total runs |
| `pinnacle` | `guest.api.arcadia.pinnacle.com/0.1/leagues/246` | moneyline, run line, totals, team totals — full game, first 5 innings, first inning, incl. alternate lines |
| `betrivers_kambi` | `eu-offering-api.kambicdn.com/offering/v2018/rsiusil` | moneyline, run line, totals, team totals — full game, first 5 innings, first inning |

## Quick start

```bash
pip install -r requirements-dev.txt

python -m src.collector collect          # one pass over all three books
python -m src.collector runs             # recent runs + per-source health
python -m src.collector show --limit 20  # normalized rows from the last run
python -m src.collector replay           # re-parse the last run's stored bytes
python -m src.collector lines            # best price per market, across books
python -m src.collector arb              # arbitrage in the last run
python -m src.collector health           # per-source success rate over time
python -m src.report --open              # browsable dashboard of everything stored
python -m pytest tests/ -q               # incl. tests over real captured payloads
```

Arbitrage over a stored run, with the reasons anything was refused:

```bash
python -m src.collector arb --stake 500 --min-margin 0.5 --verbose
```

Continuous collection, unattended:

```bash
python -m src.collector collect --watch --interval 300
```

Collect from a subset, or skip the database while still storing raw responses:

```bash
python -m src.collector collect --source pinnacle --source fanduel
python -m src.collector collect --no-store
```

Everything lands under `data/` (gitignored): raw responses in `data/raw/`,
normalized rows in `data/collector.sqlite3`. Override with `MLB_DATA_DIR`,
`MLB_RAW_DIR`, `MLB_DB_PATH`, `MLB_INTERVAL_SECONDS`, `MLB_HTTP_TIMEOUT`.

## How a run works

```
fetch_raw() → store raw bytes → parse() → reconcile → validate() → arb → persist
   guards      data/raw/*.json   pure fn   event ids   graded      risk   sqlite
                                                       checks      free
```

Event identity is reconciled across **all** sources at once, before validation.
Each adapter can only number a doubleheader over its own slate, which makes `#2`
a per-source ordinal rather than an identity — so if one book lists both games
and another lists only the second, the second book's game 2 would otherwise be
joined onto the first book's game 1. `src/events.py` re-derives every key by
clustering start times globally.

Raw bytes are written to disk **before** anything interprets them, and `parse()`
is a pure function of those bytes with no I/O. Replay is therefore just `parse()`
over stored envelopes, and `collector replay` asserts a re-parse reproduces the
stored rows exactly.

A failing source does not abort a run. A run is `ok` only when validation passes
*and* at least two sportsbooks produced data.

## The schema

One row is one priced selection: `src/schema.py`, table `quote`.

`market`, `period`, and `selection` are **closed enums**. A source value that
cannot be mapped to one is counted as out of scope or rejected — never passed
through as free text, because a free-text fallback makes unnormalized data look
normalized.

- `market`: `moneyline`, `run_line`, `total_runs`, `team_total_runs`
- `period`: `full_game`, `first_5_innings`, `first_1_inning`
- `selection`: `home`, `away`, `draw`, `over`, `under`
- `line` is always from the selection's own perspective, so `home.line ==
  -away.line` on a run line and `over.line == under.line` on a total.
- `event_key` (`AWAY@HOME:YYYY-MM-DD` on the US/Eastern scheduling date, `#2`
  for the second game of a doubleheader) is the cross-source join key.
- `observed_at` is when *this collector* fetched the payload. A source's own
  clock never lands there; `last_change_at` holds the source-reported price
  change time when a source provides one.

## Validation

`src/validation.py` grades findings. `ERROR` means the data is wrong or unusable;
`WARNING` means suspicious. Beyond per-field checks it asserts things a single
row cannot know about itself:

- **Overround** — a complete market's implied probabilities must sum above 1.0.
  A book does not price itself to lose, so a sum below 1.0 means the parser
  mispaired prices or lines.
- **Line mirroring** — run-line sides must be opposites; both sides of a total
  must share a line.
- **Cross-source agreement** — books must agree on which team is home and on
  start time within 20 minutes.
- **Duplicates** — two different prices for one selection is an error (this is
  how pitcher-conditional moneylines leak in as the moneyline).
- **Core-market coverage** — if a book stops returning moneylines entirely, a
  label has probably been renamed upstream. Caught as an error rather than
  appearing as a quiet drop in row count.
- **Futures leakage** — a "game" starting more than 30 days out is a futures
  market that was parsed as a game.
- **Market-group homogeneity** — one `source_market_id` must identify one
  market. Several lines, both teams' totals, or two periods fused under one id
  makes every other market-level check read an arbitrary row, and the failure is
  silent because a fused group still looks complete.
- **Price encoding** — American and decimal odds are compared on *net payout*,
  not on the decimal price, so the tolerance means the same thing for a −5000
  favourite as for a +2400 longshot. An American value between −100 and +100 is
  not a price at all and is rejected.
- **Availability** — a source marking almost everything suspended contributes
  nothing downstream while its row count still looks healthy. Books on one slate
  do not disagree by 80 points on whether it is open, so that gap is reported.

Soft failures are detected explicitly in `src/sources/guards.py`: empty bodies,
CAPTCHA and bot challenges, login pages, blocks and geo-restrictions, non-JSON
responses, and changed payload shapes. Marker scanning only runs on bodies that
are not valid JSON, so a market legitimately named "please log in" is not
mistaken for a login page.

## Freshness

Repeated runs can legitimately return byte-identical payloads — an MLB market at
3am ET does not move. To keep that distinguishable from a stuck or cached feed,
each fetch is compared to the previous fetch of the same endpoint and reported
as `N/M payloads byte-identical to the previous run`. Response headers (`age`,
`x-cache`, `cache-control`) are stored alongside each payload for auditing, and
`last_change_at` carries the book's own view of when the price last moved.

## Dashboard

`python -m src.report` reads the SQLite store and writes one self-contained HTML
file — no server, no build step, no network access at all. Open it and you can see
what the collector grabbed and what it refused:

```bash
python -m src.report                  # writes data/dashboard.html
python -m src.report --open           # ... and opens it
python -m src.report --serve 8000     # serve it on localhost instead of file://
python -m src.report --quote-runs 20  # embed price rows for 20 runs, not 6
```

It is a click-through, not one long scroll. One panel is on screen at a time and
the rail switches between eleven of them: how to read a price at all and what the
four collected markets are; this run end to end (fetch → raw → parse → validate →
persist, with counts at each step); which sports two or more books priced;
per-source health; a coverage grid of every fixture against every book; a
filterable table of all normalized rows; price movement across consecutive runs;
validation findings and the overround distribution; the raw-capture ledger with
checksums; a glossary; and the field reference.

Three more panels are reached by clicking rather than from the rail, each opening
the thing that was clicked:

| Click | Opens |
|---|---|
| a sportsbook card, a skip row, a saved page | that **book** — what it published, what it left alone, every page saved from it |
| a fixture row | that **fixture** — every bet on it, with each book's price side by side |
| a bet row, anywhere it appears | that **bet** — every book's price, the other sides of the same market with the book's cut on that bet specifically, and the price at every collection |

Routing is through `location.hash`, not `history.pushState`, because pushState
throws on `file://` — where this page is usually opened. So every panel is
linkable, the back button works, and a breadcrumb trail says how deep you are
(*Fixtures › Guardians at Reds › Guardians win*). The detail panels are rendered
while still off screen: a link straight into one has to open on something, and a
panel filled only on arrival is a render path nothing exercises until a reader
finds it broken. `tests/dashboard_routes.mjs` visits every panel plus a real
fixture, book and bet key drawn from the page's own payload.

Nothing on the page is one dense block. Each panel is a stack of cards, each card
carries a two-line brief above its data saying what the block is and how to read
it, and the wide tables group their columns under band headings — *what the bet
is* / *what it pays* / *can you place it* — so twelve columns read as three things
rather than twelve. The band spans are derived from the column list itself,
because a hand-written `colspan` that drifts silently files every cell under the
wrong label; `tests/dashboard_smoke.mjs` checks the spans cover exactly the
headings present.

The copy is written for someone who has never placed a bet. Enum values are
translated (`spread` → "Winner with a handicap"), and every row leads with a
sentence rather than notation — `away +1.5` is shown as "Guardians win, or lose by
1", derived from the market, period, selection, line and side together. Whole-number
lines say where the push is: `-1.0` becomes "Reds win by 2 or more (a 1-run win
refunds)". Team names come from `src.teams`, so the page can say "Reds" without
inventing a club the validator would have rejected. The book's own shorthand stays
available as a tooltip on every row, and `#schema` keeps the untranslated field
list for querying the database directly.

Those sentences are claims about what a bet settles on, so 22 of them are asserted
in `tests/dashboard_smoke.mjs` — a wrong sentence is worse than notation, because a
reader has no way to tell it is wrong. Three of the 22 use the pre-v4 market names,
because the view normalizes `run_line`/`total_runs`/`team_total_runs` onto
`spread`/`total`/`team_total`: without that, a page built from an older database
would take the totals branch for a handicap and describe it as a total.

The report is strictly a view. It never fetches anything, and every number on the
page comes from a query in `src/report.py`, so generating it cannot change what
was collected. Runs are listed in full but price rows are only embedded for the
most recent `--quote-runs`; the page says so on any run it is not holding rows
for, rather than showing an empty table as if nothing had been collected.

## Repository layout

```
src/
  collector.py     pipeline + CLI
  schema.py        the one normalized schema (closed enums)
  validation.py    graded correctness checks
  arb.py           cross-book arbitrage, settlement modelling, stake sizing
  teams.py         strict MLB team canonicalization (30 clubs, no fuzzy guessing)
  events.py        event keys + global cross-source doubleheader reconciliation
  raw_store.py     raw-response envelopes and replay
  store.py         sqlite persistence
  normalize.py     odds conversions
  report.py        dashboard: queries the store, renders one standalone HTML file
  report_assets.py the dashboard's inline stylesheet, markup and script
  settings.py      local paths
  sources/
    base.py        the source protocol: fetch_raw() + pure parse()
    guards.py      empty / blocked / CAPTCHA / login / format-change detection
    fanduel.py  pinnacle.py  betrivers_kambi.py
docs/
  INPUT_CONTRACT.md   what a scraper must deliver, and current adapter gaps
tests/
  fixtures/raw/               real captured responses from a live 2026-07-28 run
  test_source_contract.py     the input contract, executable
  test_integration.py         whole pipeline end to end, incl. the CLI
  test_validation_adversarial.py   deliberately corrupted input
```

## Arbitrage

`src/arb.py` finds positions that cannot lose. The arithmetic is trivial; the
module is almost entirely about refusing to call something an arbitrage when it
is not, because the costly failure is a *false* positive.

- **Pairing is on a canonical line.** Run lines are stated per side, so home
  −1.5 and away +1.5 are grouped as one market while home −1.5 and away +2.5
  are not. That second pair is a *middle*, which risks the stake to win more —
  a different product, deliberately not reported here.
- **Every settlement outcome is enumerated,** and the reported profit is the
  minimum across them. So a whole-number line — where the game can land exactly
  on it and refund every leg — reports a floor of zero rather than its headline
  margin. Push risk cannot be overlooked because it *is* the number.
- **Only identical contracts are combined.** A two-way first-five-innings
  moneyline where a tie voids is not the same contract as a three-way one where
  a tie loses; pairing them looks like a 5% edge on two perfectly fair prices.
- **A two-way partial-period moneyline is refused outright.** A tie either voids
  both legs or loses both, and since adapters drop an unpriced draw leg silently,
  nothing on the row says which. The readings differ by the whole bankroll.
- **Stakes are rounded to whole units and the guarantee recomputed after.** An
  edge thinner than one betting unit is reported as rejected, not as free money.
- **Refusals are counted and explained.** "No opportunities" is only meaningful
  beside how many markets were genuinely comparable across books, so both are
  always reported.

On the captured slate the answer is **zero opportunities across 38 cross-book
markets** — three correctly-priced books, as expected. `tests/test_arb.py`
therefore uses clearly-labelled synthetic quotes for the positive cases; they are
never presented as observed prices.

## The scraper contract

`docs/INPUT_CONTRACT.md` states exactly what a source adapter must deliver, and
`tests/test_source_contract.py` enforces every mechanically checkable clause of
it against each registered adapter. A new adapter is finished when that file
passes with it registered — not when it returns rows.

## Scope

This collects and normalizes odds and identifies arbitrage in the collected
data. It does not place bets, serve a UI, or send alerts.
