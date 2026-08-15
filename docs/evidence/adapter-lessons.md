# What the adapters had to be corrected for

Corrections the adapters were forced into: charges, settlement, and the traps
that produced a plausible wrong number rather than an error.

## Two corrections to the original plan

**Polymarket is far better structured than assumed.** The plan expected
"prose-titled binaries" needing a confidence-scored decomposer, and recorded that
standardised game totals largely do not exist. Neither is true of the Gamma API
as it stands: an event carries `teams` with an explicit `ordering: home|away`, a
`startTime`, and markets carrying `sportsMarketType` (`moneyline`, `spreads`,
`totals`) with a numeric `line`. Totals exist at 7.5, 8.5 and 9.5 on the MLB
slate. So the mapping is from structured fields only, exactly as required, and no
title is ever parsed.

**`settlement_shape_mismatch` was never all-or-nothing.** The plan lists it
alongside `source_prices_itself_to_lose` as a refusal that discards the whole
group. Reading the code, it records a diagnostic and then carries on per contract
shape; only `source_prices_itself_to_lose`, `mixed_sport` and
`legs_disagree_on_the_game` actually abandoned the group, and those are what were
fixed.

---

## Notes on charges

Getting a commission rate wrong in the *generous* direction manufactures
arbitrage; getting it wrong in the strict direction only loses one. Every
uncertainty below is resolved in the second direction on purpose.

- **Matchbook, Smarkets** — 2% of net winnings, the published standard rate. An
  account can earn a lower one through volume, so using the standard rate
  understates the edge rather than inventing one.
- **SX Bet** — its own `/metadata` reports `oracleFees` of `5 × 10^18` against a
  `10^20` scale. That reading is not stated in words anywhere in the payload, so
  it is taken as **5% of net winnings**, the most expensive plausible
  interpretation.
- **Kalshi** — the published schedule, `0.07 × p × (1−p)` per contract, charged on
  entry.
- **Polymarket** — read from the market payload's own
  `feeSchedule: {rate: 0.05, exponent: 1, takerOnly: true}`, which is
  `0.05 × min(p, 1−p)` per contract.

A charge on entry is not a rate on winnings with some equivalent number: it is
due whether or not the contract settles your way. Both shapes are modelled
separately in `src/commission.py`.

---

## Notes on settlement

A commission changes how much a leg pays. What a venue does with a game that is
never played decides whether the leg is part of a hedge at all, and the venues
here do three different things. Quoted from the captured payloads:

- **Sportsbooks and the exchanges** — void a cancelled or long-postponed fixture
  and return the stake. Matchbook, Smarkets and SX Bet are in this regime with
  the books: their markets void with the underlying event.
- **Kalshi** — `rules_secondary`, on every one of the 354 captured markets: *"If
  this game is postponed or delayed, the market will remain open and close after
  the rescheduled game has finished (within two days). If the game is cancelled
  or rescheduled to over two days away, the market will resolve to a fair price
  in accordance with the rules."*
- **Polymarket** — market `description`: *"If the game is postponed, this market
  will remain open until the game has been completed. If the game is canceled
  entirely, with no make-up game, this market will resolve 50-50."*

So a book leg against a Kalshi leg on a rained-out game leaves the book refunding
while Kalshi settles from the make-up game — a naked one-sided bet for the whole
Kalshi stake, on a position that was reported as risk-free. Against Polymarket, a
contract bought at 0.75 returns 0.50 while the book returns everything.

`src/settlement.py` records the regime per source and the registry refuses a
source that declares none, exactly as it does for commission. The rules are not
*modelled* — each venue reserves discretion the payloads do not pin down, and a
caveat that overstated its own precision would be worse than one that says
plainly what is unknown. What is modelled is the only thing the engine needs:
which venues void together. Every position spanning two regimes carries the
note.

---

## Traps the adapters had to be corrected for

Recorded because each was found only by reading a venue's bytes against another
venue's, and every one of them was silent — valid rows, healthy sources, no
finding of any kind.

- **Four adapters dropped rows with no reason recorded, and the losses were
  large** (found 2026-08-15 auditing run 16's own findings). The contract calls a
  bare `continue` a violation because it makes "we collected everything"
  unfalsifiable; these are what that costs.

  | adapter | the drop | scale, run 16 |
  | --- | --- | --- |
  | `vegasinsider` | `headers.index(book_label)` raised, bare `return` | **46 MLB fixtures × 8 sources**, every run |
  | `hardrock` | one `slice: {from: 0, to: 80}` taken as the whole scope | soccer 81 of **664**, tennis 81 of **144**, football 81 of **150** |
  | `cloudbet` | every `handicap > 0` deleted as a "mirror restatement" | **358 rows**, and a wrong main line on 100% of the rest |
  | `actionnetwork` | `by_side` collapsed rows across `market_id`s | 135 fabricated markets on disk, masked by an unrelated status filter |

  **VegasInsider** stopped publishing per-book columns on its baseball and hockey
  pages between 01:54 and 07:50 on 2026-08-14 — the earlier capture has eleven
  columns, the later has `Time / Open / Consensus` and 46 fixtures. `vi_fanatics`
  was the only source that surfaced, reporting `parsed 0 quotes from 4 responses;
  skipped={}`, and only because Fanatics appeared on the MLB page alone; its
  seven siblings each lost one board and looked merely thin. Every drop now names
  the book that vanished *and* the columns that were offered, because which is
  which is the whole diagnosis.

  **Hard Rock** asks the API for `count` in its own GraphQL query, and the venue
  answers honestly — 664 soccer events — and nothing read it. `tally.produced`
  was handed the truncated 81, so `scopes_truncated` never fired. DraftKings
  makes the opposite choice and says why: "a silent cap reads as 'that is the
  whole slate' when it is not." Now pages, and reports the venue's own number
  when it cannot finish.

  **Cloudbet's** filter rested on a premise the bytes contradict. One
  `baseball.run_line` carries all four rows: `handicap=+1.5` home 1.49 / away
  2.50, and `handicap=-1.5` home 2.80 / away 1.42. 1.49 is not the mirror of
  2.80 — they are the market where home is favoured and the market where away
  is. Keeping only the negative framing made the home line negative in **every**
  stored cloudbet row, so whenever the home team was the underdog the stored main
  line was the wrong bet, which is what fired `line_favours_the_other_competitor`
  on 12 of 33 shared fixtures. The two directions now carry the venue's signed
  handicap as their market id; keying on `abs()` had filed all four rows under
  one id, the "two offers sharing a key" shape `src/schema.py` warns about.

- **Action Network published a spread whose two sides were the same line**
  (2026-08-14). Book 30 — the *opener* column — carried `home -1.5 @ +175` and
  `away -1.5 @ +162` under one `market_id` for MLB-MIA@MLB-CIN. Both prices are
  individually plausible, so every per-row check passed; the implied sum 0.7453
  surfaced only after storage, as two run-level ERRORs. `docs/INPUT_CONTRACT.md`
  lists mirroring among the **hard invariants**, whose violation "breaks the
  pipeline rather than degrading it", so the pair is now refused where it arrives
  rather than written and flagged.

  The same investigation found the adapter's own latent version: `_emit_v2_market`
  reduced its rows to a dict keyed on `side`, so two markets in one list became
  one market holding the home price of the first and the away price of the
  second, filed under whichever `market_id` sorted first — 63 such pairs at book
  262, 27 at 270, 27 at 1538, 9 each at 282 and 4601. Every one was masked by the
  `inprogress` status filter dropping the fixture for an unrelated reason, which
  is not a guard: book 15 carries the same shape on *scheduled* games.

- **1xBet invented 24 fixtures out of player names and the words "Home" and
  "Away"** (2026-08-14), 9.3% of its event keys that run, every one a
  single-source card carrying real prices. Two causes, two structural fixes. The
  competition is literally named `"Spain. La Liga. Team vs Player"`, and its
  country prefix made `league_from_label` file it as LA_LIGA. And `DI` — 1xBet's
  own "aggregates N matches" descriptor — appears on exactly 16 events across
  every captured payload and **not one is a fixture**. Neither needed a new
  heuristic; both were already in the bytes. Deliberately *not* solved by
  widening `is_statistic`, which a test pins as "it must not catch a competitor"
  and which five other adapters share.

- **One fixture under two `event_key`s, because two books spell a club
  differently** (2026-08-14). Soccer resolves through an open slug, so a spelling
  disagreement is an unrecoverable split — and **nothing detects it at runtime**:
  `participant_pair_disagreement` groups by `event_key` and `_check_orientation`
  groups by the participant-key set, and a split differs in both. Measured on run
  16: 271 soccer clusters held one fixture under two or more keys. Filtered to
  what costs money — a cross-key pair of US-bettable books inside the 180 s
  `arb.MAX_OBSERVATION_SPREAD` — **21** clusters, in MLS, Serie A, Ligue 1,
  Bundesliga and La Liga.

  Two systematic classes, not a list of one-offs. *Legal forms and affixes*:
  `parma`/`parmacalcio`, `lazio`/`sslazio`, `lecce`/`uslecce`,
  `venezia`/`unionevenezia`, `atalanta`/`atalantabc`, `lille`/`lilleosc`,
  `angers`/`angerssco`, `leverkusen`/`bayerleverkusen` — closed by adding
  `calcio`, `ss`, `us`, `unione`, `bc`, `osc`, `sco`, `fsv` and `bayer` to
  `_CLUB_TYPE_TOKENS`. **`se` was tried and refused**: it belongs there by shape
  (SE Palmeiras, *Sociedade Esportiva*) and is also Sergipe's state code, which
  is how books tell two Brazilian clubs of one name apart. The disjointness
  assertion in `TestAStateCodeIsNotAClubType` caught it — the same guard the
  Botafogo RJ/SP finding put there, doing its job on a second case two months
  later. *MLS city codes*: BetRivers' Kambi tenant is the only
  source that writes `CHI Fire` / `SEA Sounders` / `COL Crew`, while the other
  tenant of the same platform writes them out — 14 clubs, each 1 source against 8
  or 9, closed by curated aliases.

  Founding years split the difference: a **trailing four-digit** year is stripped
  as a rule (`Como 1907`, `Bologna 1909`), a **leading** one never is (`1860
  Munich` reduced to `munich` would be a different claim about who is playing),
  and two digits are never stripped in either position because `Schalke 04` is a
  founding year that every book keeps — so `SV 07 Elversberg`, `Bayer 04
  Leverkusen` and `Paderborn 07` are curated aliases instead.

  Result on run 16: soccer event keys 1799 → 1722, and clusters costing a
  US-bettable pair **21 → 2**. The residual is deliberate: FanDuel writes bare
  `Deportivo`, and `_SOCCER_ALIASES`' own comment records that on a live capture
  "Deportivo" is Deportivo Pasto — so the alias is refused and the split is named
  in the test rather than closed. One unjoined source is recoverable; two clubs'
  prices on one fixture is not.

  Widening `test_the_fixture_slate_no_longer_splits_the_match` past tennis — it
  had been scoped to it since it was written — immediately found eleven more in
  the committed captures alone: `inter`/`internazionalemilano`,
  `betis`/`realbetis`, `celta`/`celtavigo`/`celtadevigo`, `marseille`/
  `olympiquemarseille`, `brest`/`stadebrestois29`, `troyes`/`estactroyes`,
  `alaves`/`deportivoalaves`, `espanyol`/`espanyolbarcelona`,
  `racingsantander`/`racingdesantander`. That test is now the only thing
  reporting this class at all.

- **Five adapters classified soccer competitions by name, and every one of them
  collided** (2026-08-14, swept together). BetMGM and Cloudbet carried the
  *identical* unanchored marker table, byte for byte; 1xBet and Hard Rock carried
  their own; Smarkets carried a country-prefixed table holding un-prefixed keys.
  Measured against their own captures:

  | adapter | grammar | what went wrong |
  | --- | --- | --- |
  | BetMGM | ids, unused | 29 EPL fixtures on a slate of 10; see below |
  | Cloudbet | `soccer-<country>-<slug>` | dropped at *fetch* time, uncounted |
  | Hard Rock | `Country - Competition` | `Brazil - Serie A` → SERIE_A (19), `Spain - La Liga 2` → LA_LIGA (8) |
  | 1xBet | `Country. Competition` | anchored left, open right: `Premier League 2` → EPL |
  | Smarkets | `country-competition` slug | table held `la-liga` where the URL says `spain-la-liga` — **0 of 947** football events matched |

  Smarkets is the largest soccer feed in the run and *every* top-flight fixture
  it priced was filed as `SOCCER_OTHER`; nine of the run's fifteen
  `league_disagreement` findings were that one bug. No row was ever lost — league
  is not part of `event_key` — but the league filter, the coverage grid and every
  per-league count were wrong.

  All five are now anchored on the venue's own country segment, matched with
  separators removed (the trap that made `LA_LIGA` unreachable at BetMGM), with a
  second-tier demotion and `competition_marker` demoting women's/youth/reserve
  tiers. **1xBet keeps its drop deliberately**: it fetches one request per
  competition and listed **847** soccer competitions against the 20 it collects,
  so a catch-all would make one pass ~830 requests at a venue that served a
  CAPTCHA on its baseball scope in that same run. Cloudbet's catch-all costs 20
  extra requests and Hard Rock's costs none, both being bulk fetches.

  Cloudbet's and Hard Rock's own names for the English, Italian, German and US
  top flights are **unmeasured** — no stored capture carries them, those seasons
  not having begun — so their country prefixes come from the observed grammar and
  nothing else. A wrong prefix lands the competition in the catch-all, which
  `league_disagreement` reports; a guessed full name could land it on the wrong
  senior key, which nothing would.

- **BetMGM's `sourceName` "1" is the away side on a US title and the home side
  on soccer's 1X2** (found 2026-08-14, IL egress, run 16 capture). The adapter
  applied the US reading unconditionally, with the comment *"On 'Away at Home'
  fixtures, sourceName 1 is the away side"* describing a condition the code
  never checked. Measured over every Match result option whose label matched a
  side exactly: soccer `sourceName` 1 → home **688** times, 2 → away **694**,
  with no counterexample. It fires only when the label match fails first, which
  is why it survived — and BetMGM prices `"FC Dynamo Kiev"` on a fixture whose
  participant it spells `"FC Dynamo Kyiv"`, so the label match does fail. The
  away price then landed on the home key. It surfaced as a `duplicate_dedup_key`
  rejection rather than as a wrong price *only* because the home option had
  already claimed that key; had the labels failed in the other order it would
  have published silently. `_sides` now returns which side the book listed
  first, and `sourceName` counts from that.

- **BetMGM classified 109 soccer competitions by unanchored substring**
  (2026-08-14). One `sportIds=4` feed carries "LaLiga" beside "LaLiga 2",
  "Serie A" beside "Serie B" *and* "Brasileiro Serie A", "Bundesliga" beside
  "2. Bundesliga", "Ligue 1" beside "Ligue 2", and five unrelated competitions
  called "Premier League" (Welsh, Maltese, Tanzanian, Armenian, Canadian). The
  run reported **29 EPL fixtures on a slate holding 10**, and 24 BUNDESLIGA on
  one holding 9. It also read `Frauen-Bundesliga` as `BUNDESLIGA` — a women's
  fixture on the men's key, which is the fault
  `TestTheWomensMarkerReachesEveryVenueThatNeedsIt` exists to prevent, live, at
  an adapter that list recorded as *immune* to it. Nothing had ever checked the
  immunity claim against the adapters it exempted. Two silent gaps came out with
  it: BetMGM writes the Spanish top flight as one word, `"LaLiga"`, so the
  `"la liga"` marker never fired and a **declared** league was unreachable from
  the feed; and unrecognised competitions were dropped rather than caught, so of
  **712** soccer fixtures returned, **599 were discarded** — 333 of them
  fixtures other books in the same run had priced, 109 of those showing exactly
  one source. Every id is stable on this feed, so routing is now by id, which
  makes each collision unreachable rather than guarded case by case.

- **Bovada writes an Asian split line as two fields.** `price.handicap` and
  `price.handicap2` are the two halves the stake divides between, always exactly
  half a point apart (54 of 54 in the fixtures, 114 of 114 live). The line is
  their midpoint. Reading only the first published a −0.25 handicap as a
  draw-no-bet, which is a different contract, not a near-miss: **41 of 43
  reported positions on a live slate were built on a mis-stated line**, and
  reading the pair correctly took that run from 43 opportunities to 2.

- **Kalshi does not publish `no_ask_size_fp`.** It appears in none of the 354
  captured markets, so 255 of 590 rows carried no bankroll cap and were sized
  entirely from the other leg. Buying NO at its ask is selling YES at its bid —
  `no_ask == 1 - yes_bid` holds exactly on all 354 — so the depth is the YES bid
  size. One of the caps that reappeared is $0.12.

- **Matchbook's `odds` means whatever `odds-type` says.** Both fields are on all
  17,057 captured prices and agree, because the type is `DECIMAL` on every one.
  The dangerous direction is AMERICAN: a true decimal 4.00 arrives as `+300` and
  would be recorded as decimal 300.0 — an implied probability of 0.33%, inside
  the plausibility band, on every underdog. The adapter reads `decimal-odds`,
  which is unconditional.

- **SX Bet's `type` is not a global vocabulary.** Type 52 is the match winner in
  tennis and a draw-loses two-way in soccer. One flat table held only the
  US-sport codes, so the whole soccer and tennis book was fetched and discarded
  while the source declared eighteen leagues and delivered three. The table is
  keyed by `(sport, type)`, which made it safe to add tennis and correct to leave
  soccer out until someone maps it deliberately.

- **Pinnacle's `isLive` is a product flag, not a clock.** It says whether a
  matchup has moved to the in-play book, which is not whether the game has begun:
  248 rows across 18 fixtures carried a start time in the past, all `isLive:
  false`, all emitted as active — including a tennis match two hours old priced
  at 1.01. Every other adapter already compared the start against the capture.

- **Pinnacle routes unrecognised soccer competitions to one catch-all league,**
  and the competition name was then read by nothing — so `Club Friendlies Women`
  Utrecht v De Graafschap produced a key byte-identical to the men's fixture. The
  marker is now taken from the competition and appended to the club, for club
  sports only: a player is the same player whatever the draw is called.

- **Kambi's betoffer responses carry no `range` field.** The truncation guard read
  `range.total` and therefore returned `None` unconditionally, which killed both
  the fetch-time batch-halving retry and the parse-time rejection. The `events`
  array is real and echoes the request exactly, so a shortfall there is the same
  signal against bytes that exist.

---
