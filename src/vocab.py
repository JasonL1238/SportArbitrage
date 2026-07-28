"""The closed vocabularies shared by every sport, and the settlement facts.

These enums are **closed**.  A source value that cannot be mapped to one of them
is rejected or counted out of scope upstream — never passed through as free
text, because a free-text fallback makes unnormalized data look normalized.

The part of this module that earns its keep is not the enums; it is
:data:`PERIOD_RULES`.  Two prices may only be compared when they are the same
contract, and for a moneyline "the same contract" depends on facts that no
sportsbook states on the row:

* Can the scoring window end level at all?  A tennis match cannot.  An NFL game
  can.
* If it can, does the book *price* the tie as a third selection, or does a tie
  **void** the two-way market?

Those two bits decide whether a two-way moneyline has a push outcome, and the
two readings differ by the entire bankroll.  They are recorded here per
``(sport, period)`` so exactly one place in the codebase knows them.

The overtime distinction lives in :class:`Period` rather than in a separate
"settlement" field, because ``period`` is already part of ``dedup_key``,
``market_key``, and arbitrage pairing.  Encoding it there makes "never match
different settlement rules" structural rather than a rule someone has to
remember: ``full_game`` and ``regulation`` can no more be paired than a first
inning and a full game can.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Sport(StrEnum):
    """The sports collected.  ``sport`` is part of every event's identity, so a
    baseball spread and a soccer handicap can never be joined even though both
    are :attr:`Market.SPREAD`."""

    BASEBALL = "baseball"
    BASKETBALL = "basketball"
    HOCKEY = "hockey"
    FOOTBALL = "football"
    TENNIS = "tennis"
    SOCCER = "soccer"


class Market(StrEnum):
    """Canonical game-market types.

    Deliberately sport-neutral.  A book's own name for :attr:`SPREAD` is a run
    line in baseball, a puck line in hockey, a point spread in football and
    basketball, and an Asian handicap in soccer — but the contract is the same
    shape in all of them: one number, stated from the backed side's
    perspective, applied to the final margin.  Keeping one enum member and
    letting ``sport`` disambiguate avoids four near-identical members that would
    all need the same handling everywhere.
    """

    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    TEAM_TOTAL = "team_total"


class Period(StrEnum):
    """The scoring window a market settles on.

    ``FULL_GAME`` means the complete contest **as the book settles it by
    default**, including overtime, extra innings, or a shootout where those
    apply.  ``REGULATION`` explicitly excludes them.

    Both are collected for hockey because both books offer both — Kambi as
    ``"Puck Line - Including Overtime and Penalty Shootout"`` versus
    ``"Puck Line - Regular Time"``, Pinnacle as period 0 versus period 6 — and
    they are different contracts with different outcome counts.  A 60-minute
    hockey market is three-way because a tie is a real settlement outcome; the
    same market including the shootout is two-way because it cannot tie.
    Pairing one against the other looks like a large edge on two fair prices.

    Soccer is the case worth stating explicitly: a soccer ``FULL_GAME`` market
    is **90 minutes plus stoppage**, because that is the complete contest a
    league fixture settles on.  Cup extra-time and "to qualify" markets are a
    different contract and are counted out of scope, never mapped here.
    """

    FULL_GAME = "full_game"
    REGULATION = "regulation"
    FIRST_HALF = "first_half"
    FIRST_5_INNINGS = "first_5_innings"
    FIRST_1_INNING = "first_1_inning"


class Selection(StrEnum):
    """Canonical selection within a market."""

    HOME = "home"
    AWAY = "away"
    DRAW = "draw"
    OVER = "over"
    UNDER = "under"


class Side(StrEnum):
    """Which participant a team-total market refers to."""

    HOME = "home"
    AWAY = "away"


class QuoteStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


# ── market shape ─────────────────────────────────────────────────────────────

#: Selections that are structurally legal for each market.  Whether ``DRAW`` is
#: legal *in a given sport and period* is a further restriction — see
#: :func:`draw_is_priced`.
SELECTIONS_BY_MARKET: dict[Market, frozenset[Selection]] = {
    Market.MONEYLINE: frozenset({Selection.HOME, Selection.AWAY, Selection.DRAW}),
    Market.SPREAD: frozenset({Selection.HOME, Selection.AWAY}),
    Market.TOTAL: frozenset({Selection.OVER, Selection.UNDER}),
    Market.TEAM_TOTAL: frozenset({Selection.OVER, Selection.UNDER}),
}

#: Markets that are meaningless without a line.
MARKETS_REQUIRING_LINE: frozenset[Market] = frozenset(
    {Market.SPREAD, Market.TOTAL, Market.TEAM_TOTAL}
)

#: Markets that must name which participant they are about.
MARKETS_REQUIRING_SIDE: frozenset[Market] = frozenset({Market.TEAM_TOTAL})


# ── settlement facts ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PeriodRules:
    """What can happen at the end of one scoring window in one sport.

    :param draw_is_priced: the books price a draw as a third moneyline
        selection, so a complete moneyline here has three outcomes.
    :param tie_possible: the window can end level.  When this is true and
        :attr:`draw_is_priced` is false, a two-way moneyline **voids** on a tie
        — the market has a push outcome that no field on the row reveals.
    :param scoring_unit: what the total counts, for human-readable output.
    """

    draw_is_priced: bool
    tie_possible: bool
    scoring_unit: str


#: Per ``(sport, period)`` settlement facts.  A ``(sport, period)`` pair absent
#: from this table is **not collectable**: the pipeline cannot reason about a
#: window whose tie semantics it does not know, so adapters must reject rather
#: than guess.  :func:`period_rules` enforces that.
#:
#: The entries that matter most:
#:
#: * ``(FOOTBALL, FULL_GAME)`` — ``tie_possible`` is true.  An NFL game that is
#:   still level after overtime is a tie, and US books do not price a draw on
#:   the two-way moneyline; the bet voids.  Rare, but a voided leg turns a
#:   "guaranteed" position into a one-sided bet, so arbitrage must model it.
#: * ``(HOCKEY, FULL_GAME)`` — cannot tie, because the shootout decides it.
#:   ``(HOCKEY, REGULATION)`` can, and is priced three-way.
#: * ``(BASEBALL, FULL_GAME)`` — cannot tie; extra innings run until decided.
#: * ``(TENNIS, FULL_GAME)`` — cannot tie.  A retirement voids the market, but
#:   that is a non-completion, not a level scoreline.
PERIOD_RULES: dict[tuple[Sport, Period], PeriodRules] = {
    (Sport.BASEBALL, Period.FULL_GAME): PeriodRules(False, False, "runs"),
    (Sport.BASEBALL, Period.FIRST_5_INNINGS): PeriodRules(True, True, "runs"),
    (Sport.BASEBALL, Period.FIRST_1_INNING): PeriodRules(True, True, "runs"),
    (Sport.BASKETBALL, Period.FULL_GAME): PeriodRules(False, False, "points"),
    (Sport.BASKETBALL, Period.REGULATION): PeriodRules(True, True, "points"),
    (Sport.BASKETBALL, Period.FIRST_HALF): PeriodRules(True, True, "points"),
    (Sport.HOCKEY, Period.FULL_GAME): PeriodRules(False, False, "goals"),
    (Sport.HOCKEY, Period.REGULATION): PeriodRules(True, True, "goals"),
    (Sport.FOOTBALL, Period.FULL_GAME): PeriodRules(False, True, "points"),
    (Sport.FOOTBALL, Period.REGULATION): PeriodRules(True, True, "points"),
    (Sport.FOOTBALL, Period.FIRST_HALF): PeriodRules(True, True, "points"),
    (Sport.TENNIS, Period.FULL_GAME): PeriodRules(False, False, "games"),
    (Sport.SOCCER, Period.FULL_GAME): PeriodRules(True, True, "goals"),
    (Sport.SOCCER, Period.FIRST_HALF): PeriodRules(True, True, "goals"),
}


def period_rules(sport: Sport, period: Period) -> PeriodRules:
    """Settlement facts for one scoring window.

    Raises ``KeyError`` for an unknown combination rather than returning a
    default.  A default would be a guess about whether a tie voids the bet, and
    that guess is worth the whole stake.
    """
    try:
        return PERIOD_RULES[(sport, period)]
    except KeyError:
        raise KeyError(
            f"no settlement rules recorded for {sport.value}/{period.value}; "
            "add them to src.vocab.PERIOD_RULES before collecting this window"
        ) from None


def is_collectable(sport: Sport, period: Period) -> bool:
    """Is this scoring window one the pipeline knows how to settle?"""
    return (sport, period) in PERIOD_RULES


def draw_is_priced(sport: Sport, period: Period) -> bool:
    """May a ``DRAW`` selection legally appear on this market?"""
    return is_collectable(sport, period) and PERIOD_RULES[(sport, period)].draw_is_priced


def tie_possible(sport: Sport, period: Period) -> bool:
    """Can this scoring window end level?"""
    return is_collectable(sport, period) and PERIOD_RULES[(sport, period)].tie_possible


def scoring_unit(sport: Sport, period: Period = Period.FULL_GAME) -> str:
    """What a total counts in this sport — ``"runs"``, ``"goals"``, …"""
    rules = PERIOD_RULES.get((sport, period))
    return rules.scoring_unit if rules else "points"


# ── in-scope markets ─────────────────────────────────────────────────────────

#: The markets each sport is expected to produce.  Used by validation's
#: core-coverage check: if a book stops returning one of these entirely, a label
#: has probably been renamed upstream, and that must surface as an error rather
#: than as a quiet drop in row count.
#:
#: Tennis carries only the moneyline (match winner).  Games handicaps and total
#: games are real markets but a different scope, so they are counted out of
#: scope rather than half-supported.
CORE_MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.TENNIS: frozenset({Market.MONEYLINE}),
    Sport.SOCCER: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
}
