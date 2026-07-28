# Multi-sport odds collector

A local pipeline that continuously collects real pregame betting data for **six
sports** from three sportsbooks and normalizes it into one validated schema.

Every source is a **public endpoint of the sportsbook's own website**, fetched
directly with `httpx`. There are no third-party odds APIs, data vendors, scraping
services, proxies, hosted browsers, accounts, API keys, or paid tiers — and
nothing that could later require payment. The only dependencies are `httpx` and
`pydantic`; storage is stdlib `sqlite3`.

Nothing here logs in, defeats a CAPTCHA, or works around a geo-block or bot
protection. When a source returns a challenge, login page, or block, the run
records it as a failure for that source and moves on.

## Sports and markets

Full-game pregame markets, plus hockey's regulation window and baseball's partial
periods.

| Sport | Leagues | Markets |
|---|---|---|
| Baseball | MLB | moneyline, run line, totals (full game, first 5 innings, first inning) |
| Basketball | WNBA, NBA¹ | moneyline, spread, totals |
| Hockey | NHL, club friendlies | moneyline, puck line, totals — **full game (incl. OT/shootout) and regulation (60 min, three-way)** |
| Football | NFL | moneyline, spread, totals |
| Tennis | ATP, WTA, Challenger, ITF | match winner |
| Soccer | EPL, MLS, La Liga, Serie A, Bundesliga, Ligue 1, + catch-all | three-way moneyline, handicap, totals (90 minutes) |

¹ Registered and fetched, but the NBA is in its offseason: it returns futures
containers only and produces no rows until October. See **Support status**.

## Sources

| Source | Endpoints |
|---|---|
| `fanduel` | `content-managed-page?page=CUSTOM&customPageId=…` per US league; `page=SPORT&eventTypeId=1\|2` for soccer/tennis; `event-page?eventId=…&tab=popular` for soccer detail |
| `pinnacle` | `guest.api.arcadia.pinnacle.com/0.1/{leagues/{id}\|sports/{id}}/{matchups,markets/straight}` |
| `betrivers_kambi` | `eu-offering-api.kambicdn.com/offering/v2018/rsiusil/{listView,betoffer}` |

## Quick start

```bash
pip install -r requirements-dev.txt

python -m src.collector collect                    # one pass over all books and sports
python -m src.collector collect --sport hockey     # or narrow it
python -m src.collector runs                       # recent runs + per-sport coverage
python -m src.collector show --limit 20            # normalized rows
python -m src.collector replay                     # re-parse the last run's stored bytes
python -m src.collector lines --cross-book-only    # best price per market, across books
python -m src.collector arb --verbose              # arbitrage, and why anything was refused
python -m src.collector health                     # per-source success rate over time
python -m src.report --open                        # browsable dashboard
python -m pytest -q                                # incl. tests over real captured payloads
```

Everything lands under `data/` (gitignored): raw responses in `data/raw/`,
normalized rows in `data/collector.sqlite3`. Override with `ODDS_DATA_DIR`,
`ODDS_RAW_DIR`, `ODDS_DB_PATH`, `ODDS_INTERVAL_SECONDS`, `ODDS_HTTP_TIMEOUT` (the
older `MLB_*` names still work and log a deprecation).

A database written by an earlier schema is **refused with instructions** rather
than silently written into; `python -m src.collector migrate` upgrades it, backing
up first and aborting on any row it cannot rebuild exactly.

## Support status

A sport is only *usable* when at least two books price the **same fixture**, and
that is a property of the calendar as much as of the code. Measured on a live run
on 2026-07-28:

| Sport | Rows | Fixtures | Priced by 2+ books | Books |
|---|---|---|---|---|
| baseball | 1,656 | 16 | **16** | all 3 |
| basketball | 844 | 7 | **7** | all 3 |
| football | 1,100 | 34 | **16** | all 3 |
| hockey | 217 | 9 | **5** | all 3 |
| soccer | 29,384 | 728 | **84** | all 3 |
| tennis | 1,042 | 312 | **148** | all 3 |

Two caveats stated plainly, because row counts alone would hide them:

- **Basketball is verified on the WNBA, not the NBA.** The NBA is out of season;
  its pages return futures containers and it contributes zero rows.
- **Pinnacle carries no NHL games in July.** Its NHL "moneylines" are outrights
  priced by `participantId`, and its only real hockey is club friendlies — which is
  where hockey's regulation window was verified. The five cross-book NHL fixtures
  are FanDuel and Kambi on September openers. Hockey's *market* logic is verified
  against live data on both windows; hockey on a mid-season NHL slate is not,
  because no such slate exists yet.

`collector runs` prints this table per run, including which configured leagues
returned nothing, so a gap is visible rather than indistinguishable from a league
nobody asked for.

## How a run works

```
fetch_raw() → store raw bytes → parse() → reconcile → validate → arb → persist
   guards      data/raw/*.json   pure fn   event ids   graded      risk   sqlite
                                                       checks      free
```

Raw bytes are written to disk **before** anything interprets them, and `parse()` is
a pure function of those bytes with no I/O. Replay is therefore just `parse()` over
stored envelopes — and `collector replay` asserts a re-parse reproduces the stored
rows exactly, reconciliation included.

Event identity is reconciled across **all** sources at once, before validation and
before any sport/league filter. Both orderings are load-bearing: each adapter can
only number a repeat fixture over its own slate, and clustering start times is
global, so scoping first would cluster a subset and produce different keys.

A failing source does not abort a run. A run is `ok` only when validation passes
*and* at least two sportsbooks produced data.

## The schema

One row is one priced selection: `src/schema.py`, table `quote`.

`sport`, `market`, `period` and `selection` are **closed enums** (`src/vocab.py`).
A source value that cannot be mapped is counted as out of scope or rejected —
never passed through as free text, because a free-text fallback makes unnormalized
data look normalized.

- `market`: `moneyline`, `spread`, `total`, `team_total` — sport-neutral, because a
  run line, a puck line, a point spread and an Asian handicap are one contract
  shape and `sport` already distinguishes them.
- `period`: `full_game`, `regulation`, `first_half`, `first_5_innings`,
  `first_1_inning`.
- `line` is stated from the selection's own perspective, so `home.line ==
  -away.line` on a spread and `over.line == under.line` on a total. Soccer and
  tennis use quarter lines legitimately; other sports land on half-points.
- `home_participant` / `away_participant` are the resolved identities everything
  joins on; `home_team` / `away_team` are display names.
- `event_key` is `AWAY@HOME:YYYY-MM-DD` built from participant keys
  (`MLB-PHI@MLB-MIA:2026-07-28`). **League is deliberately not in it** — books
  disagree about classification constantly, and that must not be able to break the
  join on exactly the events they all cover.
- `raw_ref` is the response the *price* came from; `identity_raw_ref` the one that
  supplied the participants, when those differ.

### Settlement is the part that matters

Two prices may only be compared when they are the same contract, and for a
moneyline that depends on facts no book states on the row: can this window end
level, and if so is the tie *priced* or does it **void** the bet?
`src/vocab.py::PERIOD_RULES` records both per `(sport, period)`:

| Window | Can tie | Draw priced | Consequence |
|---|---|---|---|
| baseball full game | no | no | complete 2-way |
| baseball first 5 / first inning | yes | yes | complete 3-way |
| basketball full game | no | no | complete 2-way |
| hockey **full game** (incl. OT + shootout) | no | no | complete 2-way |
| hockey **regulation** (60 min) | yes | yes | complete 3-way |
| football full game | **yes** | **no** | 2-way that **voids** on a tie |
| soccer full game (90 min) | yes | yes | complete 3-way |
| tennis match | no | no | complete 2-way |

Hockey's two windows are different contracts and both books sell both — Kambi as
`Puck Line - Including Overtime and Penalty Shootout` versus `Puck Line - Regular
Time`, Pinnacle as period 0 versus period 6. Pairing one against the other looks
like a large edge on two perfectly fair prices. Because `period` is part of
`dedup_key`, `market_key` and arbitrage pairing, that mistake is structurally
impossible rather than a rule someone has to remember. A `(sport, period)` pair
absent from the table is refused outright: a default would be a guess about
whether a tie voids the bet, and that guess is worth the whole stake.

## Participant identity

`src/participants.py`, with rosters in `src/rosters.py`. Two regimes:

- **Closed rosters** (MLB, NBA, WNBA, NHL, NFL) resolve strictly or not at all,
  and resolution is **per league**. A global index would make "Rangers" mean both
  Texas and New York, "Panthers" both Carolina and Florida, "Kings" both Los
  Angeles and Sacramento, and "Jets" both New York and Winnipeg.
- **Open rosters** (soccer clubs, tennis players) use deterministic normalization,
  biased so that a *missed* match is possible and a *false* one is not. Reserve,
  age-group and women's markers are kept, so "FK Transinvest II" never resolves to
  "FK Transinvest" and "Freiburg (W)" never to "Freiburg". Doubles pairings are
  explicitly unresolvable.

## Validation

`src/validation.py` grades findings. `ERROR` means the data is wrong or unusable;
`WARNING` means suspicious. Every threshold that differs by sport is looked up
rather than hardcoded, because each global constant was actively wrong for at least
one sport: a 30-day futures horizon rejected every real NFL and NHL game, and a
20-minute start-time tolerance split nearly every tennis match in two.

Beyond per-field checks it asserts what a single row cannot know: overround over a
*sport-correct* number of legs, line mirroring, cross-source agreement on the
participants (and on home/away only where that is a fact), per-league total
plausibility, duplicates, market-group homogeneity, price encoding compared on
**net payout**, and availability.

Clustering fixtures and reporting a bad clock are deliberately separate
thresholds. Clustering is generous, because splitting a fixture loses the join on
exactly the events two books share; reporting is strict, because a three-hour
disagreement about a kickoff is a real defect even once the rows have joined
correctly.

## Arbitrage

`src/arb.py` finds positions that cannot lose. The arithmetic is trivial; the
module is almost entirely about refusing to call something an arbitrage when it is
not, because the costly failure is a *false* positive.

- **Every settlement outcome is enumerated,** and the reported profit is the
  minimum across them — so a whole-number line reports a floor of zero rather than
  its headline margin, an NFL full-game moneyline carries its tie-voids outcome,
  and a quarter line's half-push is modelled.
- **Only identical contracts are combined.** Never across sport, period, market, or
  line. A two-way market in a window where the draw is priced is *incomplete*, not
  a two-way contract, and is refused as `ambiguous_tie_settlement`.
- **Stakes are rounded to whole units and the guarantee recomputed after.**
- **Refusals are counted and explained,** alongside how many markets were genuinely
  comparable — "no opportunities" is only meaningful beside that number.

On a live 2026-07-28 slate the answer is **zero opportunities from 953 cross-book
markets** — three correctly-priced books, as expected. `tests/test_arb.py`
therefore uses clearly-labelled synthetic quotes for the positive cases; they are
never presented as observed prices.

## Dashboard

`python -m src.report` reads the SQLite store and writes one self-contained HTML
file — no server, no build step, no network access at all. It is strictly a view:
every number comes from a query in `src/report.py`, so generating it cannot change
what was collected. It shows the run end to end, per-source health, a per-sport and
per-book coverage grid with the two-book bar, every normalized row, price movement
across runs, validation findings, the raw-capture ledger, and the schema itself.

## Repository layout

```
src/
  collector.py     pipeline + CLI
  vocab.py         closed enums + the per-(sport, period) settlement facts
  leagues.py       league registry: calendars, horizons, tolerances, total ranges
  rosters.py       closed membership lists, written from live payloads
  participants.py  participant identity, per league
  schema.py        the one normalized row
  events.py        event keys + global cross-source reconciliation
  validation.py    graded, sport-aware correctness checks
  arb.py           cross-book arbitrage, settlement modelling, stake sizing
  raw_store.py     raw-response envelopes and replay
  store.py         sqlite persistence + versioned migration
  normalize.py     odds conversions
  report.py        dashboard: queries the store, renders one standalone HTML file
  sources/
    base.py        the source protocol: fetch_raw() + pure parse()
    guards.py      empty / blocked / CAPTCHA / login / format-change detection
    fanduel.py  pinnacle.py  betrivers_kambi.py
docs/
  INPUT_CONTRACT.md   what a scraper must deliver, executable as a test
tests/
  fixtures/raw/               real captured responses, all six sports
  test_source_contract.py     the input contract, enforced per adapter
  test_fixture_replay.py      the whole slate replayed offline, cross-book
  test_integration.py         pipeline end to end, incl. the CLI
  test_validation_adversarial.py   deliberately corrupted input
```

## Scope

This collects and normalizes odds and identifies arbitrage in the collected data.
It does not place bets, serve a UI, or send alerts.
