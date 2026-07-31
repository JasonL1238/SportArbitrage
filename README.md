# Multi-sport odds collector

A local pipeline that continuously collects real pregame betting data for **six
sports** from **eleven venues** — six sportsbooks, three betting exchanges and two
prediction markets — and normalizes it into one validated schema.

Every source is a **public endpoint of the venue's own website**. The goal is
coverage: scrape whatever answers. The default HTTP stack is **Chrome TLS
impersonation** via `curl_cffi` (not plain `httpx`), which is what opens Akamai /
CloudFront / Cloudflare edges that reject a stock Python client. Set
`ODDS_HTTP_PROXY` (or `HTTPS_PROXY`) when an exit IP is still geo-blocked.
`httpx` remains for tests and injectable mocks; storage is stdlib `sqlite3`.

A block is a transport problem, not a stop sign. Rate limits are still paced so
a run does not burn the exit node; geo and bot walls are routed around with the
strongest client fingerprint (and proxy) available.

**A source is a counterparty, not a hostname.** Kambi fronts BetRivers, LeoVegas
and a dozen others from one CDN, and most of those tenants answer with
byte-identical prices — licences of one book. Counting two of them as two sources
would let the engine report an "arbitrage" between BetRivers and BetRivers.
Distinctness is therefore *measured* against a live slate (`src/distinctness.py`)
before a source is registered, and the rejected tenants are recorded with the
source they mirror in [`docs/SOURCE_FEASIBILITY.md`](docs/SOURCE_FEASIBILITY.md).

It is measured **per competition**, because that is what the data turned out to
be. BetRivers and LeoVegas agree on 64% of prices overall — comfortably distinct
by any single threshold — and underneath that number they agree on *every* ITF,
ATP, WTA and Bundesliga price on the slate while disagreeing about baseball,
hockey and most soccer. They are one price feed for all of tennis and two books
for everything else. A single rate over everything averages that away, so each
competition is judged on its own and the pair is treated as one counterparty in
the leagues where it is one, and nowhere else. Removing either source instead
would throw away real coverage the other does not have.

## Sports and markets

Full-game pregame markets, plus hockey's regulation window and baseball's partial
periods.

| Sport | Leagues | Markets |
|---|---|---|
| Baseball | MLB | moneyline, run line, totals, team totals (full game, first 5 innings, first inning) |
| Basketball | WNBA, NBA¹ | moneyline, spread, totals, team totals |
| Hockey | NHL, club friendlies | moneyline, puck line, totals — **full game (incl. OT/shootout) and regulation (60 min, three-way)** |
| Football | NFL | moneyline, spread, totals, team totals |
| Tennis | ATP, WTA, Challenger, ITF | match winner |
| Soccer | EPL, MLS, La Liga, Serie A, Bundesliga, Ligue 1, + catch-all | three-way moneyline, handicap, totals, team totals (90 minutes) |

¹ Registered and fetched, but the NBA is in its offseason: it returns futures
containers only and produces no rows until October. See **Support status**.

## Sources

Eleven registered venues, of three kinds. The kind is not decoration: a sportsbook
posts a price it will take the other side of, an exchange shows you somebody
else's order with a size and a commission, and a prediction market shows a
contract price with an entry fee. The arbitrage engine prices all three
**net of what the venue charges** (`src/commission.py`) — exchanges quote tighter
than books, so their legs are exactly the ones that would otherwise produce a
stream of false positives.

The kind also decides what happens when the game is not played. A book voids and
refunds; Kalshi keeps the market open through a postponement and settles from the
make-up game, or resolves a cancellation at a price it chooses; Polymarket
resolves every contract at 0.50 whatever you paid. Two legs that do not void
together are not a hedge, so `src/settlement.py` records each venue's regime —
the registry refuses a source that declares none — and every position spanning
two of them says so. It is not a small caveat: on a rained-out game the hedge
disappears and what is left is a one-sided bet for the whole stake.

| Source | Kind | Endpoints |
|---|---|---|
| `fanduel` | sportsbook | `content-managed-page?page=CUSTOM&customPageId=…` per US league; `page=SPORT&eventTypeId=1\|2` for soccer/tennis; `event-page?eventId=…&tab=popular` for soccer detail |
| `pinnacle` | sportsbook | `guest.api.arcadia.pinnacle.com/0.1/{leagues/{id}\|sports/{id}}/{matchups,markets/straight}` |
| `betrivers_kambi` | sportsbook | `eu-offering-api.kambicdn.com/offering/v2018/rsiusil/{listView,betoffer}` |
| `leovegas_kambi` | sportsbook | the same Kambi API under operator `leo` — a different book on one platform, [verified distinct](docs/SOURCE_FEASIBILITY.md) |
| `bovada` | sportsbook | `www.bovada.lv/services/sports/event/coupon/events/A/description/{path}` — one request per league, states `competitors[].home` |
| `betmgm` | sportsbook | `www.il.betmgm.com/cds-api/bettingoffer/fixtures` — public `x-bwin-accessid`, one paged request per sport |
| `matchbook` | exchange | `www.matchbook.com/edge/rest/{navigation,events}` — moneyline, totals and handicaps in one call, **with the money behind each price** |
| `smarkets` | exchange | `api.smarkets.com/v3/{events,markets,contracts,quotes}` — fully typed; moneyline only, within the venue's 20/min limit |
| `sxbet` | exchange | `api.sx.bet/{markets/active,orders}` — a resting order book; every price is one counterparty's offer, with its own size |
| `kalshi` | prediction market | `api.elections.kalshi.com/trade-api/v2/markets` — MLB series, structured strikes and tickers |
| `polymarket` | prediction market | `gamma-api.polymarket.com/events` — structured `sportsMarketType`, `line` and `teams` |

Four of them publish **how much money is behind a price**: a top-of-book price
with $40 behind it is not an arbitrage at a $500 stake, so it caps the position
rather than being ignored. On the captured slate Kalshi, Matchbook and SX Bet
state a size on every row, and Pinnacle states `maxRiskStake` on every row too —
so this is not, as this sentence used to say, a fact no sportsbook states.

### Request budget

`--tier core` asks each source only for the endpoints that return a whole league
at once — a few requests per source, safe on a short interval. `--tier full`
adds the per-event follow-ups, which for FanDuel alone is 126 of its 133 requests.
The tier is a *request budget*, not a market filter: a source that genuinely
collects less under `core` narrows what it **claims** to price, so a market
missing because it was not asked for is never reported as a market that vanished.

## Quick start

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium              # for ODDS_FETCH_MODE=browser

python -m src.collector collect                    # one pass over all venues and sports
python -m src.collector collect --tier core        # slate endpoints only — a few requests per source
python scripts/probe_sources.py                    # curl_cffi Chrome impersonation
python scripts/probe_sources.py --browser          # Playwright Chromium
python scripts/probe_sources.py --only blocked     # DK / Caesars / Fanatics / bet365
# ODDS_HTTP_PROXY=http://user:pass@host:port       # residential exit in a licensed state
# ODDS_FETCH_MODE=browser                          # force Playwright for the collector
python -m src.collector collect --sport hockey     # or narrow it
python -m src.collector runs                       # recent runs + per-sport coverage
python -m src.collector show --limit 20            # normalized rows
python -m src.collector replay                     # re-parse the last run's stored bytes
python -m src.collector lines --cross-book-only    # best price per market, across books
python -m src.collector arb --verbose              # arbitrage, and why anything was refused
python -m src.collector health                     # per-source success rate over time
python -m src.report --open                        # browsable dashboard (view only)
python -m src.report --serve 8765 --open           # dashboard + Scrape button on localhost
python -m pytest -q                                # incl. tests over real captured payloads
```

Open the dashboard with **`--serve`** if you want the **Scrape now** button. A plain
`file://` open stays view-only on purpose: the page cannot run the collector
without a local process. After each scrape the page reloads on the newest
snapshot; the left rail lists every collection by time so you can flip between
them (or click bars on **Price changes**).

Everything lands under `data/` (gitignored): raw responses in `data/raw/`,
normalized rows in `data/collector.sqlite3`. Override with `ODDS_DATA_DIR`,
`ODDS_RAW_DIR`, `ODDS_DB_PATH`, `ODDS_INTERVAL_SECONDS`, `ODDS_HTTP_TIMEOUT` (the
older `MLB_*` names still work and log a deprecation).

A database written by an earlier schema is **refused with instructions** rather
than silently written into; `python -m src.collector migrate` upgrades it, backing
up first and aborting on any row it cannot rebuild exactly.

## Support status

A sport is only *usable* when at least two venues price the **same fixture**, and
that is a property of the calendar as much as of the code. Measured on a live
`--tier core` run on 2026-07-28, with nine of the ten sources answering (Pinnacle
was in vendor maintenance and reported as failed, which is what that looks like):

| Sport | Rows | Fixtures | Priced by 2+ venues | Venues |
|---|---|---|---|---|
| baseball | 3,186 | 76 | **16** | 9 |
| basketball | 1,773 | 42 | **7** | 8 |
| football | 616 | 82 | **17** | 6 |
| hockey | 336 | 7 | **7** | 3 |
| soccer | 7,319 | 380 | **109** | 7 |
| tennis | 972 | 201 | **136** | 5 |

That run produced **1,683 cross-book markets** against 331 from the original
three books, and five risk-free positions where three books found none — every
one of them priced net of the venue's commission and capped by the money actually
resting behind the price. Validation passed with no errors and `replay` reproduced
the run byte-for-byte.

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
*and* at least two **venues** produced data — any two, not two sportsbooks. Two
exchanges can be compared with each other, so refusing that would throw away a
usable slate; what the run does say, as a warning, is when *no* sportsbook
produced at all, because a posted price and a resting order are different things.

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
| baseball first 1 inning | **yes** | yes | complete 3-way |
| baseball first 5 innings | **yes** | yes | complete 3-way |
| baseball full game | no | no | complete 2-way |
| basketball first half | **yes** | yes | complete 3-way |
| basketball full game | no | no | complete 2-way |
| basketball regulation | **yes** | yes | complete 3-way |
| football first half | **yes** | yes | complete 3-way |
| football full game | **yes** | **no** | 2-way that **voids** on a tie |
| football regulation | **yes** | yes | complete 3-way |
| hockey full game | no | no | complete 2-way |
| hockey regulation | **yes** | yes | complete 3-way |
| soccer first half | **yes** | yes | complete 3-way |
| soccer full game | **yes** | yes | complete 3-way |
| tennis full game | no | no | complete 2-way |

Hockey's two windows are different contracts and both books sell both — Kambi as
`Puck Line - Including Overtime and Penalty Shootout` versus `Puck Line - Regular
Time`, Pinnacle as period 0 versus period 6. Pairing one against the other looks
like a large edge on two perfectly fair prices. Because `period` is part of
`dedup_key`, `market_key` and arbitrage pairing, that mistake is structurally
impossible rather than a rule someone has to remember. The table above *is*
`PERIOD_RULES` — every pair the pipeline can collect is in it, and a pair absent
from `PERIOD_RULES` has no settlement rule, so nothing downstream can price it:
a default would be a guess about whether a tie voids the bet, and that guess is
worth the whole stake.

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

Three of those cross-source checks exist because a source can be *internally
consistent and wrong*, which no per-row rule can see:

- **Which participant is home.** Grouped on the unordered pair, not on the event
  key — the key *is* `away@home:date`, so a reversed source used to form a group
  of its own with nobody to disagree with. The check ran on every run and could
  never fire; a fully reversed book passed with zero errors while losing 103 of
  its 133 shared fixtures.
- **Which competitor the handicap favours.** A sign flip mirrors within a
  source's own market perfectly, so nothing caught it: flipping one source's
  spreads produced 185 phantom positions and no finding at all.
- **What a bet is worth.** The general net beneath the other two. Honest sources
  never sit more than 0.083 of implied probability from a consensus of three; a
  source whose prices are attached to the wrong side sits far outside that, and
  this is the only check that sees it when the participants are correct.

Each is graded on a *rate* rather than an occurrence, and per sport as well as
overall — a source reversed in one sport is 100% wrong there and a few per cent
wrong on average, and the average is what hid it.

**A price is only comparable with prices collected near it in time.** Two legs
more than `MAX_OBSERVATION_SPREAD` apart are not a position anyone can take, and
the detector refuses them — correctly, and silently. That silence hid something
worth knowing: a source paced to its own published rate limit can take minutes to
finish, and *everything collected after it* inherits that distance. Smarkets sits
at 3.1 s per request and runs about eight minutes; from the middle of the
collection order it pushed Kalshi and Polymarket — four and ten requests each —
past the window, and **847 of their shared markets became uncomparable for no
reason but list position**. Slow sources now collect last, and a source that ends
up outside the window says so rather than contributing coverage the detector
cannot use.

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
- **Nor across venues that settle differently.** A book refunds a cancelled game;
  Kalshi settles the make-up game; Polymarket resolves every contract at 0.50.
  Two legs that do not void together are not a hedge — on a rained-out game the
  hedge vanishes and one leg is a naked bet for its whole stake — so the position
  is refused as `legs_do_not_void_together` rather than reported with a caveat.
- **Nor between two keys belonging to one counterparty.** Distinct *sources* is
  not the same test as distinct *counterparties*; the per-league mirror
  measurement above feeds straight into this, so a pair that is one price feed in
  tennis cannot be arbitraged against itself there.
- **Stakes are rounded to whole units and the guarantee recomputed after.**
- **Prices have to still exist.** Every other freshness check is *relative* — two
  legs captured six weeks ago are seconds apart and pass — and a fixture weeks out
  is still in the future, so the started-game gate does not catch a stale run
  either. The read commands refuse a run older than `MAX_PRICE_AGE` unless it is
  asked for by id, and print its age either way.
- **Refusals are counted and explained,** alongside how many markets were genuinely
  comparable — "no opportunities" is only meaningful beside that number.

On a live 2026-07-28 slate at ten sources the answer is **a handful of positions
from ~2,460 cross-book markets**, all of them thin and most of them refused by
stake rounding. `tests/test_arb.py` uses clearly-labelled synthetic quotes for the
positive cases; they are never presented as observed prices.

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
  commission.py    what each venue takes out of a winning bet
  settlement.py    what each venue does with a game that is not played
  distinctness.py  measures whether two sources are one counterparty
  raw_store.py     raw-response envelopes and replay
  store.py         sqlite persistence + versioned migration
  normalize.py     odds conversions
  report.py        dashboard: queries the store, renders one standalone HTML file
  report_assets.py the dashboard front end — CSS and JS, inlined into that file
  sources/
    base.py        the source protocol: fetch_raw() + pure parse()
    guards.py      empty / blocked / CAPTCHA / login / format-change detection
    registry.py    which sources exist, and how to build one
    _common.py     shared fetch layer: pacing, retries, capture, scope tallies
    fanduel.py  pinnacle.py  betrivers_kambi.py  bovada.py  betmgm.py
    matchbook.py  smarkets.py  sxbet.py  kalshi.py  polymarket.py
docs/
  INPUT_CONTRACT.md      what a scraper must deliver, executable as a test
  SOURCE_FEASIBILITY.md  what each venue actually serves, and what was measured
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
