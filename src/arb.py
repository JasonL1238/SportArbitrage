"""Cross-book arbitrage detection over normalized quotes.

An arbitrage is a set of bets, one per settlement outcome, whose payouts leave a
profit no matter which outcome occurs.  The arithmetic is trivial; everything
that makes this module non-trivial is about refusing to call something an
arbitrage when it is not one.  The failure mode being defended against is not a
missed opportunity — it is a *false* opportunity, because acting on one loses
real money.

The traps, and how each is closed:

**Mispaired lines.**  ``line`` on a row is stated from that row's own
perspective, so a home spread of -1.5 and an away spread of +1.5 are the two
halves of one market.  Pairing is therefore done on a single canonical
orientation (:func:`canonical_line`) rather than on the raw field, which makes
"home -1.5 against away +2.5" impossible to express.

**Push.**  Home -1 and away +1 are complementary until the home team wins by
exactly one, at which point *both* legs refund and the profit is zero rather than
the advertised margin.  Rather than carry a boolean that callers may ignore,
every opportunity enumerates its settlement outcomes explicitly and reports the
*minimum* profit across all of them.  A push outcome cannot be overlooked
because it is part of the number.

**A tie that no field on the row mentions.**  This is the sport-aware core of the
module.  :data:`src.vocab.PERIOD_RULES` records, per ``(sport, period)``, whether
the window can end level and whether the books price a draw for it, and those two
bits decide the outcome set:

* ``(FOOTBALL, FULL_GAME)`` can end level and US books price no draw, so an NFL
  game still tied after overtime **voids** the two-way moneyline.  That is a push
  outcome created by nothing on the row, and it is enumerated exactly like a
  whole-number spread landing on its number.  Without it, a 4.8% "guaranteed"
  NFL moneyline position is really a 4.8% position with a floor of zero.
* ``(BASEBALL | BASKETBALL | HOCKEY | TENNIS, FULL_GAME)`` cannot end level —
  extra innings, overtime and the shootout run until somebody wins — so a
  complete two-way moneyline there has no push outcome at all, and inventing one
  would understate the profit floor as zero for an impossible outcome.
* ``(SOCCER, FULL_GAME)`` and ``(HOCKEY, REGULATION)`` price the draw, so a
  *complete* market has three legs.  A two-way market in one of those windows is
  **incomplete**, and which way the missing tie settles differs by the whole
  bankroll: either the book voids ties, or its draw leg arrived without a price
  and was dropped, in which case a level score loses both legs.  Nothing on the
  row says which, so no position is reported (``ambiguous_tie_settlement``).

**Quarter lines.**  A soccer Asian total of 2.75 splits the stake between 2.5 and
3.0, so a match landing on exactly 3 refunds half the stake and settles the other
half.  That is a *half* push, enumerated as its own outcome with its own
multipliers, and its profit works out to exactly half the profit of the side whose
half survives — so a quarter line reduces a real edge rather than reversing it,
which is why it can be reported at all instead of refused.  A line at a
granularity this module does not model *is* refused, with a counted reason, rather
than settled as if it were a half line: 200 of the captured slate's lines are
quarter lines, so this is a live case and not a hypothetical one.

**Incompatible contracts.**  A two-way moneyline where a tie refunds is a
different contract from a three-way one where a tie is a losing outcome.  Backing
home at the first and away at the second is not an arbitrage: a tie loses both
legs, and on fair prices it looks like a 5% edge.  So legs are only combined
across books offering the same outcome set (:func:`contract_shape`).

**Prices from a book that contradicts itself.**  A book whose own complete market
prices below 1.0 has been mispaired by the parser.  No leg is drawn from it, even
when the rest of the position looks sound, because that leg is not the bet it
appears to be.

**Prices that were never simultaneously available.**  Two quotes observed twenty
minutes apart never coexisted, and a game that has already started cannot be
backed at all.  Legs must be active, observed close together, and — when a clock
is supplied — belong to a game that has not yet begun.

**An edge smaller than a betting unit.**  Stakes go on in whole units, so the
guarantee is recomputed *after* rounding and the split is chosen to maximise that
floor rather than to minimise rounding error.  A position whose floor is negative
is reported as rejected, not as money.

Middles — backing over 8.5 at one book and under 9.5 at another — are
deliberately *not* detected here.  A middle risks the stake to win a larger
amount, which is a different product from a risk-free arbitrage, and reporting
the two through one interface is how a middle gets bet as if it were riskless.
"""
from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Collection, Iterable, Mapping, Sequence

from src.commission import Commission, commission_for
from src.redundancy import REDUNDANT_PAIRS, is_redundant_pair
from src.sources.registry import VIEW_ONLY_SOURCES
from src.settlement import mismatch as settlement_mismatch

if TYPE_CHECKING:
    from src.distinctness import Agreement
from src.schema import (
    Market,
    Period,
    Quote,
    QuoteStatus,
    Selection,
    Side,
    Sport,
    draw_is_priced,
    scoring_unit,
    tie_possible,
)

#: Source keys that participate in an intentional failover pair.  Used to keep
#: the hot assignment path at the old ``len({who(s)})`` cost when no failover
#: key is present.
_FAILOVER_KEYS: frozenset[str] = frozenset(
    key for pair in REDUNDANT_PAIRS for key in pair
)

#: Only report an edge above this. Zero means "any strictly positive edge", but
#: a real edge has to clear transaction friction, so callers normally raise it.
DEFAULT_MIN_MARGIN = 0.0

#: Bankroll allocated across the legs of one opportunity.
DEFAULT_TOTAL_STAKE = 100.0

#: Books accept whole currency units. Stakes are rounded to this, and the
#: reported profit is the profit *after* rounding, not before.
STAKE_INCREMENT = 1.0

#: Two legs must have been observed close enough together to have plausibly
#: been available at the same moment.
#:
#: This bound is **relative** — it compares the legs to each other — and that is
#: the whole of what it checks.  Two prices captured six weeks ago have a spread
#: of seconds and clear it perfectly.
MAX_OBSERVATION_SPREAD = timedelta(seconds=180)

#: And how old the newest leg may be, measured against *now*.
#:
#: The relative bound above cannot catch a stale run, and neither can the
#: started-game gate: a fixture six weeks out is still in the future, so a run
#: collected six weeks ago reports every one of its positions as takeable. That
#: is not a hypothetical failure mode but the *expected* one — it is what
#: ``arb`` does the morning after collection silently stops, because
#: ``latest_run_id`` returns the newest finished run however old it is.  A page
#: of "guaranteed" positions priced weeks ago is the exact shape this pipeline
#: exists not to produce.
#:
#: Fifteen minutes: three times the default collection interval, so a command
#: run against a healthy pipeline can never trip it, and small enough that
#: nothing survives an outage.
#:
#: Enforced by the commands that read a *stored* run rather than inside the
#: detector, because that is the only place it can happen.  A live pass has just
#: collected its prices, and the detector is a pure function of the rows it is
#: given — it has no way to tell "these are six weeks old" from "the caller is
#: deliberately studying history", which is a legitimate thing to do.  The
#: command knows which of the two it is.
MAX_PRICE_AGE = timedelta(minutes=15)

#: Floating-point dust: 1e-9 of implied probability is not an edge.
_EPSILON = 1e-9

#: Above this, a "free" edge is more likely to be a stale price, a palpable
#: error the book will void, or two markets that are not the same contract —
#: than it is to be money.  Real cross-book arbitrage on a liquid market lives in
#: the fractions of a percent to low single digits.  Flagged, not hidden: the
#: judgement of whether an 8% edge is real belongs to whoever reads the report.
IMPLAUSIBLE_MARGIN = 0.05

#: Above *this*, the position is refused outright rather than flagged, because no
#: reading of it is an arbitrage.  Two books do not price the same two-way market
#: 25% apart; what produces a number like that is a leg that is not the bet it
#: claims to be — most often a selection mapped to the wrong participant, which
#: prices the favourite as the underdog and leaves both legs backing the same
#: competitor.  The captured tennis slate contains exactly that: two books whose
#: home/away price assignment for one match is inverted, showing a 54% "edge" on
#: two perfectly ordinary prices.  Reporting that as money, even with a warning
#: note attached, is the single most expensive thing this module could do.
REFUSE_MARGIN = 0.25

#: Void asymmetry on a shortened or abandoned contest, per sport.  A moneyline
#: has action once a book's minimum has been played and every book agrees on that
#: minimum; the derived markets do not have that agreement, so a position built
#: from them is not void-symmetric even when the prices are.
_VOID_RISK: dict[Sport, str] = {
    Sport.BASEBALL: (
        "full-game {market} needs all nine innings for action, and books disagree on "
        "rain-shortened games (some void both sides, some settle an outcome already "
        "determined) — unlike the moneyline, which has action after five innings "
        "everywhere, so this position is not void-symmetric"
    ),
    Sport.SOCCER: (
        "full-game {market} on an abandoned match is voided by some books and settled "
        "by others once the outcome is already determined, so the two legs may not void "
        "together"
    ),
}
_VOID_RISK_DEFAULT = (
    "full-game {market} on a suspended or abandoned game is not settled the same way by "
    "every book, so confirm both legs void together before treating this as risk-free"
)


# ── the trivial part: the arithmetic ─────────────────────────────────────────


def net_decimal(quote: Quote, commissions: Mapping[str, Commission] | None = None) -> float:
    """The price this quote actually pays, after its venue's charge.

    A sportsbook's margin is already inside the number it publishes, so this is
    the identity for one.  An exchange's is not: Matchbook quotes 3.00 and pays
    2.96 after 2% of winnings, and Kalshi adds a per-contract fee to the price
    you pay.  Every comparison and every payout below uses this rather than
    :attr:`Quote.decimal_odds`, because on the venues that charge, the quoted
    number is not the one you get — and the gap is the same size as the edge
    being looked for.
    """
    return commission_for(quote.source, commissions).net_decimal(quote.decimal_odds)


def _net_implied(quote: Quote, commissions: Mapping[str, Commission] | None = None) -> float:
    return 1.0 / net_decimal(quote, commissions)


def arb_margin(decimal_odds: Sequence[float]) -> float:
    """Edge on a complete set of mutually exclusive outcomes.

    Returns ``1 - sum(1/d)``.  Positive means an arbitrage exists: the outcomes
    can be covered for less than the amount any one of them returns.  Note this
    is the *margin*, not the return on stake — see :func:`arb_roi`.
    """
    _require_prices(decimal_odds)
    return 1.0 - sum(1.0 / odds for odds in decimal_odds)


def arb_roi(decimal_odds: Sequence[float]) -> float:
    """Guaranteed return per unit staked, with ideal (unrounded) stakes.

    ``margin`` is the fraction of the *payout* kept; this is the fraction of the
    *stake* gained.  They differ, and confusing them overstates a 2% edge as
    2% ROI when it is 2.04%.
    """
    _require_prices(decimal_odds)
    total = sum(1.0 / odds for odds in decimal_odds)
    return 1.0 / total - 1.0


def _require_prices(decimal_odds: Sequence[float]) -> None:
    """Reject inputs that are not prices, rather than returning nonsense."""
    if not decimal_odds:
        raise ValueError("no odds supplied")
    for odds in decimal_odds:
        if odds <= 1.0:
            raise ValueError(f"decimal odds must exceed 1.0, got {odds}")


def stake_split(decimal_odds: Sequence[float], total_stake: float) -> list[float]:
    """Split *total_stake* so every outcome returns the same amount."""
    _require_prices(decimal_odds)
    if total_stake <= 0:
        raise ValueError(f"total_stake must be positive, got {total_stake}")
    total_implied = sum(1.0 / odds for odds in decimal_odds)
    return [total_stake * (1.0 / odds) / total_implied for odds in decimal_odds]


def floor_maximising_split(
    multipliers: Sequence[Sequence[float]], total_stake: float
) -> list[float] | None:
    """The two-leg split that maximises the *worst* outcome, exactly.

    :func:`stake_split` equalises the two full-win returns, which maximises the
    floor only when every outcome pays one leg its full price and the other
    nothing.  A quarter (Asian split) line does not: half the stake rides each
    neighbouring line, so the middle outcome pays half of the surviving side's
    profit, and the floor is ``min(full_win_a, full_win_b, half)`` — a different
    function with a different maximum.  Measured: soccer total 2.75 priced 2.10
    against 2.10 reported ``+2.50 on 100`` at 50/50 where 51/49 guarantees
    **+2.90**.  200 of the captured slate's lines are quarter lines.

    *multipliers* is one row per outcome giving what each leg returns per unit
    staked.  With two legs and ``s`` on the first, each outcome's profit is a
    straight line in ``s``, so the floor is a concave piecewise-linear function
    and its maximum is at an endpoint or where two of those lines cross.  Both
    sets are small and exact, so there is nothing to search and nothing to tune.

    Returns ``None`` when there is nothing to choose between — no outcomes, or a
    market that is not two-legged.
    """
    if total_stake <= 0 or any(len(row) != 2 for row in multipliers):
        return None
    # profit(s) = s * first + (total - s) * second - total
    lines = [
        (first - second, total_stake * (second - 1.0)) for first, second in multipliers
    ]
    if not lines:
        return None
    marks = {0.0, total_stake}
    for index, (slope_a, intercept_a) in enumerate(lines):
        for slope_b, intercept_b in lines[index + 1 :]:
            if abs(slope_a - slope_b) < _EPSILON:
                continue
            crossing = (intercept_b - intercept_a) / (slope_a - slope_b)
            if 0.0 <= crossing <= total_stake:
                marks.add(crossing)
    best = max(
        marks, key=lambda s: min(slope * s + intercept for slope, intercept in lines)
    )
    return [best, total_stake - best]


def stake_candidates(
    ideal: Sequence[float], total_stake: float, increment: float
) -> list[list[float]]:
    """Every whole-unit allocation that could be the best one.

    The caller picks between them by the profit floor they produce, which is the
    objective that decides whether a position is risk-free at all — and which
    only the caller can compute, because it depends on the settlement model, the
    commissions and whether the line can land on itself.  This function's job is
    to make sure the best allocation is *in the list*.

    At the optimum some leg is **binding**: it is the one whose return sets the
    guarantee.  Fix that leg at *k* units and the guarantee follows from it; every
    other leg then needs the fewest units whose own return clears that guarantee,
    which is a ceiling.  So sweeping (binding leg, k) enumerates the shape the
    optimum has to have, and no allocation outside that sweep can beat one inside
    it.  ``ideal`` is enough to do this without the odds: ``ideal_i * d_i`` is the
    same number for every leg, so "leg *i* returns at least *R*" is exactly
    "``s_i / ideal_i`` is at least *t*" for one common *t*.

    **Spending the whole bankroll is not the objective**, and requiring it cost
    real money.  Leftover units used to be handed out one per leg, which made the
    pure-floors allocation unreachable whenever anything was left over — so a
    spare unit was forced onto some leg, and it lands on the leg that is *not*
    setting the floor, where it buys nothing and is subtracted from every outcome.
    On an ordinary two-book moneyline (1.88 against 2.35) the engine staked 56/44
    and reported ``+3.40 on 100`` where 55/44 — one unit left unstaked —
    guarantees **+4.40**.  Measured over randomised realistic pairs: 69% of
    positions understated, and in the thin band where cross-book edges actually
    live, 257 of 593 genuinely risk-free positions were *refused outright* as
    ``rounding_destroys_edge``.  ``total_stake`` is the bankroll a position may
    use, not a quota it has to spend.

    The sweep replaced a rounding box around ``ideal``.  A box cannot reach the
    optimum because the optimum is often at a different *total*, not a different
    split of the same total — and a box widened until it usually did would be a
    tuned constant standing where an exact argument fits.

    ``units_total`` floors rather than rounds, so the position never exceeds the
    bankroll it was given.  Rounding up lets the allocation *overspend* when the
    increment does not divide the bankroll — 50/50 of 100 in units of 6 becomes
    54/48, which is 102.

    An allocation that leaves a leg at zero is not a position, so those are
    dropped — unless every one of them does, which is the sub-unit bankroll the
    caller's own guards refuse with a reason.
    """
    if increment <= 0:
        return [list(ideal)]
    units_total = int(total_stake / increment + 1e-9)
    seen: set[tuple[int, ...]] = set()
    corners: list[tuple[int, ...]] = []
    for leg, share in enumerate(ideal):
        if share <= 0:
            continue
        for units in range(1, int(share / increment + 1e-9) + 2):
            # ``units`` on the binding leg fixes the guarantee; every other leg
            # takes the fewest units that clear it.
            scale = units * increment / share
            counts = tuple(
                units
                if other == leg
                else math.ceil(scale * value / increment - 1e-9)
                for other, value in enumerate(ideal)
            )
            if sum(counts) > units_total or counts in seen:
                continue
            seen.add(counts)
            corners.append(counts)
    playable = [counts for counts in corners if all(count >= 1 for count in counts)]
    if not corners:
        # The bankroll is too small for any leg to clear a whole unit on its own
        # share, so the sweep has nothing to offer.  Hand back the floors plus the
        # leftover units one per leg, which is what this function used to do
        # always: a bankroll of exactly one increment still has to reach the
        # caller as a position it can refuse *with a reason* rather than as a
        # zero-stake nothing.
        floors = [int(value / increment + 1e-9) for value in ideal]
        remainder = max(units_total - sum(floors), 0)
        if remainder == 0:
            corners = [tuple(floors)]
        else:
            corners = [
                tuple(
                    floor + (1 if index in extra else 0)
                    for index, floor in enumerate(floors)
                )
                for extra in itertools.combinations(
                    range(len(ideal)), min(remainder, len(ideal))
                )
            ]
    return [
        [count * increment for count in counts] for counts in (playable or corners)
    ]


# ── settlement model ─────────────────────────────────────────────────────────

WIN = "win"
PUSH = "push"
LOSE = "lose"

#: Half the stake wins and half is refunded (or half loses and half is refunded).
#: These exist only for quarter lines, where the bet really is two half-stake bets
#: at the two adjacent lines.
HALF_WIN = "half_win"
HALF_LOSE = "half_lose"


def _return_multiplier(result: str, decimal_odds: float) -> float:
    """What one unit staked returns under *result*, stake included."""
    if result == WIN:
        return decimal_odds
    if result == HALF_WIN:
        # Half the stake wins at the full price; the other half is refunded.
        return (decimal_odds + 1.0) / 2.0
    if result == PUSH:
        return 1.0
    if result == HALF_LOSE:
        return 0.5
    return 0.0


#: Line granularities this module can settle.
LINE_WHOLE = "whole"
LINE_HALF = "half"
LINE_QUARTER = "quarter"
LINE_UNSUPPORTED = "unsupported"


def line_granularity(line: float) -> str:
    """Classify a line by what it can do at settlement.

    * ``whole`` — the market can land exactly on it and refund both sides.
    * ``half`` — cannot be landed on; the ubiquitous -1.5 / 8.5 case.
    * ``quarter`` — the stake splits between the two adjacent lines, so landing
      on the whole one refunds half the stake and settles the other half.
    * ``unsupported`` — anything else.  Named rather than silently treated as a
      half line, because "no push outcome exists" is precisely the assumption
      that turns a mis-settled market into a reported guarantee.
    """
    fraction = abs(line) % 1.0
    for value, granularity in (
        (0.0, LINE_WHOLE),
        (0.25, LINE_QUARTER),
        (0.5, LINE_HALF),
        (0.75, LINE_QUARTER),
        (1.0, LINE_WHOLE),
    ):
        if abs(fraction - value) < 1e-9:
            return granularity
    return LINE_UNSUPPORTED


def canonical_line(quote: Quote) -> float | None:
    """The market's line in one fixed orientation, independent of selection.

    Spreads are stated per side (home -1.5, away +1.5), so the raw field cannot
    be used to decide whether two rows are opposite halves of the same market.
    Expressing every spread from the home team's perspective makes that
    decidable.  Totals are already shared by both sides.
    """
    if quote.line is None:
        return None
    if quote.market is Market.SPREAD:
        return quote.line if quote.selection is Selection.HOME else -quote.line
    return quote.line


def _sides(market: Market) -> tuple[Selection, Selection]:
    if market is Market.SPREAD:
        return (Selection.HOME, Selection.AWAY)
    return (Selection.OVER, Selection.UNDER)


def contract_shape(
    sport: Sport, market: Market, period: Period, rows: Iterable[Quote]
) -> frozenset[Selection]:
    """Which outcomes one book's version of this market settles on.

    This is the *contract*, not an inventory of what happened to be available:
    it is derived from whether the book prices a tie as its own selection, so a
    book whose away price is suspended is still recognised as a two-way market
    rather than as a one-outcome market.  Getting that distinction wrong either
    invents incompatibility or, worse, hides it.

    Only a moneyline can differ between books, and only in a window where a draw
    is a priced outcome at all — which is a fact about the sport, not about the
    period alone: a hockey ``REGULATION`` moneyline is three-way while the same
    fixture's ``FULL_GAME`` moneyline is two-way, because the shootout decides it.
    """
    if market is Market.MONEYLINE:
        if draw_is_priced(sport, period) and any(
            row.selection is Selection.DRAW for row in rows
        ):
            return frozenset({Selection.HOME, Selection.AWAY, Selection.DRAW})
        return frozenset({Selection.HOME, Selection.AWAY})
    if market is Market.SPREAD:
        return frozenset({Selection.HOME, Selection.AWAY})
    return frozenset({Selection.OVER, Selection.UNDER})


def _landing_outcome(
    sport: Sport, market: Market, period: Period, line: float | None
) -> tuple[str, dict[Selection, str]] | None:
    """The extra outcome created by the market landing on its own line.

    ``None`` means the market cannot land on its line, so there is no third
    outcome to cover.  Assuming that wrongly is expensive in both directions: a
    push that is not enumerated overstates the guarantee by the whole margin,
    while a push that cannot happen understates it as zero.
    """
    if line is None or market is Market.MONEYLINE:
        return None
    granularity = line_granularity(line)
    if granularity in (LINE_HALF, LINE_UNSUPPORTED):
        # A half line cannot be landed on.  An unsupported granularity is refused
        # by the caller, so it must not silently produce "no push outcome" here.
        return None

    left, right = _sides(market)
    if granularity == LINE_WHOLE:
        # A spread of zero is the exception: it pushes only if the contest can end
        # level.  A full nine-inning game cannot — extra innings are played until
        # somebody wins — while an NFL game can, and a soccer 90 minutes can.
        if market is Market.SPREAD and abs(line) < 1e-9 and not tie_possible(sport, period):
            return None
        return ("push", {left: PUSH, right: PUSH})

    # Quarter line: the stake is two half-stake bets, at the whole line and at the
    # half line either side of it.  Only the whole one can be landed on, and when
    # it is, that half refunds while the other half settles normally.
    whole = float(round(line))
    if market is Market.SPREAD and abs(whole) < 1e-9 and not tie_possible(sport, period):
        return None
    if market is Market.SPREAD:
        # Home is helped by a *larger* line, so the surviving half wins when it
        # sits above the whole number the margin landed on.
        half_winner, half_loser = (
            (Selection.HOME, Selection.AWAY) if whole < line else (Selection.AWAY, Selection.HOME)
        )
    else:
        # Over wins when the total exceeds its line, so its surviving half wins
        # when the landing whole number sits above the quarter line.
        half_winner, half_loser = (
            (Selection.OVER, Selection.UNDER) if whole > line else (Selection.UNDER, Selection.OVER)
        )
    return (f"half_push_at_{whole:g}", {half_winner: HALF_WIN, half_loser: HALF_LOSE})


def settlement_outcomes(
    sport: Sport,
    market: Market,
    period: Period,
    line: float | None,
    shape: frozenset[Selection],
) -> list[tuple[str, dict[Selection, str]]]:
    """Every way the market can settle, and what each selection does in it.

    This is the whole risk model.  If an outcome is missing from this list, the
    profit floor computed from it is wrong.
    """
    if market is Market.MONEYLINE:
        # Each selection wins in its own outcome and loses in the others. A
        # three-way market's draw is a real losing outcome for home and away,
        # which is exactly why it cannot be paired against a two-way market.
        outcomes = [
            (selection.value, {other: WIN if other is selection else LOSE for other in shape})
            for selection in sorted(shape, key=lambda s: s.value)
        ]
        if Selection.DRAW not in shape and tie_possible(sport, period):
            # The window can end level and the draw is not one of the priced
            # selections, so a level result voids every leg. This is the NFL case:
            # a game still tied after overtime refunds both sides of a two-way
            # moneyline, and no field on either row mentions it. It is also the
            # reading forced on a two-way market in a draw-pricing window, which
            # the detector refuses outright — see ``ambiguous_tie_settlement`` —
            # because there the alternative reading is that the tie *loses* both
            # legs.
            outcomes.append(("push", {selection: PUSH for selection in shape}))
        return outcomes

    if market is Market.SPREAD:
        outcomes = [
            ("home_covers", {Selection.HOME: WIN, Selection.AWAY: LOSE}),
            ("away_covers", {Selection.HOME: LOSE, Selection.AWAY: WIN}),
        ]
    else:
        outcomes = [
            ("over", {Selection.OVER: WIN, Selection.UNDER: LOSE}),
            ("under", {Selection.OVER: LOSE, Selection.UNDER: WIN}),
        ]
    landing = _landing_outcome(sport, market, period, line)
    if landing is not None:
        outcomes.append(landing)
    return outcomes


# ── grouping ─────────────────────────────────────────────────────────────────

#: Identity of one *comparable market*: the thing whose two sides can be bet
#: against each other at different books.
#:
#: ``sport`` is deliberately absent, and its absence is checked rather than
#: assumed: ``event_key`` is built from namespaced participant keys
#: (``MLB-PHI``, ``SOCCER-arsenal``), so two sports cannot collide in one group
#: through legitimate data, and a row whose ``sport`` field is *mislabelled*
#: should produce a counted refusal rather than silently drop out of its own
#: group.  :func:`_examine_group` enforces that.
#:
#: ``is_alternate`` is absent for a different reason: a bet at 8.5 is a bet at 8.5
#: whichever screen the book showed it on, and refusing to compare a main line
#: against another book's alternate line at the same number would discard real
#: positions.  What must not happen is two rows for one selection at one book at
#: the same alternate status, and that is refused explicitly below.
MarketGroup = tuple[str, Market, Period, Side | None, float | None]


def group_key(quote: Quote) -> MarketGroup:
    return (
        quote.event_key,
        quote.market,
        quote.period,
        quote.side,
        canonical_line(quote),
    )


@dataclass(frozen=True)
class ArbLeg:
    """One bet in an arbitrage position."""

    quote: Quote
    stake: float
    net_decimal_odds: float | None = None
    """What this leg actually pays, after the venue's commission.

    Carried alongside the quoted price rather than replacing it, because the two
    are both true and are needed for different things: you place the bet at the
    *quoted* price and you are paid at the *net* one.  ``None`` means "the same
    as quoted", which is every sportsbook.
    """

    @property
    def source(self) -> str:
        return self.quote.source

    @property
    def selection(self) -> Selection:
        return self.quote.selection

    @property
    def decimal_odds(self) -> float:
        """The price the venue publishes — what you place the bet at."""
        return self.quote.decimal_odds

    @property
    def net_odds(self) -> float:
        """The price you are actually paid — what the arithmetic uses."""
        return self.net_decimal_odds if self.net_decimal_odds is not None else self.quote.decimal_odds

    @property
    def pays_commission(self) -> bool:
        return abs(self.net_odds - self.quote.decimal_odds) > 1e-12

    @property
    def payout(self) -> float:
        """Total returned if this leg wins, stake included and commission taken."""
        return self.stake * self.net_odds

    def describe(self) -> str:
        line = "" if self.quote.line is None else f" {self.quote.line:+g}"
        net = f" (net {self.net_odds:.4f})" if self.pays_commission else ""
        return (
            f"{self.quote.source} {self.selection.value}{line} "
            f"@ {self.quote.decimal_odds:.4f} ({self.quote.american_odds:+d}){net} "
            f"stake {self.stake:.2f}"
        )


@dataclass(frozen=True)
class Opportunity:
    """A position that cannot lose, with the arithmetic to prove it."""

    event_key: str
    sport: Sport
    home_team: str
    away_team: str
    commence_time: datetime
    market: Market
    period: Period
    side: Side | None
    line: float | None
    """Canonical line — home perspective for spreads."""
    legs: tuple[ArbLeg, ...]
    total_stake: float
    outcome_profits: tuple[tuple[str, float], ...]
    """Profit in every settlement outcome, after stake rounding.  The floor of
    these is the only profit figure that is actually guaranteed."""
    max_total_stake: float | None
    """Largest bankroll the books' stated limits allow, when they state any."""
    notes: tuple[str, ...] = ()

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(leg.source for leg in self.legs)

    @property
    def sum_implied(self) -> float:
        """Total implied probability at the prices actually paid.

        Net of commission, which is the only reading that can be trusted once
        exchanges are in the mix: they quote tighter than sportsbooks, so an
        exchange leg is exactly what drags a cross-book sum below 1.0, and a
        margin measured on the quoted price would report a stream of edges that
        the venue's own charge has already eaten.
        """
        return sum(1.0 / leg.net_odds for leg in self.legs)

    @property
    def margin(self) -> float:
        """Ideal-stake edge: the headline number."""
        return 1.0 - self.sum_implied

    @property
    def roi(self) -> float:
        """Ideal-stake return on bankroll."""
        return 1.0 / self.sum_implied - 1.0

    @property
    def guaranteed_profit(self) -> float:
        """Worst case over every settlement outcome, after rounding.

        This is what the position actually makes.  It is lower than
        ``roi * total_stake`` whenever rounding bites, it is zero on any market
        that can push, and it is reduced — not zeroed — on a quarter line, where
        only half the stake is refunded.
        """
        return min(profit for _, profit in self.outcome_profits)

    @property
    def can_push(self) -> bool:
        """Can every leg be refunded, leaving the position at zero?"""
        return any(label == "push" for label, _ in self.outcome_profits)

    @property
    def can_half_push(self) -> bool:
        """Is this a quarter line, where landing on the whole number refunds half?"""
        return any(label.startswith("half_push") for label, _ in self.outcome_profits)

    @property
    def is_risk_free(self) -> bool:
        """Does this position lose money in no outcome at all?

        A pushable market whose floor is exactly zero still qualifies: it can
        fail to make anything, but it cannot lose.  A position whose floor has
        been driven negative by stake rounding does not qualify, and is not an
        arbitrage however good its headline margin looks.
        """
        return self.guaranteed_profit >= -_EPSILON

    @property
    def observed_at(self) -> datetime:
        """Oldest leg — the position is only as fresh as its stalest price."""
        return min(leg.quote.observed_at for leg in self.legs)

    def describe(self, *, leg_note: Callable[[str], str] | None = None) -> str:
        line = "" if self.line is None else f" @ {self.line:+g}"
        side = f" ({self.side.value})" if self.side else ""
        head = (
            f"{self.event_key} {self.sport.value} "
            f"{self.market.value}/{self.period.value}{side}{line}: "
            f"margin {self.margin * 100:.2f}%, guaranteed "
            f"{self.guaranteed_profit:+.2f} on {self.total_stake:.0f}"
        )
        # ``leg_note`` keeps jurisdiction vocabulary out of this module: the
        # caller knows which state's marking applies, this class only knows how
        # a position prints.
        rows = [
            f"    {leg.describe()}{leg_note(leg.source) if leg_note else ''}"
            for leg in self.legs
        ]
        rows += [
            "    outcomes: "
            + ", ".join(f"{label} {profit:+.2f}" for label, profit in self.outcome_profits)
        ]
        if self.max_total_stake is not None:
            rows.append(f"    limit-capped bankroll: {self.max_total_stake:.2f}")
        rows += [f"    note: {note}" for note in self.notes]
        return "\n".join([head, *rows])


@dataclass(frozen=True)
class Diagnostic:
    """A market that looked arbitrageable but was rejected, and why.

    Kept because "no opportunities found" and "opportunities found and thrown
    away for a stated reason" are different facts, and only one of them means
    the detector is working.
    """

    event_key: str
    market: Market
    period: Period
    code: str
    detail: str
    sport: Sport | None = None


@dataclass
class ArbReport:
    opportunities: list[Opportunity]
    diagnostics: list[Diagnostic]
    group_count: int = 0
    comparable_group_count: int = 0
    """Groups priced by at least two books with a compatible contract."""

    def summary(self) -> str:
        return (
            f"{len(self.opportunities)} opportunit"
            f"{'y' if len(self.opportunities) == 1 else 'ies'} from "
            f"{self.comparable_group_count} cross-book markets "
            f"({self.group_count} total, {len(self.diagnostics)} rejected)"
        )


# ── detection ────────────────────────────────────────────────────────────────


#: Key under which a counterparty group applies to every competition rather
#: than one named one.  No league key can collide with it.
EVERY_LEAGUE = "*"


def merge_counterparty_groups(
    *tables: Mapping[str, list[frozenset[str]]] | None,
) -> dict[str, list[frozenset[str]]]:
    """Union several ``league -> groups`` tables into one gate.

    Evidence only ever accumulates: a pair recorded as one counterparty by one
    measurement stays one counterparty when another measurement, taken from
    narrower rows, could not see it.  The narrow case is real — a run collected
    under ``--sport tennis`` stores none of the MLB rows the mirror was measured
    on, so re-measuring from the stored rows alone reopened the gate and
    published a "guaranteed" position with both legs at one operator.

    Intentional failover pairs (:mod:`src.redundancy`) are dropped even when a
    pre-fix run recorded them: re-introducing ``{primary, an_*}`` into the
    transitive union-find next to a measured ``{an_*, third}`` mirror collapses
    the primary with the third book on re-analysis.
    """
    merged: dict[str, list[frozenset[str]]] = {}
    for table in tables:
        for league, groups in (table or {}).items():
            bucket = merged.setdefault(league, [])
            for group in groups:
                if len(group) == 2:
                    a, b = tuple(group)
                    if is_redundant_pair(a, b):
                        continue
                if group not in bucket:
                    bucket.append(group)
    return merged


def counterparty_groups(
    quotes: Sequence[Quote],
    *,
    mirrors: Sequence[Agreement] | None = None,
) -> dict[str, list[frozenset[str]]]:
    """``league -> source groups that are one book``, measured from the rows.

    Kept here rather than in :mod:`src.distinctness` so that every entry point
    into detection shares one answer: the collector reports the same pairs as
    findings, and a re-analysis of the stored rows reaches the same verdict
    without the caller doing anything.

    *mirrors*, when supplied, is the already-measured :func:`src.distinctness.find_mirrors`
    result for *quotes*.  The collector measures once and shares the list with
    the filing check; recomputing it is a second full pairwise scan of the slate.

    A mirrored pair is filed under :data:`EVERY_LEAGUE`, **not** under the
    leagues the mirror was measured in.  Those are two different questions, and
    conflating them was the most expensive defect this gate has had:

    :data:`src.distinctness.MIN_SHARED_SELECTIONS` says how much overlap it
    takes to *establish* that two sources are one counterparty — 20 shared
    selections, because a handful agreeing perfectly is what a one-fixture
    overlap looks like.  It says nothing about *where* that fact then applies.
    Two licences of one operator are one counterparty in every market they
    both quote; they do not become independent in a competition where this
    pipeline happens to hold fewer than 20 shared prices.

    Filing per measured league meant exactly that.  Two Kambi tenants
    byte-identical across 25 ITF fixtures were gated in ITF and joined as
    independent books in WTA, where they shared 18 selections and one tenant's
    cache was stale — reported as ``margin 3.26%, guaranteed +3.35``, both legs
    at the same book, on a run with no errors and no diagnostic. Worse, a pair
    that is a mirror by its **overall** rate but has no single league above the
    floor contributed nothing at all: 139 of 140 prices identical, verdict
    MIRROR, gate empty.  On the committed slate every WNBA pairing shares 2-14
    selections, so no pair could ever have been gated there however identical.

    This is what :attr:`src.distinctness.Agreement.verdict` already says — "a
    mirror found anywhere stands, because the evidence for it does not stop
    being evidence when other markets disagree".  The verdict implemented it;
    the gate did not.

    Intentional failover pairs from :mod:`src.redundancy` are **not** filed
    here, even when distinctness measures them as mirrors.  Union-find is
    transitive: filing ``{betrivers, an_betrivers}`` next to a measured
    ``{an_betrivers, leovegas}`` tennis mirror would collapse BetRivers and
    LeoVegas into one counterparty on every sport.  Those pairs are enforced
    as non-transitive "cannot trade against" edges in
    :func:`_independent_source_count` instead.
    """
    if mirrors is None:
        from src.distinctness import find_mirrors

        pairs: Sequence[Agreement] = find_mirrors(quotes)
    else:
        pairs = mirrors
    groups: dict[str, list[frozenset[str]]] = defaultdict(list)
    for pair in pairs:
        if is_redundant_pair(pair.source_a, pair.source_b):
            continue
        groups[EVERY_LEAGUE].append(frozenset({pair.source_a, pair.source_b}))
    return dict(groups)


def _order_driven_sources() -> frozenset[str]:
    """Venues whose rows exist only because somebody left liquidity resting.

    Read off the registry, which is where that fact lives, rather than guessed
    from the data — and imported here rather than at module scope because
    :mod:`src.validation` imports this module.
    """
    from src.sources import registry

    return frozenset(
        key for key, entry in registry.BY_KEY.items() if entry.kind.has_stated_liquidity
    )


def describe_age(age: timedelta) -> str:
    """A duration in the largest unit that keeps it readable."""
    seconds = int(age.total_seconds())
    if seconds < 90:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    days = hours // 24
    return f"{days} day{'' if days == 1 else 's'}"


def find_opportunities(
    quotes: Sequence[Quote],
    *,
    total_stake: float = DEFAULT_TOTAL_STAKE,
    min_margin: float = DEFAULT_MIN_MARGIN,
    stake_increment: float = STAKE_INCREMENT,
    max_observation_spread: timedelta = MAX_OBSERVATION_SPREAD,
    require_distinct_sources: bool = True,
    as_of: datetime | None = None,
    commissions: Mapping[str, Commission] | None = None,
    one_counterparty: Mapping[str, Sequence[frozenset[str]]] | None = None,
    order_book_sources: Collection[str] | None = None,
    view_only_sources: Collection[str] | None = None,
) -> ArbReport:
    """Find every risk-free position in one run's worth of quotes.

    *as_of* is the moment the answer is being asked about — normally now.  Legs
    are required to be close together in time, but that alone does not make a
    position takeable: re-analysing a stored run from last month yields legs that
    agree with each other perfectly and describe games that have already been
    played.  Passing *as_of* excludes those instead of reporting settled games as
    free money.  Left as ``None``, no such gate is applied, which is what a
    historical study of the stored data wants.

    *commissions* maps a source key to what that venue charges; left ``None`` it
    is :data:`src.commission.COMMISSIONS`, the real table.  Pass ``{}`` to price
    every venue as charging nothing, which is what a test of the pure arithmetic
    wants and what a live run must never do.

    *view_only_sources* defaults to the process's active jurisdiction. Reports
    pass the stored run jurisdiction explicitly so historical PA and IL runs do
    not inherit whichever state happens to be selected today.

    *one_counterparty* names groups of source keys that are the same
    counterparty, per league — what :mod:`src.distinctness` measures.  Distinct
    *sources* is not the same test as distinct *counterparties*, and the live
    slate shows why the distinction has to be per league rather than per source:
    BetRivers and LeoVegas disagree about baseball, hockey and most soccer, and
    are byte-identical across all of tennis and the Bundesliga.  Banning the pair
    outright would throw away real coverage; ignoring it would report an
    arbitrage between one book and itself in the competitions where it is one
    feed.  Sources named together here count as one book for
    *require_distinct_sources* in the leagues they are named for, and nowhere
    else.

    Left ``None`` it is **measured from the quotes**, so every caller gets the
    gate without having to remember it.  That default is the point: the first
    version took the map as a required argument, the live path passed it and the
    re-analysis path did not, and the same rows produced two different answers
    depending on which command asked.  Pass ``{}`` to compare source keys alone,
    which is what a test of the pure arithmetic wants.
    """
    if total_stake < stake_increment:
        # A bankroll under one betting unit cannot hold a position at all.
        #
        # ``stake_candidates`` floors every leg to zero, every settlement
        # outcome then returns exactly zero, ``is_risk_free`` (floor >= 0)
        # passes, and ``rounding_destroys_edge`` never fires — so ``--stake
        # 0.5`` printed "margin 9.09%, guaranteed +0.00 on 0" with both legs at
        # stake 0.00.  The argument parser only refuses values at or below zero,
        # and it is not its business to know the increment.
        raise ValueError(
            f"total_stake {total_stake:g} is below one {stake_increment:g}-unit stake, "
            "so every leg would round to zero and no position could be placed"
        )
    # Consensus / opening columns (``an_open``) are on the board for context
    # only — never a leg, never a counterparty in the measured gate.
    view_only = (
        VIEW_ONLY_SOURCES
        if view_only_sources is None
        else frozenset(view_only_sources)
    )
    if view_only:
        quotes = [quote for quote in quotes if quote.source not in view_only]

    if one_counterparty is None:
        one_counterparty = counterparty_groups(quotes)
    if order_book_sources is None:
        order_book_sources = _order_driven_sources()
    order_driven = frozenset(order_book_sources)
    report = ArbReport(opportunities=[], diagnostics=[])

    grouped: dict[MarketGroup, list[Quote]] = defaultdict(list)
    for quote in quotes:
        grouped[group_key(quote)].append(quote)
    report.group_count = len(grouped)

    for key, rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        event_key, market, period, side, line = key
        _examine_group(
            event_key=event_key,
            market=market,
            period=period,
            side=side,
            line=line,
            rows=rows,
            report=report,
            total_stake=total_stake,
            min_margin=min_margin,
            stake_increment=stake_increment,
            max_observation_spread=max_observation_spread,
            require_distinct_sources=require_distinct_sources,
            as_of=as_of,
            commissions=commissions,
            counterparties=_counterparties(rows, one_counterparty),
            order_driven=order_driven,
        )

    # Ranked by the money actually guaranteed, not by the headline margin. A
    # pushable market can show a 9% margin against a floor of zero, and ordering
    # by margin would list it above a 2% position that really pays.
    report.opportunities.sort(key=lambda o: (o.guaranteed_profit, o.margin), reverse=True)
    return report


def _largest_regime(sources: Sequence[str]) -> list[str]:
    """The biggest group of sources that settle a non-event the same way.

    Ties break on the regime name so the answer does not depend on row order.
    """
    from src.settlement import regime_for

    grouped: dict[str, list[str]] = defaultdict(list)
    for source in sources:
        grouped[regime_for(source).value].append(source)
    if not grouped:
        return []
    best_regime = min(grouped, key=lambda name: (-len(grouped[name]), name))
    return grouped[best_regime]


def _counterparties(
    rows: Sequence[Quote],
    one_counterparty: Mapping[str, Sequence[frozenset[str]]] | None,
) -> dict[str, str]:
    """``source key -> counterparty id`` for the sources in one market group.

    A source not named in any group is its own counterparty, which is the case
    for every venue on an ordinary slate.  Where a group applies, its members
    share one id — the alphabetically first key in the *connected component*, so
    the id is stable across runs and does not depend on which member happened to
    price this market.

    The component, not the group.  The measurement is pairwise, so one operator
    running three names arrives here as three overlapping pairs — ``{a, b}``,
    ``{a, c}``, ``{b, c}`` — and assigning each pair its own minimum in turn
    lets the last one processed overwrite the first: ``a`` keeps id ``a`` while
    ``b`` is rewritten to ``b`` by the ``{b, c}`` pair, and the gate between
    ``a`` and ``b`` reopens.  Being one counterparty is transitive; the ids have
    to be too.

    Groups filed under :data:`EVERY_LEAGUE` apply here whatever this market's
    competition is; groups filed under a league name apply only to that one.
    The measured gate uses the first — see :func:`counterparty_groups` — and the
    second exists for a caller passing a mapping of its own.

    The league is read off the rows rather than passed in: every row of a market
    group is the same fixture, and where two sources disagree about its league
    that disagreement is its own finding, so taking the modal spelling here
    cannot mask anything that is not already reported.
    """
    if not one_counterparty:
        return {}
    leagues = Counter(row.league for row in rows)
    # Ties broken by name, matching ``src.distinctness._leagues`` exactly.
    # ``most_common`` breaks them by insertion order, so the same rows in a
    # different order filed the gate under one league and read it back under
    # another — and the mirror gate reopened on whichever way the rows arrived.
    league = (
        min(leagues.items(), key=lambda item: (-item[1], item[0]))[0] if leagues else ""
    )
    parent: dict[str, str] = {}

    def find(source: str) -> str:
        parent.setdefault(source, source)
        while parent[source] != source:
            parent[source] = parent[parent[source]]
            source = parent[source]
        return source

    for group in (*one_counterparty.get(EVERY_LEAGUE, ()), *one_counterparty.get(league, ())):
        if len(group) < 2:
            continue
        members = iter(sorted(group))
        first = find(next(members))
        for source in members:
            other = find(source)
            if other != first:
                # Union onto the lexicographically smaller root, so the id of a
                # component is its smallest member however the pairs arrived.
                low, high = sorted((first, other))
                parent[high] = low
                first = low
    return {source: find(source) for source in parent}


def _uses_both_failover_feeds(sources: Sequence[str]) -> bool:
    """True if an assignment takes legs from both feeds of one failover pair.

    Merging the pair into one counterparty for the *count* still allows each
    key to supply a different leg, which stitches disagreeing dual-feed prices
    into a synthetic book.  Failover must pick one feed per position.
    """
    present = set(sources)
    for primary, secondary in REDUNDANT_PAIRS:
        if primary in present and secondary in present:
            return True
    return False


def _brute_best_assignment(
    *,
    needed: frozenset[Selection],
    eligible: Sequence[str],
    best: dict[str, dict[Selection, Quote]],
    require_distinct_sources: bool,
    commissions: Mapping[str, Commission] | None = None,
    counterparties: Mapping[str, str] | None = None,
) -> tuple[dict[Selection, Quote], float] | None:
    """Exhaustive fallback when the linear switch cannot see a failover escape."""
    selections = sorted(needed, key=lambda s: s.value)
    options = [
        [source for source in eligible if selection in best[source]]
        for selection in selections
    ]
    if any(not sources for sources in options):
        return None
    min_books = 2 if require_distinct_sources else 1
    winner: tuple[dict[Selection, Quote], float] | None = None
    for combination in itertools.product(*options):
        if _uses_both_failover_feeds(combination):
            continue
        if _independent_source_count(combination, counterparties) < min_books:
            continue
        assignment = {
            selection: best[source][selection]
            for source, selection in zip(combination, selections)
        }
        total = sum(
            _net_implied(assignment[selection], commissions) for selection in selections
        )
        if winner is None or total < winner[1]:
            winner = (assignment, total)
    return winner


def _independent_source_count(
    sources: Sequence[str],
    counterparties: Mapping[str, str] | None = None,
) -> int:
    """How many distinct books an assignment actually spans.

    Measured mirrors come from *counterparties*.  Intentional failover pairs
    (:mod:`src.redundancy`) are merged **only among the sources in this
    assignment**, so declaring ``{betrivers, an_betrivers}`` cannot transitively
    collapse BetRivers with LeoVegas through a measured AN tennis mirror.
    """
    if not sources:
        return 0
    behind = counterparties or {}
    # Fast path: no failover key in the assignment — same as the old
    # ``len({who(s)})`` check, which the thirty-source scaling budget assumes.
    if not any(source in _FAILOVER_KEYS for source in sources):
        return len({behind.get(source, source) for source in sources})

    parent: dict[str, str] = {source: source for source in sources}

    def find(source: str) -> str:
        while parent[source] != source:
            parent[source] = parent[parent[source]]
            source = parent[source]
        return source

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            low, high = sorted((a, b))
            parent[high] = low

    ordered = list(sources)
    for index, source in enumerate(ordered):
        for other in ordered[index + 1 :]:
            if behind.get(source, source) == behind.get(other, other):
                union(source, other)
            elif is_redundant_pair(source, other):
                union(source, other)
    return len({find(source) for source in ordered})


def _examine_group(
    *,
    order_driven: frozenset[str] = frozenset(),
    event_key: str,
    market: Market,
    period: Period,
    side: Side | None,
    line: float | None,
    rows: list[Quote],
    report: ArbReport,
    total_stake: float,
    min_margin: float,
    stake_increment: float,
    max_observation_spread: timedelta,
    require_distinct_sources: bool,
    as_of: datetime | None = None,
    commissions: Mapping[str, Commission] | None = None,
    counterparties: Mapping[str, str] | None = None,
) -> None:
    sport = rows[0].sport
    # Who is really behind each source key here.  Two keys belonging to one
    # counterparty are one book for every distinctness test below.
    behind = counterparties or {}
    who = lambda source: behind.get(source, source)  # noqa: E731

    def reject(code: str, detail: str) -> None:
        report.diagnostics.append(
            Diagnostic(
                event_key=event_key,
                market=market,
                period=period,
                code=code,
                detail=detail,
                sport=sport,
            )
        )

    # Every settlement rule below is looked up by sport, so a group holding two
    # sports is a group whose rules are unknowable. It cannot arise from
    # legitimate data — event keys are built from namespaced participant keys —
    # so if it happens, a row's sport is mislabelled and the fix is upstream.
    #
    # The mislabelled *source* is dropped and the rest of the group carries on,
    # rather than the group being abandoned.  Refusing everything punishes the
    # books that were right, and with ten sources one bad label would silently
    # remove a market from comparison entirely.  A genuine tie — no sport with
    # more rows than another — is still refused outright, because then there is
    # no consensus to be an outlier from.
    if len({row.sport for row in rows}) > 1:
        by_sport = Counter(row.sport for row in rows)
        ranked = by_sport.most_common()
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            reject(
                "mixed_sport",
                "rows under one event key disagree about the sport with no majority: "
                + "; ".join(sorted({f"{row.source}: {row.sport.value}" for row in rows}))
                + " — the settlement rules differ by sport, so no position is reported",
            )
            return
        sport = ranked[0][0]
        odd_sources = sorted({row.source for row in rows if row.sport is not sport})
        reject(
            "mixed_sport",
            f"{', '.join(odd_sources)} label this event as "
            + "/".join(sorted({row.sport.value for row in rows if row.sport is not sport}))
            + f" where the other sources say {sport.value}; the settlement rules differ "
            "by sport, so those rows are left out and the rest of the market is still "
            "compared",
        )
        rows = [row for row in rows if row.sport is sport]
        if len(rows) < 2:
            return

    # A line at a granularity the settlement model does not cover would be
    # settled as though it could never land on its own number, which is exactly
    # the assumption that turns a mis-settled market into a reported guarantee.
    if line is not None and line_granularity(line) == LINE_UNSUPPORTED:
        reject(
            "unsupported_line_granularity",
            f"line {line:g} is neither a whole, half nor quarter line, so how it settles "
            "when the market lands on it is not modelled here; no position is reported",
        )
        return

    # The contract each book is offering, judged from *all* its rows for this
    # market including suspended ones: suspending a price does not change which
    # outcomes the market settles on.
    rows_by_source: dict[str, list[Quote]] = defaultdict(list)
    for row in rows:
        rows_by_source[row.source].append(row)
    shapes: dict[str, frozenset[Selection]] = {
        source: contract_shape(sport, market, period, source_rows)
        for source, source_rows in rows_by_source.items()
    }

    # A game that has already started cannot be backed on both sides, however
    # well the prices agree with each other.
    #
    # Judged on the **earliest** time any source gives the fixture, not on row
    # zero's.  Books disagree about a start by minutes as a matter of course and
    # by hours in tennis, so reading one arbitrary row made the answer depend on
    # the order the rows arrived in: the same group, the same clock, and a
    # position reported or not according to which book happened to be first.
    # Earliest is also the safe reading — if any source says it has begun, it has.
    if as_of is not None and rows and min(row.commence_time for row in rows) <= as_of:
        return

    # Only prices you could actually take.
    active = [row for row in rows if row.status is QuoteStatus.ACTIVE]
    if len(active) < 2:
        return

    # Best price per (book, selection). A book may legitimately offer the same
    # bet twice — the primary line and an alternate-line market can sit at the
    # same number — and taking the better of those is correct. Two rows with the
    # *same* alternate flag, however, is a parser fault, and betting the better
    # of them would be betting on the fault.
    best: dict[str, dict[Selection, Quote]] = defaultdict(dict)
    seen: set[tuple[str, Selection, bool]] = set()
    duplicated: set[str] = set()
    for row in active:
        offer = (row.source, row.selection, row.is_alternate)
        if offer in seen:
            duplicated.add(row.source)
        seen.add(offer)
        existing = best[row.source].get(row.selection)
        if existing is None or row.decimal_odds > existing.decimal_odds:
            best[row.source][row.selection] = row
    if duplicated:
        # Blocked, not merely noted. Two prices for one selection at one book, at
        # the same alternate status, means the parse put something in this group
        # that does not belong to it — so at least one of the two is not the bet
        # it claims to be. Taking the better of them is betting on the fault.
        #
        # **That book** is excluded and the others carry on, and the message
        # names it.  This was a group-wide boolean that abandoned the market: one
        # adapter's parser fault destroyed every other book's comparison, and the
        # message did not even say whose fault it was.  It is the identical flaw
        # already fixed for ``source_prices_itself_to_lose`` below, whose comment
        # reads "with ten it costs nine books' worth of comparison every time one
        # adapter has a bad day, which is the opposite of what adding sources is
        # for."
        reject(
            "duplicate_selection",
            f"{', '.join(sorted(duplicated))} published more than one price for the "
            "same selection at the same alternate status, so at least one of them is "
            "mis-grouped; those books are excluded from this market and the rest are "
            "unaffected",
        )
        for source in duplicated:
            best.pop(source, None)
        if len(best) < (2 if require_distinct_sources else 1):
            return

    # A book whose own complete market prices below 1.0 has been mispaired by the
    # parser — validation reports it as an error. Any leg drawn from such a book
    # is not the bet it appears to be, even when the rest of the position is
    # sound, so **that book** is excluded and the others carry on.
    #
    # This used to refuse the whole group.  The code and its own message already
    # disagreed about that: the message said its prices "are mispaired, so none of
    # them are used" — that source's — while the return discarded every source's.
    # With three books it cost a market; with ten it costs nine books' worth of
    # comparison every time one adapter has a bad day, which is the opposite of
    # what adding sources is for.  Judged on the **quoted** prices, not net ones,
    # because the question here is whether the parser paired them correctly and
    # commission is not part of that.
    for source in sorted(best):
        selections = best[source]
        shape = shapes[source]
        if not set(selections) >= set(shape):
            continue
        own_margin = arb_margin([selections[selection].decimal_odds for selection in shape])
        # A sportsbook holds an edge on every market it posts, so a sum below
        # 1.0 there is evidence about the parser.  An order book is not that: it
        # shows what two strangers happened to leave resting, and "crossed" only
        # means anything if somebody could take both sides at a profit — which
        # is a question about prices **after the venue's own commission**.  A
        # Kalshi market resting at 49¢/49¢ sums to 0.98 gross and ~1.015 net of
        # its contract fee, so makers legitimately sit there; the flat one-cent
        # tolerance this used to import deleted the venue from the market for
        # exactly that shape.  ``src.validation`` judges the same boundary the
        # same way — the same *rule* rather than the same constant, which is a
        # stronger form of the agreement the old import bought: two copies of a
        # rule cannot drift apart on a market neither has seen.
        if source in order_driven:
            # Function-level import: ``src.validation`` imports this module at
            # module level, so the constant has to come in here — the same
            # cycle-shaped reason the retired flat floor was imported here too.
            from src.validation import ORDER_BOOK_CROSSING_TOLERANCE

            net_sum = sum(
                _net_implied(selections[selection], commissions) for selection in shape
            )
            crossed_after_fees = net_sum < 1.0 - ORDER_BOOK_CROSSING_TOLERANCE
        else:
            crossed_after_fees = own_margin > _EPSILON
        if crossed_after_fees:
            reject(
                "source_prices_itself_to_lose",
                f"{source} prices every outcome of this market at a "
                f"{own_margin * 100:.2f}% edge against itself, which does not happen — "
                "its prices or lines are mispaired, so none of them are used; the "
                "other books in this market are unaffected",
            )
            del best[source]
            del shapes[source]

    # Every leg must describe the same fixture, the same way round.  Cheap to
    # check and catastrophic to miss: two legs that both back the same team lose
    # the entire bankroll together.
    #
    # Excluded per source against the group's own consensus, rather than the
    # market being abandoned once a candidate position happens to include the odd
    # one out.  Two reasons.  A market abandoned costs every *other* book its
    # comparison, which gets worse the more books there are.  And judging a
    # two-leg position on its own gives a one-against-one tie with no consensus
    # to appeal to, while the group as a whole usually has a clear majority.
    #
    # Compared on the resolved participant keys, never on display names: for an
    # open-roster competition the display name is the book's own spelling, and
    # "Wolves" against "Wolverhampton" would refuse every legitimate soccer and
    # tennis position.
    for source in _fixture_outliers(rows):
        if source not in best:
            continue
        odd = rows_by_source[source][0]
        reject(
            "legs_disagree_on_the_game",
            f"{source} describes this event as {odd.away_participant} @ "
            f"{odd.home_participant} where the other sources disagree — its rows are "
            "left out and the market is still compared without them",
        )
        del best[source]
        del shapes[source]

    # Action Network failover feeds are backups.  When the first-party adapter
    # offers the **same complete contract** as the republisher, drop the
    # republisher so it cannot win best-price selection or — via a measured AN
    # mirror of a third book — suppress a real primary-vs-third arb.  A smaller
    # primary shape (soccer two-way vs AN three-way) or a one-sided primary must
    # leave the secondary in play.
    if any(source in _FAILOVER_KEYS for source in best):
        for primary, secondary in REDUNDANT_PAIRS:
            if primary not in best or secondary not in best:
                continue
            primary_shape = shapes.get(primary)
            secondary_shape = shapes.get(secondary)
            if (
                primary_shape is not None
                and primary_shape == secondary_shape
                and set(best[primary]) >= set(primary_shape)
            ):
                del best[secondary]
                shapes.pop(secondary, None)

    # A single book cannot be arbitraged against itself: the check above has
    # already refused the only case where its own prices would allow it.
    if require_distinct_sources and len({who(source) for source in best}) < 2:
        return

    # Group books by contract; only books offering the same contract may be
    # combined into one position.
    by_shape: dict[frozenset[Selection], list[str]] = defaultdict(list)
    for source in best:
        by_shape[shapes[source]].append(source)

    if len(by_shape) > 1:
        reject(
            "settlement_shape_mismatch",
            "books price different outcome sets for this market — "
            + "; ".join(
                f"{source}: {sorted(s.value for s in shapes[source])}"
                for source in sorted(best)
            )
            + " — a two-way and a three-way market are different contracts, "
            "so their legs are not combined",
        )

    for shape, sources in by_shape.items():
        # The contract's own outcome set is what has to be covered: taking two
        # legs of a three-way market leaves the third outcome naked.
        needed = shape
        if len(needed) < 2:
            continue

        # A two-way moneyline in a window where the books *do* price a draw is
        # ambiguous, and the two readings differ by the entire bankroll. If the
        # book genuinely voids ties, a level score refunds both legs and the
        # position makes nothing. If instead this is a three-way market whose draw
        # leg simply arrived without a price — which adapters drop silently — then
        # a level score *loses both legs*. Nothing on the row distinguishes these,
        # so no position is reported. This is the soccer 90-minute and hockey
        # regulation case as much as the first-five-innings one it was written for.
        if (
            market is Market.MONEYLINE
            and draw_is_priced(sport, period)
            and Selection.DRAW not in shape
        ):
            reject(
                "ambiguous_tie_settlement",
                f"two-way moneyline on {sport.value}/{period.value}, where the books price "
                "a draw: a tie either voids both legs or loses both, depending on whether "
                "this is genuinely a two-way market or a three-way one whose draw price was "
                "dropped — the row cannot say which, so no position is reported",
            )
            continue

        # A book only has to offer the one selection being taken from it: the
        # normal shape of an arbitrage is the best home price at one book against
        # the best away price at another.
        # **Sorted**, so the answer does not depend on the order the rows arrived
        # in.  ``_best_assignment`` and ``_pick_switch`` break ties on the index
        # into this list, and the enumeration below inherits it, so two venues
        # quoting the same net price — which mirrored tenants do by construction
        # — made the reported position a function of row order: 107 of 4,000
        # tie-heavy trials changed, one of them reporting the same +0.70 as
        # ``fanduel/smarkets`` capped at 100 in one order and ``bovada/smarkets``
        # with no cap at all in the other.
        eligible = sorted(
            source for source in sources if any(selection in best[source] for selection in needed)
        )
        covered = {
            selection for selection in needed for source in eligible if selection in best[source]
        }
        if covered != needed:
            continue
        # Two books are enough for a three-way position: two legs go on at one
        # book and the third at the other. What is forbidden is *every* leg at
        # one book, which is checked once the legs are chosen.
        if require_distinct_sources and len({who(source) for source in eligible}) < 2:
            continue

        # Counted here, before the edge is known: this is the denominator that
        # makes "no opportunities" meaningful. Zero opportunities out of zero
        # comparable markets means the detector never had anything to compare,
        # which is a coverage problem masquerading as a clean result.
        report.comparable_group_count += 1

        candidates = _best_assignment(
            needed=needed,
            eligible=eligible,
            best=best,
            require_distinct_sources=require_distinct_sources,
            commissions=commissions,
            counterparties=behind,
        )
        # One-leg switch cannot always escape a measured AN↔third mirror once a
        # partial primary left the secondary in play.  Brute force only when a
        # failover key is eligible — the thirty-source scaling path never hits it.
        if candidates is None and any(source in _FAILOVER_KEYS for source in eligible):
            candidates = _brute_best_assignment(
                needed=needed,
                eligible=eligible,
                best=best,
                require_distinct_sources=require_distinct_sources,
                commissions=commissions,
                counterparties=behind,
            )
        if candidates is None:
            continue
        chosen, sum_implied = candidates
        legs_quotes = [chosen[selection] for selection in sorted(needed, key=lambda s: s.value)]

        margin = 1.0 - sum_implied
        if margin <= min_margin + _EPSILON:
            continue

        # An impossible edge is a mapping fault, not money. Refused before
        # anything else is checked, and counted, because the alternative — a note
        # on a reported "opportunity" — puts a 54% phantom at the top of the
        # report, ranked above every real position.
        if margin >= REFUSE_MARGIN:
            # Retried without the book holding the extreme price, as the
            # settlement path below already does for its own narrowing.
            #
            # The rejection's own text says "suspect a selection mapped to the
            # wrong participant", so it already knows the fault belongs to one
            # leg — and then abandoned the whole contract shape.  A real
            # position between two innocent books disappeared because a third
            # book had an inverted mapping, and an inverted mapping does not
            # trip ``source_prices_itself_to_lose``: a book with home 1.10 and
            # away 9.00 has a perfectly healthy overround of its own.
            # **Each** chosen leg's source tried in turn, best survivor kept —
            # as the freshness retry below does, and as this one did not.
            #
            # Excluding only the book holding the longest price is arbitrary and
            # throws away the leg a real position needs.  Measured: fanduel
            # home 2.846 / bovada away 3.579 hits the bar at 36.9%, the longest
            # price is bovada's, and dropping it leaves fanduel/betrivers at
            # -0.33% — while dropping *fanduel* leaves pinnacle 1.545 + bovada
            # 3.579, a 7.33% position between two distinct books in one
            # settlement regime, observed together.
            best_retry = None
            for excluded in sorted({q.source for q in legs_quotes}):
                narrowed = [source for source in eligible if source != excluded]
                if len({who(source) for source in narrowed}) < (
                    2 if require_distinct_sources else 1
                ):
                    continue
                candidate = _best_assignment(
                    needed=needed,
                    eligible=narrowed,
                    best=best,
                    require_distinct_sources=require_distinct_sources,
                    commissions=commissions,
                    counterparties=behind,
                )
                if candidate is None:
                    continue
                candidate_margin = 1.0 - candidate[1]
                if candidate_margin >= REFUSE_MARGIN:
                    continue  # this exclusion kept the fault
                if candidate_margin <= min_margin + _EPSILON:
                    continue  # nothing worth reporting under it
                # The **smallest** surviving margin, not the largest.
                #
                # Taking the largest re-selects the fault: with an inverted book
                # in the group, the exclusion that keeps its long price is the
                # one that looks most profitable.  Measured: dropping pinnacle
                # left fanduel's inverted 9.00 in a 12.0% "position", while
                # dropping fanduel left a plausible 3.6% one.  Among candidate
                # explanations for an impossible edge, the conservative one is
                # the answer — a genuine cross-book edge is small.
                if best_retry is None or candidate[1] > best_retry[0][1]:
                    best_retry = (candidate, narrowed)
            retry, narrowed = best_retry if best_retry else (None, eligible)
            if retry is not None:
                # The exclusion is **persisted**, not just applied to this
                # assignment.  Narrowing ``chosen`` alone left ``eligible``
                # untouched, and the two retries below re-enumerate it — so a
                # settlement or freshness narrowing rebuilt the very assignment
                # this refused.  Measured: fanduel with an inverted mapping
                # (home 1.10 / away 9.00) against pinnacle and kalshi published
                # ``margin 41.27%, guaranteed +70.10 on 100`` with **zero
                # diagnostics**, while the same two rows without kalshi were
                # correctly refused.  Adding a venue that never appears in the
                # reported position reversed the refusal.
                eligible = narrowed
                chosen, sum_implied = retry
                legs_quotes = [
                    chosen[selection] for selection in sorted(needed, key=lambda s: s.value)
                ]
                margin = 1.0 - sum_implied
                # No floor check here: the search above already discards any
                # narrowing at or under ``min_margin``, so a candidate reaching
                # this line has cleared it.  A check was briefly added and was
                # unreachable — the pattern this module keeps having to
                # unlearn — and what it was standing in for belongs in the
                # refusal below, which always runs when nothing survives.
            else:
                reject(
                    "margin_implausibly_large",
                    f"a {margin * 100:.1f}% edge on "
                    + " vs ".join(
                        f"{chosen[selection].source} {selection.value} "
                        f"@ {chosen[selection].decimal_odds:.3f}"
                        for selection in sorted(needed, key=lambda s: s.value)
                    )
                    + " is not a price two books both published for the same contract: "
                    "suspect a selection mapped to the wrong participant (which leaves "
                    "both legs backing the same competitor), a market that is not the one "
                    "it claims to be, or a stale quote — no position is reported: "
                    "excluding each book in turn leaves nothing that is both plausible "
                    "and above the margin asked for",
                )
                continue

        # Freshness, settlement and stated size, searched **together** — by
        # scoring **every** assignment rather than searching over source subsets.
        #
        # These were narrowings in a row, each irreversible, and they composed
        # badly in every direction.  A window chosen on pre-settlement margin was
        # persisted, so the settlement narrowing could only look inside it and a
        # real same-regime position in an *earlier* window became unreachable;
        # the settlement narrowing had no freshness retry of its own, so it could
        # hand back stale legs and the market was refused as ``stale_leg`` while a
        # simultaneous same-regime position sat underneath.  Measured on
        # randomised realistic slates, one or other fired on 1,046 of 40,000
        # trials, costing up to 5.66% margin; on the captured slate 710 of 724
        # cross-source groups span more than the window, so this is the geometry
        # the data actually has.
        #
        # Sequencing them was replaced by a search over (window x regime) subsets,
        # then by that plus a walk that blocked chosen legs to reach a placeable
        # assignment — each fix reaching further and none of them exhaustive.  The
        # walk carried a depth bound, and the bound truncated: on limit-heavy
        # slates, raising it from 3 to 4 changed the published profit on 1 trial
        # in 8,000 (13 in 8,000 from 2 to 4).  A bound that changes the answer is
        # not a safety margin, it is a silent understatement — the market still
        # published, just for less money, with nothing to say it had stopped
        # looking.
        #
        # So there is no search over subsets any more.  Every constraint the
        # subsets encoded — one settlement regime, legs priced close enough
        # together, at least two counterparties, stated sizes — is checked on the
        # assignment itself, here or in the limit step below, which is why the
        # subsets could only ever remove assignments those checks would have
        # judged anyway.  Enumerating instead is exact by construction: no depth,
        # no ordering, nothing to tune.
        #
        # The cost is ``sources ** selections`` and both are small: the registry
        # holds ten sources and a contract has at most three selections, so 1,000
        # bounds it — and almost nothing gets near the bound, because a group
        # only reaches this loop after the cheaper gates above have kept it.
        #
        # Counted by instrumenting ``itertools.product`` here: a live
        # 41,683-row slate enumerates **198 assignments across 4 groups**
        # (median 49, max 64) out of 1,920 comparable groups; the committed
        # fixtures, 7,372 rows, enumerate **24 across 3** (median 4, max 16).
        #
        # An earlier version of this comment claimed "27,215 assignments across
        # 2,664 cross-source groups". That number was the *theoretical*
        # cross-product summed over every comparable group — what this loop
        # would cost if every group reached it — not what runs. It overstated
        # the work by about a thousandfold, in the direction that would make a
        # maintainer think this loop is the expensive thing to optimise. It is
        # not.
        ordered = sorted(needed, key=lambda s: s.value)
        options = [
            [source for source in eligible if selection in best[source]]
            for selection in ordered
        ]
        implied = [
            {
                source: _net_implied(best[source][selection], commissions)
                for source in sources
            }
            for selection, sources in zip(ordered, options)
        ]

        ranked: list[tuple[tuple[dict[Selection, Quote], float], list[Quote]]] = []
        # Whether any assignment was ever priced inside the window, whatever
        # else was wrong with it — so a refusal cannot blame collection timing
        # for a market that simply has no edge.
        priced_together = False
        # ...and whether one of those had a real edge and failed *only* because
        # its books do not void together.  Without this the ``stale_leg`` refusal
        # said "the books that were priced together have no edge between them"
        # about two simultaneous books carrying 3.6%, sending the operator after
        # collection timing instead of the settlement regime that actually
        # blocked it.
        clashed_in_window = False

        failover_in_play = any(source in _FAILOVER_KEYS for source in eligible)
        min_books = 2 if require_distinct_sources else 1
        for combination in itertools.product(*options):
            # one counterparty on both sides is not a position
            if failover_in_play and _uses_both_failover_feeds(combination):
                continue
            if failover_in_play:
                if _independent_source_count(combination, behind) < min_books:
                    continue
            elif len({who(source) for source in combination}) < min_books:
                continue
            attempt_legs = [
                best[source][selection]
                for source, selection in zip(combination, ordered)
            ]
            stamps = [quote.observed_at for quote in attempt_legs]
            attempt_spread = max(stamps) - min(stamps)
            if attempt_spread <= max_observation_spread:
                priced_together = True
            attempt_implied = sum(
                implied[index][source] for index, source in enumerate(combination)
            )
            # ``>= REFUSE_MARGIN`` is not re-tested: the implausible-margin gate
            # above already ran on the best-priced assignment, and no other
            # assignment can beat it, so nothing here can be more implausible
            # than what already passed.
            if 1.0 - attempt_implied <= min_margin + _EPSILON:
                continue  # no edge worth reporting
            if attempt_spread > max_observation_spread:
                continue  # these legs were not priced close enough together
            if settlement_mismatch(list(combination)) is not None:
                clashed_in_window = True
                continue  # these books do not void together
            ranked.append(
                ((dict(zip(ordered, attempt_legs)), attempt_implied), attempt_legs)
            )

        # Best margin first, and **every** survivor kept rather than only the
        # best.  The limit gate below is the one refusal with no retry of its
        # own: a thin stated limit on the best-priced leg — a matchbook
        # top-of-book with 0.60 resting behind it — took the whole market down,
        # while the same market without that venue paid 2.44%.  Keeping the
        # ranked list lets the tail fall through to the next candidate instead.
        # Margin first, then the legs themselves — a stable sort would otherwise
        # leave ties to enumeration order, which is one more thing to keep
        # accidentally deterministic rather than deliberately so.
        ranked.sort(
            key=lambda entry: (
                entry[0][1],
                tuple((quote.source, quote.selection.value) for quote in entry[1]),
            )
        )
        if not ranked:
            # Nothing satisfies both gates.  The reason named is the one the
            # *unnarrowed* legs failed, because that is what the operator was
            # looking at.
            spread = max(q.observed_at for q in legs_quotes) - min(
                q.observed_at for q in legs_quotes
            )
            settlement = settlement_mismatch([q.source for q in legs_quotes])
            if settlement is not None:
                # Named against the largest same-regime set, because "these
                # books do not void together" on its own does not say whether
                # there was a same-regime position underneath and what was wrong
                # with it.
                narrowed = _largest_regime(eligible)
                retry = (
                    _best_assignment(
                        needed=needed,
                        eligible=narrowed,
                        best=best,
                        require_distinct_sources=require_distinct_sources,
                        commissions=commissions,
                        counterparties=behind,
                    )
                    if len({who(source) for source in narrowed})
                    >= (2 if require_distinct_sources else 1)
                    else None
                )
                if retry is None:
                    reject("legs_do_not_void_together", settlement.note)
                    continue
                retry_legs = [
                    retry[0][selection]
                    for selection in sorted(needed, key=lambda s: s.value)
                ]
                retry_margin = 1.0 - retry[1]
                if retry_margin <= min_margin + _EPSILON:
                    reject(
                        "legs_do_not_void_together",
                        f"{settlement.note}; the same market priced within one "
                        f"settlement regime has a margin of {retry_margin * 100:.2f}%, "
                        f"which does not clear the {min_margin * 100:.2f}% asked for",
                    )
                    continue
                retry_spread = max(q.observed_at for q in retry_legs) - min(
                    q.observed_at for q in retry_legs
                )
                if retry_spread > max_observation_spread:
                    reject(
                        "stale_leg",
                        f"legs observed {retry_spread} apart (limit "
                        f"{max_observation_spread}) once narrowed to one settlement "
                        f"regime; margin would have been {retry_margin * 100:.2f}%",
                    )
                    continue
                reject("legs_do_not_void_together", settlement.note)
            else:
                # The window is the only thing left it can be, so the condition
                # that used to guard this is dropped rather than the branch.
                #
                # ``ranked`` is empty and the assignment named here settles
                # consistently.  That assignment is one of the combinations the
                # enumeration scored — both are built from the same ``eligible``
                # and ``best`` — and it has already cleared the margin bar above
                # and the counterparty rule by construction.  So the only gate it
                # can have failed is the observation window.
                #
                # Guarding on ``spread`` left an ``else`` naming a third reason
                # that cannot arise: 23,000 fuzzed slates never reached it, and
                # replacing its body with a raise left the whole suite green.  An
                # unreachable branch reads as coverage and is worse than none.
                reject(
                    "stale_leg",
                    f"legs observed {spread} apart (limit {max_observation_spread}); "
                    f"margin would have been {margin * 100:.2f}%, and "
                    + (
                        "the books that were priced together carry an edge but do "
                        "not settle the same way"
                        if clashed_in_window
                        else "the books that were priced together have no edge between them"
                        if priced_together
                        else "no set of these books priced this market close enough "
                        "together to replace them"
                    ),
                )
            continue

        # Each survivor in turn, best margin first, until one is reportable.
        #
        # The limit gate below refuses without a retry of its own, so a thin
        # stated size on the best-priced leg cost the whole market its position.
        reported: Opportunity | None = None
        refusals: list[tuple[str, str]] = []

        def _refuse(code: str, detail: str) -> None:
            refusals.append((code, detail))

        for (_chosen, sum_implied), legs_quotes in ranked:
            margin = 1.0 - sum_implied
            reject_here = _refuse

            opportunity = _build_opportunity(
                event_key=event_key,
                sport=sport,
                market=market,
                period=period,
                side=side,
                line=line,
                shape=shape,
                legs_quotes=legs_quotes,
                total_stake=total_stake,
                stake_increment=stake_increment,
                commissions=commissions,
            )

            # A thin edge can be smaller than one betting unit, in which case
            # rounding the stakes to whole units turns the guarantee negative. The
            # headline margin still reads positive, so this has to be caught here
            # rather than left for a caller to notice.
            if not opportunity.is_risk_free:
                reject_here(
                    "rounding_destroys_edge",
                    f"margin {margin * 100:.2f}% is too thin for a {stake_increment:g}-unit "
                    f"stake increment at a bankroll of {total_stake:g}: the worst outcome "
                    f"loses {abs(opportunity.guaranteed_profit):.2f}",
                )
                continue

            # And again at the bankroll this position says it can actually be placed
            # at, which is the only size anybody can take it in.
            #
            # The test above ran solely against *total_stake* — the notional 100 the
            # report is denominated in — while the books' own stated limits capped
            # the position far below it.  Rounding bites harder the smaller the
            # bankroll, so the two answers are different questions: a FanDuel leg
            # limited to 6.80 against a 3.05 at Pinnacle reported ``margin 0.55%,
            # guaranteed +0.50 on 100`` and staked that leg 67.00 against its stated
            # 6.80.  Re-run at the 10.00 the position itself publishes, the same
            # market loses 0.85 in the worst outcome.  The only placeable version of
            # it was a guaranteed loss, and nothing said so.
            # Any stated limit at all, not only one that binds below *total_stake*.
            #
            # Gating on ``cap < total_stake`` skipped the whole placeable rebuild
            # whenever the floored cap landed at or above the bankroll — and the
            # per-leg rounding still overflows there.  A matchbook leg sized 51.80
            # caps the position at 100.06, floors to exactly 100.00, so the guard
            # did not run and the split staked that leg **52.00 against its stated
            # 51.80**.  Every limit in [51.77, 52.00) does it; it is a band, not a
            # knife edge.
            # Verified up to the reported bankroll, and no further.
            #
            # ``min(cap, total_stake)`` on its own threw information away: where the
            # stated limits permit more than the notional bankroll, the true cap is
            # the useful number — an operator sizing up wants to know the position
            # holds to 150, and clamping printed "limit-capped bankroll: 100.00",
            # which reads as a constraint where there is none.  So the *ceiling* is
            # clamped, because that is as far as the per-leg split has been checked,
            # and the *published* cap keeps the venues' own answer whenever the
            # step-down did not have to reduce anything.
            stated_cap = opportunity.max_total_stake
            cap = None if stated_cap is None else min(stated_cap, total_stake)
            if cap is not None:
                if cap <= 0:
                    reject_here(
                        "cannot_be_placed_at_the_stated_limits",
                        f"margin {margin * 100:.2f}%, but the books' stated limits do not "
                        f"cover one {stake_increment:g}-unit stake",
                    )
                    continue
                # The largest bankroll at or below the venues' cap where **every
                # leg** is inside the size its own venue stated **and** the position
                # still cannot lose.
                #
                # Both conditions, and stepping past a size that fails either.
                # Flooring the *total* to whole units does not bound the *per-leg*
                # rounding that follows it — a leg sized 6.80 caps the position at
                # 13, and the split of 13 stakes it 7.00 — and stopping at the first
                # size whose legs fit threw away positions that pay at a smaller
                # one: ``home 1.50 / away 3.05 / limit 31.50`` was refused outright
                # while a bankroll of 64 fits both limits and guarantees +0.05.
                #
                # Searched from ``stated_cap``, not from the clamped ceiling, so the
                # number published as the cap is one this loop actually verified.
                # Republishing the venues' floored cap unchecked told an operator a
                # position held to 101 when re-running it there both overstakes the
                # binding leg and loses 0.35 — the very failure this loop exists to
                # prevent, in the branch that skipped it.  A parameter sweep found
                # 628 such (odds, limit) pairs in the ordinary band.
                def _fits(candidate: Opportunity) -> bool:
                    # ``total_stake > 0``: a zero-stake build has a floor of
                    # exactly 0 and passes ``is_risk_free``, and it is not a
                    # position — it is the refusal wearing a headline.
                    return candidate.total_stake > 0 and candidate.is_risk_free and all(
                        leg.quote.limit_amount is None
                        or leg.stake <= leg.quote.limit_amount + _EPSILON
                        for leg in candidate.legs
                    )

                def _at(size: float) -> Opportunity:
                    return _build_opportunity(
                        event_key=event_key,
                        sport=sport,
                        market=market,
                        period=period,
                        side=side,
                        line=line,
                        shape=shape,
                        legs_quotes=legs_quotes,
                        total_stake=size,
                        stake_increment=stake_increment,
                        commissions=commissions,
                    )

                largest = None
                step = stake_increment if stake_increment > 0 else stated_cap
                # Walked on **unit counts**, not by repeated float subtraction.
                # ``trial -= step`` accumulated error for any step that is not a
                # dyadic fraction — at 0.2 the walk ended on ``2.78e-17 > 0``,
                # which reached the allocator as a zero-unit bankroll, whose
                # fallback returned an all-zero split, whose floor of exactly 0
                # passed ``is_risk_free`` — and the engine published ``guaranteed
                # +0.00 on 0`` with both legs at stake 0.00 *instead of* the
                # refusal, on ~60% of the slates the refusal was for.  A count
                # times the step is computed once and cannot drift.
                for count in range(int(stated_cap / step + 1e-9), 0, -1):
                    candidate = _at(count * step)
                    if _fits(candidate):
                        largest = candidate
                        break
                if largest is None:
                    reject_here(
                        "cannot_be_placed_at_the_stated_limits",
                        f"margin {margin * 100:.2f}%, but no whole-unit split at or below "
                        f"{stated_cap:g} — the largest total the stated sizes could ever "
                        "pay for — both keeps every leg inside its stated size and stays "
                        "risk-free",
                    )
                    continue
                # Reported at the smaller of that and the bankroll asked for.
                placeable = (
                    largest
                    if largest.total_stake <= total_stake
                    else _at(min(total_stake, largest.total_stake))
                )
                # No second refusal here.  ``largest.total_stake > total_stake``
                # implies every limited leg clears ``total_stake`` with a whole
                # increment to spare, and ``is_risk_free`` at ``total_stake`` was
                # asserted above — so ``_fits(placeable)`` cannot fail.  Verified by
                # replaying the step-down over 32,038 two-leg and 10,651 three-way
                # configurations across the arb band: zero hits.  A branch that
                # cannot run reads as coverage and is worse than none, which is the
                # rule this module states and had stopped following.
                assert _fits(placeable), "the placeable rebuild must fit by construction"
                placeable = replace(placeable, max_total_stake=largest.total_stake)
                # Reported *as* the placeable version, not merely checked against it.
                #
                # Refusing the ones that lose at their cap was half the fix.  The
                # ones that survive were still being reported, and **ranked**, at the
                # notional bankroll: ``report.opportunities.sort`` orders by
                # ``guaranteed_profit`` and ``describe()`` prints it as the headline,
                # both computed at *total_stake*.  A pinnacle 2.20 against a
                # matchbook 2.20 sized 6.80 printed ``guaranteed +10.00 on 100`` with
                # both legs staked 50.00, and ranked above a genuinely unlimited
                # two-book position paying +2.50 — while the only version of it
                # anybody could place pays +0.20.  A number nobody can act on has no
                # business being the sort key.
                opportunity = placeable

            # Kept if it pays more than anything found so far, rather than
            # taken on sight.
            #
            # Compared at the bankroll each candidate can actually be placed
            # at — which for an uncapped one is the bankroll the operator asked
            # for.  A differential fuzz against an exhaustive search finds a
            # residual handful of markets (5 in 1,400 arb-bearing, worst 0.29 on
            # 100) where a *capped* assignment rounds to a few pence more at its
            # smaller size than an uncapped one does at the full stake.  Those
            # are stake-rounding artefacts and are deliberately not chased:
            # taking them would mean electively staking below what was asked for
            # to win a rounding remainder, and ``total_stake`` is an input, not
            # a variable to optimise.  A cap that *forces* a smaller size is a
            # different thing and is honoured above.
            #
            # The candidates are ranked by *margin*, and margin is not money: a
            # 3.60% edge capped at a bankroll of 12 by one venue's stated size
            # pays +0.30, while the 2.44% edge underneath it pays +2.50 on the
            # full 100.  ``report.opportunities.sort`` already orders the
            # published positions by ``guaranteed_profit`` for exactly this
            # reason; choosing between candidates *within* one market has to use
            # the same measure.
            if reported is None or opportunity.guaranteed_profit > (
                reported.guaranteed_profit
            ):
                reported = opportunity
            # Ranked best-margin first.  Once *this* survivor is placeable at
            # the full asked bankroll, every later candidate has a worse margin
            # at the same ceiling and cannot beat whatever ``reported`` now
            # holds — including when ``reported`` is still a smaller capped
            # position that happens to round to a few pence more.  A *capped*
            # survivor below *total_stake* must keep searching: a thinner
            # uncapped edge underneath can still win on money (see the
            # 3.60%-at-12 vs 2.44%-at-100 case above).
            if (
                opportunity.max_total_stake is None
                or opportunity.max_total_stake + _EPSILON >= total_stake
            ):
                break

        if reported is None:
            for code, detail in refusals[:1]:
                reject(code, detail)
            continue
        opportunity = reported
        report.opportunities.append(opportunity)


def _fixture_outliers(rows: Sequence[Quote]) -> set[str]:
    """Sources whose rows describe a different fixture from the consensus.

    Compared on the resolved participant keys rather than on display names,
    because for an open-roster competition the display name is the book's own
    spelling: "Wolves" and "Wolverhampton" are one club, and comparing the
    strings would refuse every legitimate soccer and tennis position.

    ``commence_time`` is deliberately **not** part of the comparison, though it
    used to be.  Books disagree about a kickoff by minutes as a matter of course
    — that is why clustering exists and why the start-time check downstream is a
    warning — so requiring exact equality here refused ordinary cross-book
    positions for a difference that carries no information.  What separates two
    genuinely different fixtures is already carried by ``event_key``, which
    clustering assigns before any of this runs.

    A source is counted once, on the identity most of its own rows carry, so a
    book that is internally inconsistent is one outlier rather than several.
    """
    per_source: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for row in rows:
        per_source[row.source][(row.home_participant, row.away_participant)] += 1
    identities = {
        source: max(sorted(counts.items()), key=lambda item: item[1])[0]
        for source, counts in per_source.items()
    }
    if len(set(identities.values())) < 2:
        return set()
    counts = Counter(identities.values())
    top = max(counts.values())
    consensus = sorted((value for value, n in counts.items() if n == top), key=str)[0]
    return {source for source, value in identities.items() if value != consensus}


def _best_assignment(
    *,
    needed: frozenset[Selection],
    eligible: Sequence[str],
    best: dict[str, dict[Selection, Quote]],
    require_distinct_sources: bool,
    commissions: Mapping[str, Commission] | None = None,
    counterparties: Mapping[str, str] | None = None,
) -> tuple[dict[Selection, Quote], float] | None:
    """Pick the book for each selection that minimises total implied probability.

    The constraint is that the position spans at least two **counterparties**.
    Two legs *sharing* a book is fine; every leg at one book is not, because that
    is a book mispricing itself rather than a position anyone can take — and two
    source keys that are one price feed are one book however different the keys
    look.  *counterparties* maps the keys that are known to be one, for this
    league; anything unnamed is its own counterparty.

    This used to enumerate ``itertools.product`` over the eligible books, which
    is ``sources ** selections`` — 27 combinations per three-way market at three
    sources and 27,000 at thirty.  Measured on 700 three-way soccer fixtures, the
    live case: 0.01 s at three sources, 8.43 s at thirty.  840× the time for 10×
    the rows, and the exponent is the source count, which is the number this
    whole exercise is trying to raise.

    The constraint is satisfiable exactly in linear time, and the argument is
    short enough to state:

    1. Take the best price for each selection independently.  If those already
       span two books, no legal assignment can beat them, because each leg is
       individually optimal.
    2. If they all land on one book *B*, every legal assignment differs from that
       one on at least one leg, and moving a second leg can only cost more.  So
       the optimum switches **exactly one** leg away from *B* — the one whose
       next-best price outside *B* is closest to *B*'s.

    Both steps are ``O(sources × selections)``.  Ties are resolved to the same
    assignment the exhaustive search would have returned, which is the
    lexicographically first one in its enumeration order; see
    :func:`_pick_switch`.  ``tests/test_arb.py`` checks that equivalence against
    a brute-force reference over randomised markets, because "provably the same"
    is worth exactly as much as the proof and the proof is worth checking.

    Prices are compared **net of commission**.  An exchange leg quoted at 3.00
    with 2% of winnings taken pays 2.96, and picking between books on the quoted
    number would prefer a venue that pays less.
    """
    behind = counterparties or {}
    who = lambda source: behind.get(source, source)  # noqa: E731
    selections = sorted(needed, key=lambda s: s.value)
    options = [
        [source for source in eligible if selection in best[source]]
        for selection in selections
    ]
    if any(not sources for sources in options):
        return None

    implied = [
        [_net_implied(best[source][selection], commissions) for source in sources]
        for selection, sources in zip(selections, options)
    ]

    # Step 1: the unconstrained optimum, earliest option on a tie.
    picked = [min(range(len(row)), key=lambda i: (row[i], i)) for row in implied]

    def assemble(indices: Sequence[int]) -> tuple[dict[Selection, Quote], float]:
        assignment = {
            selection: best[options[k][index]][selection]
            for k, (selection, index) in enumerate(zip(selections, indices))
        }
        return assignment, sum(implied[k][index] for k, index in enumerate(indices))

    if not require_distinct_sources:
        return assemble(picked)
    picked_sources = [options[k][index] for k, index in enumerate(picked)]
    failover_in_play = any(source in _FAILOVER_KEYS for source in picked_sources) or any(
        source in _FAILOVER_KEYS for row in options for source in row
    )
    dual_feed = failover_in_play and _uses_both_failover_feeds(picked_sources)
    if not dual_feed:
        if failover_in_play:
            if _independent_source_count(picked_sources, behind) >= 2:
                return assemble(picked)
        elif len({who(source) for source in picked_sources}) >= 2:
            return assemble(picked)

    # Step 2: every leg is at one counterparty, so exactly one leg has to move.
    monopolist = who(picked_sources[0])
    if failover_in_play:

        def who_for_switch(source: str) -> str:
            if _independent_source_count((picked_sources[0], source), behind) < 2:
                return monopolist
            return who(source)

        switch_who = who_for_switch
    else:
        switch_who = who
    switch = _pick_switch(options, implied, picked, monopolist, switch_who)
    if switch is None:
        return None
    leg, alternative = switch
    if failover_in_play:
        trial = list(picked_sources)
        trial[leg] = options[leg][alternative]
        if _uses_both_failover_feeds(trial):
            return None
        if _independent_source_count(trial, behind) < 2:
            return None
    indices = list(picked)
    indices[leg] = alternative
    return assemble(indices)


def _pick_switch(
    options: Sequence[Sequence[str]],
    implied: Sequence[Sequence[float]],
    picked: Sequence[int],
    monopolist: str,
    who: Callable[[str], str] = lambda source: source,
) -> tuple[int, int] | None:
    """Which leg to move off *monopolist*, and to which counterparty.

    *monopolist* is a counterparty id and *who* maps a source key to one, so a
    leg cannot be "moved" onto a second key belonging to the same book.

    Returns ``(leg, option index)``, or ``None`` when no other book offers any of
    the selections.

    The cheapest switch wins.  What decides a **tie** is chosen to match what an
    exhaustive ``itertools.product`` search would have returned, so that replacing
    the search cannot silently change a single reported position: that search
    keeps the first assignment with a strictly lower total, and enumerates in
    lexicographic order of option indices, so it ends on the lexicographically
    smallest optimal tuple.

    Since step 1 picked the *earliest* index among equally-best prices, the
    monopolist sits at the smallest index that achieves each leg's best price.
    A switch therefore moves that leg's index either up or down:

    * moving it **down** makes the tuple lexicographically smaller, and the
      earliest leg that can do so wins;
    * when every switch moves its index up, the smallest tuple is the one that
      leaves the earlier legs alone — so the **last** leg wins.
    """
    best_switch: tuple[int, int] | None = None
    best_cost: float | None = None
    for leg, (sources, row) in enumerate(zip(options, implied)):
        alternative: int | None = None
        for index, source in enumerate(sources):
            if who(source) == monopolist:
                continue
            if alternative is None or (row[index], index) < (row[alternative], alternative):
                alternative = index
        if alternative is None:
            continue
        cost = row[alternative] - row[picked[leg]]
        if best_cost is None or cost < best_cost:
            best_cost, best_switch = cost, (leg, alternative)
        elif cost == best_cost and best_switch is not None:
            incumbent_leg, incumbent_alt = best_switch
            moves_down = alternative < picked[leg]
            incumbent_moves_down = incumbent_alt < picked[incumbent_leg]
            if moves_down and not incumbent_moves_down:
                best_switch = (leg, alternative)
            elif moves_down == incumbent_moves_down and not moves_down:
                # Both move up: prefer the later leg, which leaves the earlier
                # positions at their smaller indices.
                best_switch = (leg, alternative)
    return best_switch


def _build_opportunity(
    *,
    event_key: str,
    sport: Sport,
    market: Market,
    period: Period,
    side: Side | None,
    line: float | None,
    shape: frozenset[Selection],
    legs_quotes: Sequence[Quote],
    total_stake: float,
    stake_increment: float,
    commissions: Mapping[str, Commission] | None = None,
) -> Opportunity:
    # Every number below is computed on the price actually **paid**, not the one
    # published.  On a sportsbook the two are the same; on an exchange they are
    # not, and using the published one would split the stakes so the legs no
    # longer return equal amounts — which is the difference between a hedged
    # position and a bet.
    odds = [net_decimal(quote, commissions) for quote in legs_quotes]
    ideal = stake_split(odds, total_stake)
    outcomes = settlement_outcomes(sport, market, period, line, shape)
    # ``stake_split`` equalises the *full-win* returns, which is the floor-
    # maximising ray only when every outcome pays one leg its full price and the
    # others nothing.  On a quarter line it is not, and the sweep below explores
    # along whatever ray it is given — so pointing it at the wrong one puts the
    # optimum out of reach before any rounding happens.
    if len(legs_quotes) == 2:
        aimed = floor_maximising_split(
            [
                [
                    _return_multiplier(results[quote.selection], net)
                    for quote, net in zip(legs_quotes, odds)
                ]
                for _, results in outcomes
            ],
            total_stake,
        )
        if aimed is not None and all(share > 0 for share in aimed):
            ideal = aimed

    # Return multipliers are fixed for the (outcomes × legs) grid: only the
    # stake vector changes across ``stake_candidates``.  Precomputing them is
    # what keeps the sweep linear in candidates rather than in
    # candidates × outcomes × legs × dict lookups — the hot path when many
    # assignments clear the margin bar and each one builds a position.
    #
    # Indexed, not defaulted.  Every leg is drawn from the contract's own shape
    # and ``settlement_outcomes`` maps every shape selection in every outcome —
    # checked exhaustively over all 14 ``(sport, period)`` pairs against every
    # market and line granularity: **zero** outcomes omit one.  So the default
    # was unreachable, and an unreachable default is worse than none here,
    # because it hides which way the mistake would go: a mutation audit changed
    # it from ``LOSE`` to ``PUSH`` — turning every non-participating leg into a
    # refunded one, which overstates every profit floor — and all 2,248 tests
    # passed.  A missing key now raises where it can be seen.
    outcome_multipliers = [
        (
            label,
            [
                _return_multiplier(results[quote.selection], net)
                for quote, net in zip(legs_quotes, odds)
            ],
        )
        for label, results in outcomes
    ]

    def evaluate(stakes: Sequence[float]) -> tuple[list[tuple[str, float]], float]:
        """Profit in every settlement outcome, and the floor across them."""
        staked = sum(stakes)
        profits: list[tuple[str, float]] = []
        floor = float("inf")
        for label, multipliers in outcome_multipliers:
            profit = sum(stake * mult for stake, mult in zip(stakes, multipliers)) - staked
            profits.append((label, profit))
            if profit < floor:
                floor = profit
        return profits, floor

    # Choose the whole-unit split by the profit floor it produces, because that
    # floor is what decides whether the position is risk-free. The split with the
    # smallest rounding error is a different choice and can be the one that turns
    # a thin but genuine edge negative.
    best_stakes = max(
        stake_candidates(ideal, total_stake, stake_increment), key=lambda s: evaluate(s)[1]
    )
    legs = tuple(
        ArbLeg(quote=quote, stake=stake, net_decimal_odds=net)
        for quote, stake, net in zip(legs_quotes, best_stakes, odds)
    )
    staked = sum(leg.stake for leg in legs)
    profits, _ = evaluate(best_stakes)
    labels = [label for label, _ in profits]

    notes: list[str] = []
    if "push" in labels:
        if line is None:
            notes.append(
                f"a {sport.value} {period.value} can end level and the books price no draw, "
                "so a tie voids every leg — the guaranteed profit is therefore zero, not "
                "the margin"
            )
        else:
            notes.append(
                f"line {line:g} is a whole number of {scoring_unit(sport, period)}, so the "
                "market can land exactly on it and refund every leg — the guaranteed profit "
                "is therefore zero"
            )
    if any(label.startswith("half_push") for label in labels):
        whole = float(round(line)) if line is not None else 0.0
        notes.append(
            f"line {line:g} is a quarter line: the stake splits between "
            f"{line - 0.25:g} and {line + 0.25:g}, so landing on exactly {whole:g} refunds "
            "half of each leg and settles the other half — the floor below is what that "
            "outcome pays, not the headline margin"
        )
    if any(quote.is_alternate for quote in legs_quotes):
        notes.append("uses an alternate line, which is usually offered at a lower limit")
    if Selection.DRAW in shape:
        notes.append("three-way market: all three outcomes are covered")
    if market is not Market.MONEYLINE:
        if period is Period.FULL_GAME:
            notes.append(
                _VOID_RISK.get(sport, _VOID_RISK_DEFAULT).format(market=market.value)
            )
        else:
            notes.append(
                f"partial-period {market.value} ({period.value}); confirm both books void "
                "it the same way before betting"
            )

    margin = 1.0 - sum(1.0 / odd for odd in odds)
    if any(leg.pays_commission for leg in legs):
        charged = ", ".join(
            sorted({f"{leg.source} {commission_for(leg.source, commissions).describe()}"
                    for leg in legs if leg.pays_commission})
        )
        notes.append(
            f"net of commission ({charged}); the quoted prices imply a wider margin "
            "than this position actually pays"
        )
    if margin > IMPLAUSIBLE_MARGIN:
        notes.append(
            f"margin of {margin * 100:.1f}% is implausibly large for a real cross-book "
            "price: suspect a stale quote, a palpable error the book will void, or two "
            "markets that are not in fact the same contract — verify before staking"
        )

    # How large the position can actually be.
    #
    # This used to require *every* leg to state a limit, on the reasoning that an
    # unstated limit is unknown rather than unlimited and a partial answer would
    # mislead.  That was right when every source was a sportsbook, which mostly
    # states nothing.  It is wrong now: an exchange publishes the money actually
    # sitting behind the top of book — Matchbook's ``available-amount``, SX Bet's
    # order size — so the common case is a position with one leg whose size is
    # stated and one whose is not, and dropping the cap there says nothing about
    # a $40 market being offered a $500 stake.
    #
    # A cap derived from the legs that *do* state a limit is a genuine upper
    # bound: an unstated leg can only lower it further.  So it is reported, and
    # the note says how many legs it was derived from, which is the part that
    # keeps it from being read as the whole answer.
    limits = [quote.limit_amount for quote in legs_quotes]
    max_total_stake: float | None = None
    if any(limit is not None for limit in limits):
        # ``T <= limit_i * d_i`` for every limit-stating leg, because the
        # position must be risk-free: in the outcome where leg *i* alone wins,
        # the return is ``stake_i * d_i`` and it must cover the whole total, so
        # ``T <= stake_i * d_i <= limit_i * d_i``.  Every settlement shape this
        # module builds gives each selection such an outcome, so the bound is
        # exact.
        #
        # This used to be ``limit_i * S * d_i`` — the bound for stakes on the
        # equal-return ray, where ``stake_i = T * (1/d_i)/S``.  That was right
        # while the allocator only walked that ray and became a silent truncation
        # when it stopped: an off-ray split can total more than the ray bound
        # with every leg still inside its stated size, and the step-down never
        # tried it.  Measured: a position staking 7 with legs at 5.00 of a
        # stated 5.03 and 2.00 of a stated 60.94 was refused outright with
        # "no whole-unit split at or below the 6 the venues state" — a number no
        # venue stated — and 116 of 15,086 fuzzed limited slates were refused or
        # understated, every one with the optimum's total above the ray bound.
        cap = min(
            limit * odd
            for limit, odd in zip(limits, odds)
            if limit is not None
        )
        # Floored to whole betting units. The cap is derived from ideal stakes, so
        # staking it and then rounding to whole units pushes the binding leg a
        # fraction over the limit the book actually stated — and a leg the book
        # rejects is a leg that is not on, which is exactly when the position
        # stops being risk-free.
        if stake_increment > 0:
            cap = int(cap / stake_increment + 1e-9) * stake_increment
        # A cap of zero — stated limits too small to cover one betting unit — is
        # not annotated here.  ``_examine_group`` rejects the position outright
        # in that case, so a note would never be read by anybody: any cap at or
        # below zero is also below *total_stake*, which is what triggers the
        # rejection.
        #
        # There was a branch above this testing ``limit is not None and limit
        # <= 0``, meaning a book that states a limit of zero, and it could never
        # run: :class:`src.schema.Quote` refuses a non-positive ``limit_amount``
        # outright, so no parsed or stored row can hold one.  Its covering test
        # reached it only through ``model_copy``, which skips validation.  The
        # lesson is the reason this comment exists rather than a second note —
        # an unreachable branch reads as coverage and is worse than none.
        max_total_stake = cap
        stated = sum(1 for limit in limits if limit is not None)
        if stated < len(limits):
            notes.append(
                f"the bankroll cap comes from {stated} of {len(limits)} legs; the "
                "others state no limit, which is unknown rather than unlimited, so "
                "the real cap can only be lower"
            )

    return Opportunity(
        event_key=event_key,
        sport=sport,
        home_team=legs_quotes[0].home_team,
        away_team=legs_quotes[0].away_team,
        commence_time=legs_quotes[0].commence_time,
        market=market,
        period=period,
        side=side,
        line=line,
        legs=legs,
        total_stake=staked,
        outcome_profits=tuple(profits),
        max_total_stake=max_total_stake,
        notes=tuple(notes),
    )


# ── best-price surface ───────────────────────────────────────────────────────


def best_prices(
    quotes: Sequence[Quote],
    commissions: Mapping[str, Commission] | None = None,
    *,
    view_only: Collection[str] | None = None,
) -> dict[MarketGroup, dict[Selection, Quote]]:
    """Best available price for every selection of every comparable market.

    Useful on its own: it is the line-shopping view, and it is what an
    opportunity is drawn from.

    Ranked **net of commission**, like everything else that compares two venues'
    prices.  On the quoted number this surface named an exchange at 2.10 as
    better than a book at 2.08 when the exchange pays 2.045, and printed an
    overround of 0.9877 — a 1.23% edge — for a pair whose net sum is 1.0055 and
    which therefore loses. It was the one place the commission model had not been
    applied, and it contradicted the detector on exactly the legs the model
    exists for.

    *view_only* is which sources are not a counterparty, and a **stored run has to
    be read with the set that run was collected under** rather than with the
    module-level :data:`VIEW_ONLY_SOURCES`, which is frozen at import from
    ``settings.STATE``.  ``hardrock`` is the difference: view-only in Pennsylvania,
    which licenses no Hard Rock book, and a real counterparty in Illinois.  So
    ``collector lines`` put a Hard Rock price on a Pennsylvania board — as the
    *best* price for a selection — when read from an Illinois-configured box and
    left it off when read from a Pennsylvania one, while ``collector arb`` on the
    same run resolved the set from the run's own jurisdiction.  One stored run, two
    line boards, and neither command mentioning why.  ``None`` keeps the ambient
    default for callers with no run in hand.
    """
    excluded = VIEW_ONLY_SOURCES if view_only is None else frozenset(view_only)
    surface: dict[MarketGroup, dict[Selection, Quote]] = defaultdict(dict)
    for quote in quotes:
        if quote.status is not QuoteStatus.ACTIVE:
            continue
        if quote.source in excluded:
            continue
        bucket = surface[group_key(quote)]
        existing = bucket.get(quote.selection)
        if existing is None or net_decimal(quote, commissions) > net_decimal(
            existing, commissions
        ):
            bucket[quote.selection] = quote
    return dict(surface)
