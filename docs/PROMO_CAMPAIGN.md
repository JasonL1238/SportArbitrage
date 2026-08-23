# The promo campaign: spending welcome offers well

How to turn the promos subsystem's output into money, in order, across states. The
dashboard's **Campaign** table (top of the Promos panel) is the working surface: one row
per offer, ranked by the planner's own EV figure, with a per-browser "done" checkbox. This
document is the judgment the table cannot encode.

The single load-bearing fact: **a welcome offer burns once per person per book, forever —
not per state.** DraftKings claimed in Illinois is DraftKings burned in Pennsylvania. So
the campaign's unit is the *book*, and the state you are standing in only decides which
books are claimable today.

## Where each book can be claimed

From the same routing table the collector uses (`docs/MULTI_STATE.md`):

| Book | IL | PA | NJ | DC | Scheduling |
| --- | --- | --- | --- | --- | --- |
| FanDuel, DraftKings, BetMGM, Caesars | ✓ | ✓ | ✓ | ✓ | **flexible** — claim wherever you are |
| BetRivers, bet365 | ✓ | ✓ | ✓ | — | not DC |
| Hard Rock | ✓ | — | ✓ | — | IL or NJ only |
| theScore Bet | ✓ | ✓ | — | — | IL or PA only |
| betPARX | — | ✓ | ✓ | — | PA or NJ only — its lobby's own promo feed is read per state since 2026-08-23 |

**Scheduling rule:** constrained books get priority when you are physically in a state
that carries them; flexible books are fill. A DC week should work the flexible four and
save Hard Rock and theScore for IL.

## The EV ladder (which offers first)

1. **Bet-and-get** (`qualify_then_convert`) — the table's `EV` column is fully computed:
   `bonus × measured conversion − qualifying cost`. Typically the best $/float in the
   pool (a "$10 → $150" prices at ~$105–110 for ~$10 of float). Claim these first.
2. **No-sweat / risk-free** (`no_sweat_hedge`) — real but slower money; ties up the
   protected stake plus the hedge, and the refund is credit worth its measured
   conversion, not face value.
3. **Straight credit drops** (`bonus_conversion`) — `bonus × conversion`, typically
   65–75% of face.
4. **Deposit matches** (`rollover_grind`) — last: slowest, most float, terms-heavy. Skip
   any whose computed EV is thin; a 20x rollover at 5% vig is negative.
5. **Boosts** are recurring garnish at books you already hold, not campaign slots.

## Bootstrapping: hedges need funded accounts

The planner picks hedges from the whole slate; it does not know which books you hold.
Open the **first two books as a pair** (two flexible bet-and-gets) so each book's
qualifying and conversion legs hedge at the other. After that, every new book hedges into
the growing set. When a top plan names a hedge book you don't hold, the #2/#3 plan cards
on the same offer usually give up only 1–3 points of conversion.

**Float:** at typical prices (promo leg ~3.0, hedge ~1.5) the conversion hedge needs
about **1.33 × the bonus** in cash at other books, returned as legs settle over 1–3 days.
~$1,500 of working float runs two book cycles at once. Sequence deposit matches after the
bet-and-get harvest has built the float.

## Per-offer-type execution

Every plan card carries legs (book, selection, odds, stake, link), the outcome table, and
two floors (`worst` = all outcomes, `settles` = excluding pushes). Universal rules:
**re-check both prices at placement** — a plan is a snapshot, and if the hedge has moved
by more than the printed floor, walk away and re-plan; place the cash side first and the
credit side last when the credit could expire.

- **Bet-and-get:** round 1 (`qualify`) — small cash stake at the promo book, hedged;
  cost is the printed vig. Round 2 (`convert`) — once the credit lands, stake it at ~3.0
  at the promo book, hedge elsewhere. *Verify manually:* the qualifying bet's min odds on
  the actual slip, whether the credit arrives as one bonus bet or split into several
  (re-plan per piece if split), and the credit's own expiry (often 7 days — the payload
  does not know it).
- **No-sweat:** both legs together; if the qualifier loses, run the refund as a
  conversion round after re-collecting odds. Never value the refund at face.
- **Deposit match:** repeated low-vig round trips using the market with the smallest
  `cost per $100`; expect the deposit and bonus to be locked together until cleared.
  *Verify:* what counts toward rollover (min odds per bet, market weightings, window).
- **Boosts:** `boost_locked` cards are placeable as printed; `boost_breakeven` cards are
  shopping lists — the percentage a token must exceed — never place them with a smaller
  token. Book-authored odds boosts on props are manual plays the slate cannot hedge; skip
  unless you can price the fair line yourself.

## Rules that are not negotiable

- **One account per person per book, ever.** Multi-accounting, household referral
  farming, and re-claiming burned offers are fraud, not advantage play.
- **Physical presence at bet time** (GeoComply). Cross-state promo *collection* is
  research; betting requires being there. No VPNs, no GPS spoofing — this repository
  refuses geolocation evasion and so does this playbook.
- **The label is a screen, not a verdict.** An offer's `state_confirmed` stamp means its
  own copy named the state — the first live run showed a case where the stamp was wrong
  because one description covered two different promos. Read the offer's copy in the app
  before depositing anything.
- **Bet-mix hygiene is your problem.** An account whose only history is one qualifier,
  one 3.0 conversion, and a withdrawal reads as exactly what it is, and books limit such
  accounts. Occasional ordinary bets, partial withdrawals. The software models none of
  this.
- **Taxes are real** (informational, not advice): gambling winnings are taxable; Illinois
  taxes them at a flat 4.95% with no loss offset; other states differ. Hedged promo
  profit is still profit; the betlog's leg-level record is the substantiation.
- **Never override a plan the planner refused.** The `gated out` counts on a card are
  reasons — several of them were learned from measured losses.

## The weekly loop

1. `python -m src.promos collect --state IL --state PA --state NJ --state DC` — catalogs
   for every configured state, from anywhere.
2. `python -m src.collector collect --state <where you are> --no-alert` — a fresh odds
   slate in the state you can bet from. Plans price hedges off this run, and stale slates
   plan nothing (`already_started` eats the board).
3. Open the dashboard (`python -m src.report --serve <port>`) → Promos → Campaign table,
   or `python -m src.promos plan` (add `--odds-run N` to pin a specific same-state run).
4. Act on the top 1–3 affordable rows, re-verifying each leg at the book.
5. **Log every leg** as a slip (`kind: promo`, each leg's `stake_kind` as placed —
   `cash` for qualifiers and hedges, `bonus` for credit; the slip's `promo_source` /
   `promo_offer_id` from the plan's own key). The ledger's arithmetic is
   promo-aware: bonus stakes are not bankroll and a bonus win's whole return is profit.
6. Settle yesterday's legs honestly; tick the Campaign checkbox when a book's welcome
   cycle is done (credit converted, cleared, withdrawn, every leg settled).

"Done with book X" = the offer's key is on a settled slip, the withdrawal has landed, and
the account stays open with a small balance as a future hedge venue.

## Out of scope, deliberately

Casino promos (playthrough variance is a different EV model), correlated-parlay offers
(the planner refuses to model them), referral programs, login automation of any kind
(research stays anonymous — `docs/testing.md`), and anything a book's terms name as
abuse. The pool of legitimate one-time welcome EV across four states is large enough not
to need any of it.
