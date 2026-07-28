# Source input contract

What a scraper must deliver for the downstream pipeline — validation, event
matching, arbitrage detection, storage, replay — to be correct.

This contract exists because the downstream failure mode is not a crash. It is a
**phantom arbitrage**: two prices that are not opposite sides of the same
contract, joined into a position that looks risk-free and is not. Every clause
below traces to a way that can happen.

The contract is executable. `tests/test_source_contract.py` asserts every
mechanically checkable clause against each registered adapter's output from
captured payloads. A new adapter is done when that file passes with it
registered — not when it returns rows.

---

## 1. The interface

Implement `src.sources.base.BaseballSource`:

```python
source_key: str                                  # stable, lowercase, e.g. "pinnacle"
fetch_raw() -> list[RawResponse]                 # network; raises SourceError on trouble
parse(raws: Sequence[RawResponse]) -> ParseOutcome   # pure; no I/O, no clock, no network
close() -> None
```

**`parse` must be a pure function of the bytes it is given.** No network, no
filesystem, no `datetime.now()`, no randomness, no mutable adapter state that
survives a call. Replay, regression testing, and reproducing a live failure
offline all depend only on this. `tests/test_source_contract.py` asserts that
parsing the same input twice yields identical rows.

**`fetch_raw` must raise rather than return nothing.** A `SourceError` subclass
(`BlockedError`, `CaptchaError`, `NotJsonError`, `FormatChangeError`,
`EmptyResponseError`, …) distinguishes "blocked" from "format changed" from
"genuinely no games". Returning an empty list makes a Cloudflare interstitial
indistinguishable from an off day, and the collector records it as healthy.

**`parse` receives exactly one collection run's responses.** Handed two runs, an
adapter that iterates every raw emits every row twice at different prices —
duplicate identities at conflicting prices. If an endpoint can appear more than
once, select the latest by `fetched_at` rather than iterating all of them.

### Endpoint labels

`RawResponse.endpoint` is part of the stored filename and of every row's
`raw_ref`, so the labels are load-bearing and must be stable across runs.
Declare them as module constants. Where the label carries an index
(`betoffer-batch-01`), that index is a position counter and is **not** stable
across runs when the slate size changes — never derive identity from it.

---

## 2. What every row must satisfy

One row = one priced selection, at one line, from one book, at one observation.
Emit `BaseballQuote` and nothing else; `src/schema.py` is the only vocabulary.

### Identity

| Field | Requirement |
|---|---|
| `source` | Equals `source_key`. Non-blank. |
| `source_event_id` | The book's own event id. Must distinguish two games of a doubleheader. |
| `home_team`, `away_team` | Must resolve via `src.teams.canonical_team`. Emit the canonical `Team.name`. Never a raw feed string, never a matchup string, never a market label. |
| `commence_time` | Timezone-aware, UTC. Scheduled first pitch. |
| `event_key` | Build with `src.events.build_event_key(away.abbr, home.abbr, commence_time)`. |

**Do not rely on `event_key` for doubleheader identity.** An adapter can only
number the games it happens to see, so `#2` is a per-source ordinal. The
pipeline re-derives every key globally in `src.events.reconcile_event_keys`,
clustering start times across all sources at once. Your job is to emit an
accurate `commence_time` and a `source_event_id` that separates the two games;
identity is settled downstream.

**`commence_time` is not a joinable field.** Books disagree by a minute or two
routinely (in the captured slate FanDuel is one minute later than the other two
on all 16 events). Never require exact equality across sources.

### Market vocabulary — closed enums

`market`, `period`, `selection`, `side` are closed. **A source value that cannot
be mapped is a rejection, never a passthrough.** Free text that reaches these
fields makes unnormalized data look normalized, which is the specific failure the
schema exists to prevent — `american_league_cy_young_2026` as a market type is
what this rule is about.

- `line` — required for `run_line`, `total_runs`, `team_total_runs`; forbidden
  for `moneyline`. Stated **from the row's own selection's perspective**: home
  −1.5 and away +1.5 are the two halves of one market. A two-sided market must
  satisfy `home.line == -away.line` for run lines and `over.line == under.line`
  for totals.
- `side` — required for and only for `team_total_runs`.
- `selection` — must be legal for the market. `draw` only on partial periods; a
  full-game baseball moneyline cannot push.
- Scale lines and odds out of source units. Kambi sends both in thousandths; a
  total of 8.0 arriving as `8000` is a units error, not a line.

### Price

All three of `decimal_odds`, `american_odds`, `implied_probability` must be
present and mutually consistent:

- `implied_probability == 1 / decimal_odds` (to 1e-6).
- `american_odds` and `decimal_odds` must agree on **net payout** to within 1%.
- `1.01 <= decimal_odds <= 1000.0`.
- `american_odds` must be `<= -100` or `>= +100`. Values in between are not
  prices. `-50` converts to a plausible-looking 3.00 and is undetectable
  downstream, so `src.normalize.american_to_decimal` now refuses it.

Convert with `src.normalize`; do not hand-roll the arithmetic.

> Note: when an adapter derives one format from the other, the cross-check
> becomes a tautology and cannot detect a feed error. Prefer emitting the
> **feed's own** value for each format when the feed supplies both.

### Provenance

| Field | Requirement |
|---|---|
| `observed_at` | Always `raw.fetched_at` of the response **the price came from**. Never a source-supplied clock — a `cutoffAt` is when betting closes, in the future. |
| `raw_ref` | `raw.ref` of the response the price was parsed from. Must point at a response the collector actually stored. |
| `last_change_at` | Source-reported price-change time, if the feed gives one. Must not be after `observed_at`. Never substitute for `observed_at`. |

**Do not assume one run means one `observed_at`.** A batched adapter has one per
batch. Group by run id, not by timestamp.

### Availability and limits

- `status` — `ACTIVE` only when the price is genuinely takeable. This drives
  arbitrage: suspended rows are excluded, so an over-suspending adapter silently
  contributes nothing while its row count still looks healthy. Validation now
  reports `implausible_suspension_rate` when one source suspends over 80% of its
  rows while another on the same slate suspends far fewer.
  **Verify the field's type before coercing it.** A timestamp string is truthy.
- `limit_amount` — the book's stated maximum stake, when published. Used to cap
  a position's bankroll. Leave `None` if unknown; `None` means unknown, not
  unlimited.
- `is_alternate` — `True` for a non-primary line. This must be set correctly
  when the book offers alternates, because a primary and an alternate at the
  same number are two real, distinct offers and `dedup_key` separates them on
  this flag. An adapter that leaves it `False` on every row makes the primary
  line unidentifiable.

### The tie, and why it matters most

**A market's outcome count is not recoverable from a row.** Downstream must
infer 2-way vs 3-way by grouping and counting. That inference is only sound if
adapters never drop a leg:

> **An in-scope market must be emitted whole.** If a priced selection arrives
> without a price, emit it as `SUSPENDED` or reject the whole market — do not
> silently skip the leg.

Skipping it makes a 3-way market byte-identical to a 2-way one. The two settle
differently in the only outcome that distinguishes them: a tie **voids** both
legs of a 2-way market and **loses** both legs against a 3-way one's draw. That
is the difference between a floor of zero and a floor of minus the entire
bankroll, and no field on the row can tell them apart.

Because adapters currently do drop unpriced legs, `src/arb.py` refuses to report
any position on a two-way partial-period moneyline at all
(`ambiguous_tie_settlement`). Satisfying the clause above is what would make
those markets usable.

### Skips and rejections

- **Rejection** = the source offered something in scope that could not be
  represented faithfully. A failure. Counted against health.
- **Skip** = deliberately out of scope (player props, futures, pitcher-conditional
  markets). Expected, not an error.

**Every dropped record must land in one bucket or the other.** A bare `continue`
is a contract violation: it makes "we collected everything" unfalsifiable.
Rejections must carry a stable machine-readable `reason` so a format change shows
up as a spike rather than as silence.

Pitcher-conditional moneylines ("Match Odds (X must start)") are a **different
market** from the moneyline and must be skipped, not merged — collecting both
puts conflicting prices on one selection.

---

## 3. Hard invariants

Violating any of these breaks the pipeline rather than degrading it.

1. **No two rows share a `dedup_key`.** `(source, event_key, market, period,
   side, selection, line, is_alternate)`. Storage enforces this with a UNIQUE
   constraint; a collision aborts the insert.
2. **`market_key` identifies exactly one market.** `(source, source_event_id,
   source_market_id)` must not span two markets. Several lines, both teams'
   totals, or two periods under one id fuses distinct markets into one group and
   every market-level check then reads an arbitrary row.
   *`source_market_id` need not be globally unique* — Pinnacle's `s;0;m` is the
   moneyline id for every game — but it must be unique **within an event**, and
   it must distinguish alternates from the primary line.
3. **Two-sided markets mirror.** `home.line == -away.line`; `over.line ==
   under.line`.
4. **A book never prices itself to lose.** Implied probabilities across one
   complete market at one book sum to ≥ 1.0. Below that means mispaired prices.
5. **`observed_at` is a local clock reading, never the source's.**
6. **Parsing is deterministic and side-effect free.**

---

## 4. Current adapter gaps

Audited against the captured 2026-07-28 slate. These are on the scraping side; I
have not changed adapter code. Each one is currently absorbed or flagged
downstream, and the note says how.

| # | Source | Issue | Downstream effect |
|---|---|---|---|
| 1 | betrivers_kambi | ~~`status` derived as `bool(offer["closed"])` where `closed` is a timestamp string — 739 of 755 rows marked suspended.~~ **Fixed on the scraping side; all 755 rows now active.** Validation retains `implausible_suspension_rate` as a regression guard. | Resolved. BetRivers now contributes, taking comparable cross-book markets from 38 to 220. |
| 2 | betrivers_kambi | `is_alternate` is never set — 755/755 rows are `False`. Up to 13 distinct total lines per event all arrive as primary. The `MAIN_LINE` tag in `offer["tags"]` marks the primary on exactly one offer per group (39/39) and is unread. **Still open.** | The primary line cannot be identified from a row. Arbitrage groups by line so pairing stays correct, but `dedup_key` cannot separate a primary from an alternate at the same number, and any "main line only" view is wrong. |
| 3 | all | An unpriced selection is skipped silently (`runner_without_price`, `price_without_odds`, `outcome_without_odds`). | A 3-way market missing its draw is indistinguishable from a 2-way one. Arbitrage refuses two-way partial-period moneylines outright as a result. |
| 4 | fanduel, pinnacle | Markets on out-of-scope events are dropped with a bare `continue` and no counter — 70 of 118 FanDuel markets, and every market on Pinnacle's 521 special matchups. | Coverage monitoring understates what was discarded. |
| 5 | pinnacle | `raw_ref` always points at `markets-straight`; the `matchups` response that supplied event identity is never referenced. | Identity provenance is not traceable from a row. |
| 6 | pinnacle | The `source_market_id` fallback omits `side`, `points` and `isAlternate`. Unexercised today (no market lacks `key`). | Would fuse both teams' totals and every alternate line into one `market_key`. Invariant 2. |
| 7 | fanduel | `ALTERNATE_MONEY_LINE` would map to the same `market`/`period` as the primary with `line=None`. Not currently emitted. | Would collide on `dedup_key` and abort the run's insert. Invariant 1. |
| 8 | fanduel, betrivers_kambi | `parse` iterates every raw given to it rather than selecting the latest per endpoint. | Replaying a directory holding two runs duplicates every row. |
| 9 | all | `Period.FIRST_3_INNINGS` is emitted by nobody; Kambi offers it and skips it. Pinnacle period 2 would be a rejection. | Coverage gap only. |

---

## 5. Verifying an adapter

```bash
python3 -m pytest tests/test_source_contract.py -q     # the contract itself
python3 -m pytest -q                                   # everything
python3 -m src.collector collect --source <key>        # one live run
python3 -m src.collector replay                        # re-parse stored bytes
python3 -m src.collector arb --verbose                 # what was comparable, and why not
```

A source is integrated when `collect` reports it healthy, `replay` passes, and
`arb` counts its markets in `comparable_group_count` — not merely when it returns
rows.
