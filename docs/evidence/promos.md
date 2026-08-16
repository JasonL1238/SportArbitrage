# Promotions: what each catalog actually served

What each promo source answered when asked, which offers confirmed which states, and what
a state label on a promo run really asserts. Newest section first, dated headings, literal
results — a refusal is recorded in the same detail as a success.

## Multi-state batch: four runs, one fetch — 2026-08-16

`python -m src.promos collect --state IL --state PA --state NJ --state DC`, the first batch
ever, hours after first contact and after label-don't-drop shipped. Runs #2–#5, one
`batch_id` (`a3fd0daf…`), **54 offers stored per state** with per-state verdicts:

| state | confirmed / stored | the confirmed set |
| --- | --- | --- |
| IL | 3 / 54 | 2 DraftKings boosts + tl_betmgm "$150 if you win" |
| PA | 4 / 54 | same + tl_fanatics "Bet $20, Get $200 in FanCash" |
| NJ | 4 / 54 | same as PA |
| DC | 3 / 54 | same as IL |

Global sources were fetched **once** and replayed per state (PA/NJ/DC per-source latencies
of 3–8 ms are cache replays, not requests). The run verdict flipped DEGRADED→OK and source
health became honest: betmgm grades OK with its 12 offers where the drop-based gate had
graded it `FAILED [deduped_empty]`; the venues that grade FAILED now are the genuine walls
(fanduel PerimeterX, caesars 403, bet365/onexbet captcha, smarkets cookie wall, betmgm_on
403, fanatics/hardrock 404) plus `vague_offers` on catalogs whose copy carries no numbers.

**What a state label on these runs asserts, precisely.** All four runs were fetched from a
single Illinois egress. `state_confirmed=True` asserts only that the offer's *own copy*
named the state — not that the venue was asked from inside it, and not that the copy is
well-attributed: the tl_betmgm "$150 if you win" confirms in all four states because
TheLines describes two different BetMGM promos in one blob, and the *other* promo's state
list (which includes IL and DC) bleeds into this one's verdict. Its own sentence says
MI/NJ/PA/WV. A venue that geo-varies its public catalog would not be caught either — no
promo `GeoRestrictedError` analogue exists. The stored description on each card is the
operator's recourse; the label is a screen, not a verdict.

## First live promo collection, Illinois — 2026-08-16

The promotions subsystem's first execution ever (`promo_runs` was empty until this run).
`python -m src.promos collect --state IL -v`, from the operator's own Illinois egress, after
a fresh full odds slate (run 30: 85,531 quotes, 35 sources). Batch stored as promo run #1,
verdict **DEGRADED**, exit 1 — 3 offers from 5/24 sources, 5/17 brands.

### Per-source results, all 24

| source | result | detail |
| --- | --- | --- |
| fanduel | **split** | merchandising API answered 200 with `{"promotions":[]}` — zero promos merchandised to anonymous IL; the marketing page was blocked by PerimeterX. Health graded `bot_wall` |
| draftkings | OK | 6 offers, 109 KiB; 2 survived the state gate (both profit boosts whose copy named states) |
| bovada | OK→emptied | 5 offers parsed; every one dropped by the state gate; graded `deduped_empty` |
| cloudbet | OK→emptied | 5 offers; same fate |
| leovegas_kambi | OK→emptied | 4 offers; same |
| betmgm | OK→emptied | **12 offers**, the largest catalog of the run; all dropped by the gate; graded `deduped_empty` |
| caesars | refused | HTTP 403 bot-script body. The body *would* parse into 2 phantom offers — the fetch guard is the defense, pinned by test |
| fanatics | 404 | promotions index path has moved |
| hardrock | 404 | promotions index path has moved |
| bet365 | refused | Cloudflare challenge (`challenge-platform`) |
| leovegas_on | OK→emptied | 4 offers; gate |
| betmgm_on | refused | HTTP 403 without a parseable body |
| onexbet | refused | CAPTCHA on the bonus page |
| pinnacle | OK | 0 offers, `empty_is_ok` — its landing genuinely carries no promo copy |
| smarkets | refused | cookie/bot challenge |
| matchbook | OK | 0 offers, `empty_is_ok` |
| betrivers_kambi | OK | 0 offers, 1 skipped, 257 KiB — the IL landing carries no parseable promo copy |
| tl_* (7 tenants) | OK | one shared fetch; 18 offers total; only `tl_betmgm`'s single offer survived the gate |

**No parse crashes anywhere.** The four families that had never met production HTML either
parsed correctly (bovada 5, betmgm 12, betrivers/pinnacle honest empties) or were refused by
walls. Eight genuine bodies were committed as fixtures under `tests/fixtures/promos/`
(including the refusal bodies), each with a focused test; sources named in the table above.

### The unconfirmed measurement — 94% of the catalog eaten

Pre-gate offers ≈ 54 (draftkings 6, betmgm 12, bovada 5, cloudbet 5, leovegas 4+4,
TheLines 18). Stored: **3**. `filtered unconfirmed for IL: 51`.

The fail-closed gate (`offer_confirmed_for_state`) requires the offer's own copy to name the
state, and marketing copy overwhelmingly doesn't. This is far past the plan's decision
threshold (half), so **label-don't-drop is mandatory** and follows the operator's 2026-08-08
odds-side precedent (collect and show, label every surface).

*Shipped the same day:* every offer now stores the gate's verdict as `state_confirmed`
(store schema v3) instead of being dropped on False. The predicate is unchanged; the CLI
prints `confirmed=NOT {ST}` per offer, the dashboard pills unconfirmed rows, and every
planner card for an unconfirmed offer opens with a verify-in-app caveat.

The audit-trail defect is visible in the health grades themselves: betmgm answered with 12
genuine offers and is graded `FAILED [deduped_empty]` because the gate emptied it
downstream. A working source reads as broken, and the dropped offers leave no row anywhere.

### The one survivor is wrong — twice

The single national offer that passed the IL gate, `tl_betmgm` "Bet $10 get $150 in bonus
bets **if you win**", should not have passed and was then mispriced:

1. **Wrong state.** TheLines' BetMGM copy describes *two* different promos in one blob: a
   "$1,500 paid back if you don't win" valid in a list *including IL*, and the "$150 if you
   win" available only in **MI, NJ, PA and WV**. `parse_eligibility` merged both lists, so
   the NJ/PA-only variant was stored as IL-confirmed — while 51 legitimate offers dropped.
   One description covering multiple offers defeats region attribution; the honest fix is
   the Phase 1 label, which shows the operator the copy rather than asserting eligibility.
2. **Wrong price.** The planner printed `ev=+108.57` with no win-conditional caveat: its
   `_WIN_CONDITIONAL` regex matches "if your bet wins" but not the plain **"if you win"**.
   Conditional credit was priced as unconditional. The honest number is
   `p(win) × conversion × 150 − qualifying_cost` — still positive, materially smaller.
   Found on the first live offer this planner ever priced; *fixed the same day*: the
   subject set admits the bare "you" (with the unconditional "win or lose" veto mirrored
   onto the win reading so the commonest bet-and-get wording stays unconditional), and
   the classifiers read summary+title — the enricher's canonical summary drops trailing
   clauses, and the description is deliberately excluded because it is where the *other*
   offer's state list and loss clause live.

### The planner worked on first contact

`promo run #1 × odds run #30: 3 offers over 27,640 priceable markets`. All three offers
produced concrete plans: DraftKings 25% profit boosts as `bonus_conversion`
(63.7–67.9% measured conversion) and `boost_locked` (+$11–12 per $100), and the
qualify-then-convert card above ($0.04 measured qualifying cost against a $10 stake).
Gate counters fired in volume and correctly (`below_min_odds ×457`, `no_hedge_price ×2471`,
`observation_spread ×1752`, `regime_mismatch ×87` — the prediction-market exclusion working).
Known display gap, accepted for now: hedge legs carry no locality marking, so a plan may
name a hedge book (smarkets, onexbet) not reachable from the run's state.

### Operational notes

- `python -m src.promos collect` has **no alert path** — it cannot text anyone. `plan` is
  fully offline.
- `plan` auto-selects the latest odds run for the promo run's jurisdiction; a fresh full
  odds slate must immediately precede planning or hedges price against a stale/thin run.
  `plan --odds-run N` now pins a specific run, refusing cross-jurisdiction picks.
- The planner now refuses offers whose stated `ends_at` has passed (counted as
  `expired`), and the settlement-regime gate — which fired 87 times on the first live
  slate — gained the test that asserts it fires.
- The dashboard's Promos panel gained a **Campaign** table: every offer ranked by the
  planner's own EV figure, claimed-state per browser; the operator playbook it serves is
  `docs/PROMO_CAMPAIGN.md`. The bet ledger records promo positions truthfully
  (`stake_kind`, bonus stakes excluded from bankroll, `promo_source`/`promo_offer_id`
  linkage).
- `stakeable_odds_sources` filters by the *ambient* `VIEW_ONLY_SOURCES` (process
  `ODDS_STATE`) — suppress-only, documented at `planner.py:340-347`; left as is.
- Automatic egress detection failed this run (ipapi.co 403 — its known Cloudflare wall);
  detection is advisory and the explicit `--state IL` governed.
