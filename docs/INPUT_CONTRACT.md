# Source input contract

What a scraper must deliver for the downstream pipeline — validation, event
matching, arbitrage detection, storage, replay — to be correct.

This contract exists because the downstream failure mode is not a crash. It is a
**phantom arbitrage**: two prices that are not opposite sides of the same
contract, joined into a position that looks risk-free and is not. Every clause
below traces to a way that can happen.

The contract is executable. `tests/test_source_contract.py` asserts every
mechanically checkable clause against each registered adapter's output from
captured payloads. A new adapter, or a new sport on an existing one, is done when
that file passes — not when it returns rows.

---

## 1. The interface

Implement `src.sources.base.OddsSource`:

```python
__init__(..., *, source_key: str = SOURCE_KEY)       # the identity this instance's rows carry
source_key: str                                      # stable, lowercase, e.g. "pinnacle"
leagues: tuple[str, ...]                             # canonical league keys this instance collects
capabilities(*, tier: Tier) -> Mapping[str, frozenset[Market]]   # what it claims to price
fetch_raw(*, tier: Tier) -> list[RawResponse]        # network; raises SourceError on trouble
parse(raws: Sequence[RawResponse]) -> ParseOutcome   # pure; no I/O, no clock, no network
close() -> None
```

One adapter per **venue shape**, not per book-and-sport and not per book. The
adapter owns the HTTP session, the raw-capture conventions and the parsing rules,
and is parameterized by which leagues to collect *and by which instance it is*.
Splitting per sport would duplicate all of that six times over; splitting per
*book* would duplicate it once per tenant of a platform, and Kambi alone fronts a
dozen books from one API.

**An instance is identified by `source_key`, and rows read it off the envelope.**
`source` is the first element of `dedup_key`, so two instances of one class must
emit different values for it or they collide on the storage layer's UNIQUE
constraint and abort that source's insert. `parse` takes it from
`RawResponse.source` (see `src.sources._common.envelope_source`) and **never**
from a module constant or from `self` — because replay constructs the adapter
with no arguments at all, and would otherwise file one tenant's prices under
another.

**A source must be *distinct*, not merely differently named.** Before registering
one, screen it against every registered source with `src.distinctness`: a venue
whose prices are ~100% identical to an existing one is the same counterparty, and
an "arbitrage" between the two is a position nobody can hold. See
[`SOURCE_FEASIBILITY.md`](SOURCE_FEASIBILITY.md).

**`parse` must be a pure function of the bytes it is given.** No network, no
filesystem, no `datetime.now()`, no randomness, no mutable adapter state that
survives a call. "Has this game started?" is judged against `raw.fetched_at` —
data carried in the stored envelope — not against a clock, so replaying an old
capture reproduces the same decision. Replay, regression testing, and reproducing
a live failure offline all depend only on this, and the contract test asserts
that parsing the same input twice yields identical rows.

**`capabilities` must be honest, and must narrow with the tier.** It is what lets
coverage reporting say "this venue has no totals for soccer" instead of inferring
it from an absence, and it is what stops validation reporting a market the run
deliberately did not ask for as a market that has disappeared. A source that
collects less under `--tier core` claims less under `--tier core`.

**`fetch_raw(tier=…)` is a request budget, not a market filter.** `core` asks only
for endpoints that return a whole league at once; `full` adds the per-event
follow-ups. A source with no such follow-up returns the same responses for both
and says so in its docstring — a source that quietly returned less under `core`
would make the two tiers incomparable.

**`fetch_raw` must raise rather than return nothing.** A `SourceError` subclass
(`BlockedError`, `RateLimitedError`, `GeoRestrictedError`, `ServerError`,
`CaptchaError`, `NotJsonError`, `FormatChangeError`, `EmptyResponseError`, …)
distinguishes "blocked" from "slow down" from "not from where you are" from
"format changed" from "genuinely no games". Returning an empty list makes a
Cloudflare interstitial indistinguishable from an off day, and the collector
records it as healthy.

Use `src.sources._common.SourceClient` rather than calling `httpx` directly: it
paces per host, retries only what is worth retrying (honouring the server's own
`Retry-After`), sends a browser-matched User-Agent, and captures every response — including
the refusals — before anything interprets them. Where a venue states a rate limit,
pass `host_interval` and pace to it; three of the ten do.

**`parse` receives exactly one collection run's responses.** Handed two runs, an
adapter that iterates every raw emits every row twice at different prices —
duplicate identities at conflicting prices. Select the latest per endpoint by
`fetched_at` rather than iterating all of them.

**`leagues` must be honest.** The collector reports coverage against it, so a
configured league that returned nothing shows up as a gap rather than being
indistinguishable from one nobody asked for.

### Endpoint labels

`RawResponse.endpoint` is part of the stored filename and of every row's
`raw_ref`, so the labels are load-bearing and must be stable across runs, and
must identify the league. Declare them as module constants or derive them
deterministically from the league key. Where a label carries an index
(`betoffer-batch-01`), that index is a position counter and is **not** stable
across runs when the slate size changes — never derive identity from it.

---

## 2. What every row must satisfy

One row = one priced selection, at one line, from one book, at one observation.
Emit `Quote` and nothing else; `src/vocab.py` is the only vocabulary.

### Identity

| Field | Requirement |
|---|---|
| `source` | Equals `source_key`. Non-blank. |
| `sport` | A `Sport` member. Must equal `src.leagues.league(row.league).sport`. |
| `league` | A canonical key registered in `src.leagues`. Map the book's own league identifier onto it; never invent one. |
| `source_event_id` | The book's own event id. Must distinguish two fixtures of a doubleheader. |
| `home_participant`, `away_participant` | `Participant.key` from `src.participants.canonical_participant(name, competition)`. **This is what everything joins on.** |
| `home_team`, `away_team` | The canonical display name. For a closed roster that is `Participant.name`; never a raw feed string, a matchup string, or a market label. |
| `commence_time` | Timezone-aware, UTC. Scheduled start. |
| `event_key` | Build with `src.events.build_event_key(away.key, home.key, commence_time, competition)`. |

**League is deliberately not part of `event_key`.** Books disagree about
classification constantly — the same tennis match is "ATP Challenger Bonn - R1"
at Pinnacle, `challenger` at Kambi, and a numeric `competitionId` at FanDuel — and
if that disagreement could change the key, the join would break on exactly the
events all three books cover. `league` is carried for coverage reporting and
validated as a *warning*.

**Sport is not in the key either, because it does not need to be.** Participant
keys are namespaced (`MLB-CIN`, `NHL-NYR`, `SOCCER-arsenal`, `TENNIS-humbert.ugo`),
so no two sports can collide.

**Do not rely on `event_key` for doubleheader identity.** An adapter can only
number the fixtures it happens to see, so `#2` is a per-source ordinal. The
pipeline re-derives every key globally in `src.events.reconcile_event_keys`,
clustering start times across all sources at once. Your job is an accurate
`commence_time` and a `source_event_id` that separates the two.

**`commence_time` is not a joinable field.** Books disagree by a minute or two
routinely (FanDuel is one minute later than the other two on every event in the
captured baseball slate), and in tennis by *hours* — a match is scheduled "after
the preceding match on court" and each book publishes its own estimate. Never
require exact equality; the tolerance is the league's `same_event_tolerance`.

**Home/away is a fact in some sports and a coin flip in others.** Where
`League.has_home_away` is true the book's assignment is authoritative. Where it is
false — tennis — each book orders the two names arbitrarily, and trusting that
order mislabels which competitor every price refers to and flips the sign of any
handicap. Use `src.events.orient(...)`, which imposes one deterministic order.

### Market vocabulary — closed enums

`market`, `period`, `selection`, `side` are closed. **A source value that cannot
be mapped is a rejection, never a passthrough.** Free text reaching these fields
makes unnormalized data look normalized, which is the specific failure the schema
exists to prevent — `american_league_cy_young_2026` as a market type is what this
rule is about.

- `market` — `moneyline`, `spread`, `total`, `team_total`. Sport-neutral by
  design: `spread` is a run line in baseball, a puck line in hockey, a point
  spread in football and basketball, an Asian handicap in soccer. The contract is
  the same shape in all of them and `sport` disambiguates.
- `period` — see the settlement section below. This is the highest-risk field.
- `line` — required for `spread`, `total`, `team_total`; forbidden for
  `moneyline`. Stated **from the row's own selection's perspective**: home −1.5
  and away +1.5 are the two halves of one market. A two-sided market must satisfy
  `home.line == -away.line` for spreads and `over.line == under.line` for totals.
  Soccer and tennis use quarter lines (−0.25, +0.75) legitimately; other sports
  land on half-point increments.
- `side` — required for and only for `team_total`.
- `selection` — must be legal for the market, and a `draw` only where
  `src.vocab.draw_is_priced(sport, period)` says the books price one.
- Scale lines and odds out of source units. Kambi sends **both** in thousandths;
  a total of 8.0 arriving as `8000` is a units error, not a line.

### Settlement — the part that matters most

**A market's outcome count is not recoverable from a row.** Downstream must infer
2-way vs 3-way by grouping and counting, and that inference is only sound if
adapters never drop a leg:

> **A leg dropped for want of a price must be counted, and the market it came
> from must be reported as incomplete.** A selection that arrives with no usable
> price is skipped under a stable reason; downstream, `incomplete_market` names
> the market it left short.

Dropping it makes a 3-way market byte-identical to a 2-way one. The two settle
differently in the only outcome that distinguishes them: a tie **voids** both
legs of a 2-way market and **loses** both against a 3-way one's draw. That is the
difference between a floor of zero and a floor of minus the entire bankroll, and
no field on the row can tell them apart.

This clause used to read "*emit it as `SUSPENDED` or reject the whole market*",
and it was followed by no adapter, enforced by no test, and unimplementable as
written: `Quote` requires `decimal_odds > 1.0`, so there is no such thing as a
`SUSPENDED` row carrying no price. The observed shape is a Kambi
`{"status": "SUSPENDED"}` outcome with no `odds` field at all, 29 of them across
five adapters in the captured slate. Rejecting the whole market for it would
discard the side that *is* priced, which is real and useful for line shopping —
so the honest rule is the one the code already implements, made explicit and
given a downstream signal that fires.

`src/vocab.py` records the two facts no book states on the row, per
`(sport, period)`:

| Window | Can end level? | Draw priced? | Consequence |
|---|---|---|---|
| baseball first 1 inning | **yes** | yes | complete 3-way |
| baseball first 5 innings | **yes** | yes | complete 3-way |
| baseball full game | no | no | complete 2-way, no push |
| basketball first half | **yes** | yes | complete 3-way |
| basketball full game | no | no | complete 2-way, no push |
| basketball regulation | **yes** | yes | complete 3-way |
| football first half | **yes** | yes | complete 3-way |
| football full game | **yes** | **no** | 2-way that **voids** on a tie |
| football regulation | **yes** | yes | complete 3-way |
| hockey full game | no | no | complete 2-way, no push |
| hockey regulation | **yes** | yes | complete 3-way |
| soccer first half | **yes** | yes | complete 3-way |
| soccer full game | **yes** | yes | complete 3-way |
| tennis full game | no | no | complete 2-way, no push |

Two entries deserve emphasis:

- **`(FOOTBALL, FULL_GAME)`** can tie and the books do not price a draw, so the
  moneyline voids. Rare, but a voided leg turns a "guaranteed" position into a
  one-sided bet.
- **Hockey `FULL_GAME` and `REGULATION` are different contracts.** Both books sell
  both — Kambi as `Puck Line - Including Overtime and Penalty Shootout` versus
  `Puck Line - Regular Time`, Pinnacle as period 0 versus period 6. Sixty minutes
  is three-way; including the shootout is two-way. Pairing one against the other
  looks like a large edge on two perfectly fair prices. Because `period` is part
  of `dedup_key`, `market_key` and arbitrage pairing, keeping the distinction in
  `period` makes "never match different settlement rules" structural rather than
  a rule someone has to remember.

A `(sport, period)` pair absent from `PERIOD_RULES` is **not collectable**. The
schema refuses it, because a default would be a guess about whether a tie voids
the bet, and that guess is worth the whole stake.

**Soccer scope.** A soccer `FULL_GAME` market is 90 minutes plus stoppage. Cup
extra-time and "to qualify" markets are a different contract and must be counted
out of scope, never mapped to `FULL_GAME`. Two further soccer traps:

- Kambi's `3-Way Handicap` has **three** outcomes and is not the same product as
  a two-way Asian handicap. It must not become a `spread`.
- `Asian Total` uses quarter lines that split the stake across two numbers and
  half-push. It must not be merged with `Total Goals`.

### Price

All three of `decimal_odds`, `american_odds`, `implied_probability` must be
present and mutually consistent:

- `implied_probability == 1 / decimal_odds` (to 1e-6).
- `american_odds` and `decimal_odds` must agree on **net payout** to within 1%,
  so the tolerance means the same thing for a −5000 favourite as for a +2400
  longshot.
- `1.001 <= decimal_odds <= 1000.0` (`src.normalize.MIN_DECIMAL_ODDS` /
  `MAX_DECIMAL_ODDS`). The floor was `1.01` until live data falsified it:
  Pinnacle prints −11540 (1.00867) and FanDuel 1.005, and both are real
  prices. 1.001 is −100000, still unreachable by any scaling mistake — an
  undivided Kambi thousandths value lands above the ceiling, not below the
  floor.
- `american_odds` must be `<= -100` or `>= +100`. Values in between are not
  prices. `-50` converts to a plausible-looking 3.00 and is undetectable
  downstream, so `src.normalize.american_to_decimal` refuses it.

Convert with `src.normalize`; do not hand-roll the arithmetic.

> When an adapter derives one format from the other, the cross-check becomes a
> tautology and cannot detect a feed error. Prefer emitting the **feed's own**
> value for each format when the feed supplies both.

### Provenance

| Field | Requirement |
|---|---|
| `observed_at` | Always `raw.fetched_at` of the response **the price came from**. Never a source-supplied clock — a `cutoffAt` is when betting closes, in the future. |
| `raw_ref` | `raw.ref` of the response the price was parsed from. Must point at a response the collector actually stored. |
| `last_change_at` | Source-reported price-change time, if the feed gives one. Must not be after `observed_at`. Never substitutes for it. |

**Do not assume one run means one `observed_at`.** A batched adapter has one per
batch. Group by run id, not by timestamp.

### Availability and limits

- `status` — `ACTIVE` only when the price is genuinely takeable. This drives
  arbitrage: suspended rows are excluded, so an over-suspending adapter silently
  contributes nothing while its row count still looks healthy.
  **Verify the field's type before coercing it.** Kambi's `closed` is a cutoff
  *timestamp*, and a timestamp string is truthy — reading it as a boolean marked
  739 of 755 rows suspended.
- `limit_amount` — the book's stated maximum stake, when published. `None` means
  unknown, not unlimited.
- `is_alternate` — `True` for a non-primary line. A primary and an alternate at
  the same number are two real, distinct offers and `dedup_key` separates them on
  this flag, so an adapter that leaves it `False` on every row makes the primary
  line unidentifiable. Kambi marks the primary with a `MAIN_LINE` tag in
  `offer["tags"]`; FanDuel prefixes the market type with `ALTERNATE_`; Pinnacle
  sets `isAlternate`.

### Skips and rejections

- **Rejection** = the source offered something in scope that could not be
  represented faithfully. A failure. Counted against health.
- **Skip** = deliberately out of scope (player props, futures, alternate scoring
  units, pitcher-conditional markets). Expected, not an error.

**Every dropped record must land in one bucket or the other.** A bare `continue`
is a contract violation: it makes "we collected everything" unfalsifiable.
Rejections must carry a stable machine-readable `reason` so a format change shows
up as a spike rather than as silence.

Two specific classes that must be skipped, not merged:

- **Pitcher-conditional moneylines** ("Match Odds (X must start)") are a
  different market from the moneyline. Collecting both puts conflicting prices on
  one selection. Match criterion labels **exactly**; substring matching collects
  these.
- **Alternate scoring units.** Pinnacle emits *child* matchups (`parentId` set)
  for these — tennis `units: "Games"` (298 of 598 tennis matchups on one day),
  soccer `units: "Corners"`. A child duplicates the same two participants at the
  same start time, so accepting it both fabricates a doubleheader and maps a games
  handicap as a match moneyline. **Accept only `parentId is None`.**

Futures also leak through two shapes worth naming: FanDuel serves them as events
named `"NFL Futures"` / `"NHL Specials"`, and Pinnacle as markets whose `prices`
carry `participantId` and **no `designation`**.

---

## 3. Hard invariants

Violating any of these breaks the pipeline rather than degrading it.

1. **No two rows share a `dedup_key`.** `(source, event_key, market, period, side,
   selection, line, is_alternate)`. Storage enforces this with a UNIQUE
   constraint; a collision aborts the insert of the whole run's quotes.
2. **`market_key` identifies exactly one market.** `(source, source_event_id,
   source_market_id)` must not span two markets. Several lines, both teams'
   totals, or two periods under one id fuses distinct markets, and every
   market-level check then reads an arbitrary row — silently, because a fused
   group still looks complete. *`source_market_id` need not be globally unique* —
   Pinnacle's `s;0;m` is the moneyline id for every game — but it must be unique
   **within an event** and must distinguish alternates from the primary.
3. **Two-sided markets mirror.** `home.line == -away.line`; `over.line ==
   under.line`.
4. **A book never prices itself to lose.** Implied probabilities across one
   complete market at one book sum to ≥ 1.0. Below that means mispaired prices.
   "Complete" is sport-dependent: three legs where the draw is priced, two
   otherwise.
5. **`observed_at` is a local clock reading, never the source's.**
6. **Parsing is deterministic and side-effect free.**
7. **Participants resolve within their own league.** Resolution is league-scoped
   because a global index would make "Rangers" mean both Texas and New York,
   "Panthers" both Carolina and Florida, "Kings" both Los Angeles and Sacramento,
   and "Jets" both New York and Winnipeg. A cross-sport false match is the worst
   kind: the two events are unrelated and their prices unconstrained.

---

## 4. Verifying an adapter

```bash
python3 -m pytest tests/test_source_contract.py -q     # the contract itself
python3 -m pytest -q                                   # everything
python3 -m src.collector collect --source <key>        # one live run
python3 -m src.collector replay                        # re-parse stored bytes
python3 -m src.collector arb --verbose                 # what was comparable, and why not
```

A source is integrated for a sport when `collect` reports it healthy, `replay`
passes, `arb` counts its markets in `comparable_group_count`, **and it has cleared
the distinctness gate** — not merely when it returns rows.

The contract test derives its adapter list from `src.sources.registry`, and
`tests/conftest.py` raises if a registered source has no captured payloads under
`tests/fixtures/raw/`. So a source cannot be registered and quietly escape the
contract: it fails loudly instead.

**And a sport is only *supported* when two books repeatedly price the same
events.** That is a property of the calendar as much as of the code: in late July
the NHL has no cross-book slate at all, and the NBA has none until October. An
adapter can be complete and correct for a sport that cannot yet be verified, and
the honest report says so rather than counting rows as evidence.
