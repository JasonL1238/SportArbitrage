"""Cross-book arbitrage detection over normalized quotes.

An arbitrage is a set of bets, one per settlement outcome, whose payouts leave a
profit no matter which outcome occurs.  The arithmetic is trivial; everything
that makes this module non-trivial is about refusing to call something an
arbitrage when it is not one.  The failure mode being defended against is not a
missed opportunity — it is a *false* opportunity, because acting on one loses
real money.

The traps, and how each is closed:

**Mispaired lines.**  ``line`` on a row is stated from that row's own
perspective, so a home run line of -1.5 and an away run line of +1.5 are the two
halves of one market.  Pairing is therefore done on a single canonical
orientation (:func:`canonical_line`) rather than on the raw field, which makes
"home -1.5 against away +2.5" impossible to express.

**Push.**  Home -1 and away +1 are complementary until the home team wins by
exactly one run, at which point *both* legs refund and the profit is zero rather
than the advertised margin.  Rather than carry a boolean that callers may ignore,
every opportunity enumerates its settlement outcomes explicitly and reports the
*minimum* profit across all of them.  A push outcome cannot be overlooked
because it is part of the number.

**Incompatible contracts.**  A two-way first-five-innings moneyline where a tie
refunds is a different contract from a three-way one where a tie is a losing
outcome.  Backing home at the first and away at the second is not an arbitrage:
a tie loses both legs, and on fair prices it looks like a 5% edge.  So legs are
only combined across books offering the same outcome set (:func:`contract_shape`).

**A tie whose settlement cannot be determined.**  Worse than the above, a
two-way partial-period moneyline is *ambiguous*: either the book voids ties, or
it prices a three-way market whose draw leg arrived without a price and was
dropped.  The two readings differ by the entire bankroll and no field on the row
distinguishes them, so no position is reported at all.

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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from src.schema import (
    PERIODS_ALLOWING_DRAW,
    BaseballQuote,
    Market,
    Period,
    QuoteStatus,
    Selection,
    Side,
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
MAX_OBSERVATION_SPREAD = timedelta(seconds=180)

#: Floating-point dust: 1e-9 of implied probability is not an edge.
_EPSILON = 1e-9

#: Above this, a "free" edge is more likely to be a stale price, a palpable
#: error the book will void, or two markets that are not the same contract —
#: than it is to be money.  Real cross-book baseball arbitrage lives in the
#: fractions of a percent to low single digits.  Flagged, not hidden: the
#: judgement of whether an 8% edge is real belongs to whoever reads the report.
IMPLAUSIBLE_MARGIN = 0.05

#: Full-game markets other than the moneyline need the full nine innings to have
#: action, and books do not agree on what happens when they do not get them.
_SHORTENED_GAME_RISK = (
    "full-game {market} needs all nine innings for action, and books disagree on "
    "rain-shortened games (some void both sides, some settle an outcome already "
    "determined) — unlike the moneyline, which has action after five innings "
    "everywhere, so this position is not void-symmetric"
)


# ── the trivial part: the arithmetic ─────────────────────────────────────────


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


def stake_candidates(
    ideal: Sequence[float], total_stake: float, increment: float
) -> list[list[float]]:
    """Every sensible way to round *ideal* onto whole betting units.

    Floor each leg to the increment, then hand out the leftover units — at most
    one per leg, since the floors can only lose a fraction of a unit each.  The
    caller picks between the candidates by the profit floor they produce, which
    is the objective that decides whether a position is risk-free at all.

    Largest-remainder apportionment on its own minimises *rounding error*, which
    is a different objective and not always the same choice: a thin edge can be
    driven negative by the split with the smallest rounding error while another
    whole-unit split of the same bankroll stays profitable.

    ``units_total`` floors rather than rounds, so the position never exceeds the
    bankroll it was given.  Rounding up here lets the allocation *overspend*
    when the increment does not divide the bankroll — 50/50 of 100 in units of 6
    becomes 54/48, which is 102.
    """
    if increment <= 0:
        return [list(ideal)]
    units_total = int(total_stake / increment + 1e-9)
    floors = [int(value / increment + 1e-9) for value in ideal]
    remainder = max(units_total - sum(floors), 0)
    if remainder == 0:
        return [[count * increment for count in floors]]

    candidates: list[list[float]] = []
    for extra in itertools.combinations(range(len(ideal)), min(remainder, len(ideal))):
        counts = list(floors)
        for index in extra:
            counts[index] += 1
        candidates.append([count * increment for count in counts])
    return candidates


# ── settlement model ─────────────────────────────────────────────────────────

WIN = "win"
PUSH = "push"
LOSE = "lose"

#: What a leg returns per unit staked, by settlement result.
_MULTIPLIER = {PUSH: 1.0, LOSE: 0.0}


def canonical_line(quote: BaseballQuote) -> float | None:
    """The market's line in one fixed orientation, independent of selection.

    Run lines are stated per side (home -1.5, away +1.5), so the raw field
    cannot be used to decide whether two rows are opposite halves of the same
    market.  Expressing every run line from the home team's perspective makes
    that decidable.  Totals are already shared by both sides.
    """
    if quote.line is None:
        return None
    if quote.market is Market.RUN_LINE:
        return quote.line if quote.selection is Selection.HOME else -quote.line
    return quote.line


def contract_shape(
    market: Market, period: Period, rows: Iterable[BaseballQuote]
) -> frozenset[Selection]:
    """Which outcomes one book's version of this market settles on.

    This is the *contract*, not an inventory of what happened to be available:
    it is derived from whether the book prices a tie as its own selection, so a
    book whose away price is suspended is still recognised as a two-way market
    rather than as a one-outcome market.  Getting that distinction wrong either
    invents incompatibility or, worse, hides it.

    Only a moneyline on a partial period can differ between books.  A tie is
    impossible over nine innings, and no book prices a draw on a handicap or a
    total.
    """
    if market is Market.MONEYLINE:
        if period in PERIODS_ALLOWING_DRAW and any(
            row.selection is Selection.DRAW for row in rows
        ):
            return frozenset({Selection.HOME, Selection.AWAY, Selection.DRAW})
        return frozenset({Selection.HOME, Selection.AWAY})
    if market is Market.RUN_LINE:
        return frozenset({Selection.HOME, Selection.AWAY})
    return frozenset({Selection.OVER, Selection.UNDER})


def _pushable(market: Market, period: Period, line: float | None) -> bool:
    """Can this market land exactly on its line?

    Only with a whole-number line, because runs are integers.  The ubiquitous
    half-run lines cannot push, which is why alternate integer lines are the
    ones that need care.

    A run line at zero is the exception: it pushes only if the game can end
    level, and a full nine-inning game cannot — extra innings are played until
    someone wins.  Treating a full-game pick'em as pushable understated its
    profit floor as zero when the outcome producing that zero is impossible.
    """
    if line is None or market is Market.MONEYLINE:
        return False
    if not abs(line - round(line)) < 1e-9:
        return False
    if (
        market is Market.RUN_LINE
        and period is Period.FULL_GAME
        and abs(line) < 1e-9
    ):
        return False
    return True


def settlement_outcomes(
    market: Market, period: Period, line: float | None, shape: frozenset[Selection]
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
        if period in PERIODS_ALLOWING_DRAW and Selection.DRAW not in shape:
            # A five-inning game can end level. A book pricing only two
            # selections must therefore be voiding the tie — it cannot be
            # keeping both stakes. So the tie refunds every leg and the
            # position makes nothing, which is emphatically not the advertised
            # margin. Omitting this outcome is how a partial-period "arb" gets
            # reported as risk-free when its real floor is zero.
            outcomes.append(("push", {selection: PUSH for selection in shape}))
        return outcomes

    if market is Market.RUN_LINE:
        outcomes = [
            ("home_covers", {Selection.HOME: WIN, Selection.AWAY: LOSE}),
            ("away_covers", {Selection.HOME: LOSE, Selection.AWAY: WIN}),
        ]
        if _pushable(market, period, line):
            outcomes.append(("push", {Selection.HOME: PUSH, Selection.AWAY: PUSH}))
        return outcomes

    outcomes = [
        ("over", {Selection.OVER: WIN, Selection.UNDER: LOSE}),
        ("under", {Selection.OVER: LOSE, Selection.UNDER: WIN}),
    ]
    if _pushable(market, period, line):
        outcomes.append(("push", {Selection.OVER: PUSH, Selection.UNDER: PUSH}))
    return outcomes


# ── grouping ─────────────────────────────────────────────────────────────────

#: Identity of one *comparable market*: the thing whose two sides can be bet
#: against each other at different books.
MarketGroup = tuple[str, Market, Period, Side | None, float | None]


def group_key(quote: BaseballQuote) -> MarketGroup:
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

    quote: BaseballQuote
    stake: float

    @property
    def source(self) -> str:
        return self.quote.source

    @property
    def selection(self) -> Selection:
        return self.quote.selection

    @property
    def decimal_odds(self) -> float:
        return self.quote.decimal_odds

    @property
    def payout(self) -> float:
        """Total returned if this leg wins, stake included."""
        return self.stake * self.quote.decimal_odds

    def describe(self) -> str:
        line = "" if self.quote.line is None else f" {self.quote.line:+g}"
        return (
            f"{self.quote.source} {self.selection.value}{line} "
            f"@ {self.quote.decimal_odds:.4f} ({self.quote.american_odds:+d}) "
            f"stake {self.stake:.2f}"
        )


@dataclass(frozen=True)
class Opportunity:
    """A position that cannot lose, with the arithmetic to prove it."""

    event_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    market: Market
    period: Period
    side: Side | None
    line: float | None
    """Canonical line — home perspective for run lines."""
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
        return sum(1.0 / leg.decimal_odds for leg in self.legs)

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
        ``roi * total_stake`` whenever rounding bites, and it is zero on any
        market that can push.
        """
        return min(profit for _, profit in self.outcome_profits)

    @property
    def guaranteed_roi(self) -> float:
        return self.guaranteed_profit / self.total_stake

    @property
    def can_push(self) -> bool:
        return any(label == "push" for label, _ in self.outcome_profits)

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

    def describe(self) -> str:
        line = "" if self.line is None else f" @ {self.line:+g}"
        side = f" ({self.side.value})" if self.side else ""
        head = (
            f"{self.event_key} {self.market.value}/{self.period.value}{side}{line}: "
            f"margin {self.margin * 100:.2f}%, guaranteed "
            f"{self.guaranteed_profit:+.2f} on {self.total_stake:.0f}"
        )
        rows = [f"    {leg.describe()}" for leg in self.legs]
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


def find_opportunities(
    quotes: Sequence[BaseballQuote],
    *,
    total_stake: float = DEFAULT_TOTAL_STAKE,
    min_margin: float = DEFAULT_MIN_MARGIN,
    stake_increment: float = STAKE_INCREMENT,
    max_observation_spread: timedelta = MAX_OBSERVATION_SPREAD,
    require_distinct_sources: bool = True,
    as_of: datetime | None = None,
) -> ArbReport:
    """Find every risk-free position in one run's worth of quotes.

    *as_of* is the moment the answer is being asked about — normally now.  Legs
    are required to be close together in time, but that alone does not make a
    position takeable: re-analysing a stored run from last month yields legs that
    agree with each other perfectly and describe games that have already been
    played.  Passing *as_of* excludes those instead of reporting settled games as
    free money.  Left as ``None``, no such gate is applied, which is what a
    historical study of the stored data wants.
    """
    report = ArbReport(opportunities=[], diagnostics=[])

    grouped: dict[MarketGroup, list[BaseballQuote]] = defaultdict(list)
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
        )

    # Ranked by the money actually guaranteed, not by the headline margin. A
    # pushable market can show a 9% margin against a floor of zero, and ordering
    # by margin would list it above a 2% position that really pays.
    report.opportunities.sort(key=lambda o: (o.guaranteed_profit, o.margin), reverse=True)
    return report


def _examine_group(
    *,
    event_key: str,
    market: Market,
    period: Period,
    side: Side | None,
    line: float | None,
    rows: list[BaseballQuote],
    report: ArbReport,
    total_stake: float,
    min_margin: float,
    stake_increment: float,
    max_observation_spread: timedelta,
    require_distinct_sources: bool,
    as_of: datetime | None = None,
) -> None:
    def reject(code: str, detail: str) -> None:
        report.diagnostics.append(
            Diagnostic(
                event_key=event_key, market=market, period=period, code=code, detail=detail
            )
        )

    # The contract each book is offering, judged from *all* its rows for this
    # market including suspended ones: suspending a price does not change which
    # outcomes the market settles on.
    rows_by_source: dict[str, list[BaseballQuote]] = defaultdict(list)
    for row in rows:
        rows_by_source[row.source].append(row)
    shapes: dict[str, frozenset[Selection]] = {
        source: contract_shape(market, period, source_rows)
        for source, source_rows in rows_by_source.items()
    }

    # A game that has already started cannot be backed on both sides, however
    # well the prices agree with each other.
    if as_of is not None and rows and rows[0].commence_time <= as_of:
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
    best: dict[str, dict[Selection, BaseballQuote]] = defaultdict(dict)
    seen: set[tuple[str, Selection, bool]] = set()
    duplicated = False
    for row in active:
        offer = (row.source, row.selection, row.is_alternate)
        if offer in seen:
            duplicated = True
        seen.add(offer)
        existing = best[row.source].get(row.selection)
        if existing is None or row.decimal_odds > existing.decimal_odds:
            best[row.source][row.selection] = row
    if duplicated:
        # Blocked, not merely noted. Two prices for one selection at one book, at
        # the same alternate status, means the parse put something in this group
        # that does not belong to it — so at least one of the two is not the bet
        # it claims to be. Taking the better of them is betting on the fault.
        reject(
            "duplicate_selection",
            "a book published more than one price for the same selection at the same "
            "alternate status, so at least one of them is mis-grouped; no position "
            "is reported from this market",
        )
        return

    # A book whose own complete market prices below 1.0 has been mispaired by the
    # parser — validation reports it as an error. Any leg drawn from such a book
    # is not the bet it appears to be, even when the rest of the position is
    # sound, so the whole group is refused rather than half-trusted.
    for source, selections in best.items():
        shape = shapes[source]
        if not set(selections) >= set(shape):
            continue
        own_margin = arb_margin([selections[selection].decimal_odds for selection in shape])
        if own_margin > _EPSILON:
            reject(
                "source_prices_itself_to_lose",
                f"{source} prices every outcome of this market at a "
                f"{own_margin * 100:.2f}% edge against itself, which does not happen — "
                "its prices or lines are mispaired, so none of them are used",
            )
            return

    # A single book cannot be arbitraged against itself: the check above has
    # already refused the only case where its own prices would allow it.
    if require_distinct_sources and len(best) < 2:
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

        # A partial-period moneyline priced as two-way is ambiguous, and the two
        # readings differ by the entire bankroll. If the book genuinely voids
        # ties, a level score refunds both legs and the position makes nothing.
        # If instead the book prices a three-way market whose draw leg simply
        # arrived without a price — which adapters drop silently — then a level
        # score *loses both legs*. Nothing on the row distinguishes these, so
        # this is not reported as risk-free at all.
        if (
            market is Market.MONEYLINE
            and period in PERIODS_ALLOWING_DRAW
            and Selection.DRAW not in shape
        ):
            reject(
                "ambiguous_tie_settlement",
                f"two-way moneyline on {period.value}: a tie either voids both legs or "
                "loses both, depending on whether this is genuinely a two-way market or "
                "a three-way one whose draw price was dropped — the row cannot say which, "
                "so no position is reported",
            )
            continue

        # A book only has to offer the one selection being taken from it: the
        # normal shape of an arbitrage is the best home price at one book against
        # the best away price at another.
        eligible = [
            source for source in sources if any(selection in best[source] for selection in needed)
        ]
        covered = {
            selection for selection in needed for source in eligible if selection in best[source]
        }
        if covered != needed:
            continue
        # Two books are enough for a three-way position: two legs go on at one
        # book and the third at the other. What is forbidden is *every* leg at
        # one book, which is checked once the legs are chosen.
        if require_distinct_sources and len(eligible) < 2:
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
        )
        if candidates is None:
            continue
        chosen, sum_implied = candidates

        margin = 1.0 - sum_implied
        if margin <= min_margin + _EPSILON:
            continue

        legs_quotes = [chosen[selection] for selection in sorted(needed, key=lambda s: s.value)]

        spread = max(q.observed_at for q in legs_quotes) - min(
            q.observed_at for q in legs_quotes
        )
        if spread > max_observation_spread:
            reject(
                "stale_leg",
                f"legs observed {spread} apart (limit {max_observation_spread}); "
                f"margin would have been {margin * 100:.2f}%",
            )
            continue

        # Legs must agree on who is playing and which way round. Cheap to check
        # and catastrophic to miss: two legs that both back the same team lose the
        # entire bankroll together. Reconciliation makes a mislabelled orientation
        # land in a different group, and validation reports it — but this is the
        # one guard that costs nothing, so it is not left to them.
        identities = {(q.home_team, q.away_team, q.commence_time) for q in legs_quotes}
        if len(identities) > 1:
            reject(
                "legs_disagree_on_the_game",
                "the chosen legs do not describe the same fixture: "
                + "; ".join(
                    f"{q.source}: {q.away_team} @ {q.home_team} {q.commence_time.isoformat()}"
                    for q in legs_quotes
                ),
            )
            continue

        opportunity = _build_opportunity(
            event_key=event_key,
            market=market,
            period=period,
            side=side,
            line=line,
            shape=shape,
            legs_quotes=legs_quotes,
            total_stake=total_stake,
            stake_increment=stake_increment,
        )

        # A thin edge can be smaller than one betting unit, in which case
        # rounding the stakes to whole units turns the guarantee negative. The
        # headline margin still reads positive, so this has to be caught here
        # rather than left for a caller to notice.
        if not opportunity.is_risk_free:
            reject(
                "rounding_destroys_edge",
                f"margin {margin * 100:.2f}% is too thin for a {stake_increment:g}-unit "
                f"stake increment at a bankroll of {total_stake:g}: the worst outcome "
                f"loses {opportunity.guaranteed_profit:.2f}",
            )
            continue

        report.opportunities.append(opportunity)


def _best_assignment(
    *,
    needed: frozenset[Selection],
    eligible: Sequence[str],
    best: dict[str, dict[Selection, BaseballQuote]],
    require_distinct_sources: bool,
) -> tuple[dict[Selection, BaseballQuote], float] | None:
    """Pick the book for each selection that minimises total implied probability.

    Searched rather than chosen greedily, because the best price per selection
    taken independently can put every leg at one book — which is a mispricing to
    reject, not a position to take.  Two legs *sharing* a book is fine, so the
    only constraint is that the position spans at least two books.  The space is
    a handful of books raised to at most three selections.
    """
    selections = sorted(needed, key=lambda s: s.value)
    winner: tuple[dict[Selection, BaseballQuote], float] | None = None

    source_options = [
        [source for source in eligible if selection in best[source]] for selection in selections
    ]
    if any(not options for options in source_options):
        return None

    for combination in itertools.product(*source_options):
        if require_distinct_sources and len(set(combination)) < 2:
            continue
        assignment = {
            selection: best[source][selection]
            for selection, source in zip(selections, combination)
        }
        total_implied = sum(1.0 / quote.decimal_odds for quote in assignment.values())
        if winner is None or total_implied < winner[1]:
            winner = (assignment, total_implied)
    return winner


def _build_opportunity(
    *,
    event_key: str,
    market: Market,
    period: Period,
    side: Side | None,
    line: float | None,
    shape: frozenset[Selection],
    legs_quotes: Sequence[BaseballQuote],
    total_stake: float,
    stake_increment: float,
) -> Opportunity:
    odds = [quote.decimal_odds for quote in legs_quotes]
    ideal = stake_split(odds, total_stake)
    outcomes = settlement_outcomes(market, period, line, shape)

    def evaluate(stakes: Sequence[float]) -> tuple[list[tuple[str, float]], float]:
        """Profit in every settlement outcome, and the floor across them."""
        staked = sum(stakes)
        profits: list[tuple[str, float]] = []
        for label, results in outcomes:
            returned = 0.0
            for quote, stake in zip(legs_quotes, stakes):
                # A selection absent from an outcome's mapping contributes
                # nothing, which is the right treatment for a leg that does not
                # participate in that outcome.
                result = results.get(quote.selection, LOSE)
                multiplier = quote.decimal_odds if result == WIN else _MULTIPLIER[result]
                returned += stake * multiplier
            profits.append((label, returned - staked))
        return profits, min(profit for _, profit in profits)

    # Choose the whole-unit split by the profit floor it produces, because that
    # floor is what decides whether the position is risk-free. The split with the
    # smallest rounding error is a different choice and can be the one that turns
    # a thin but genuine edge negative.
    best_stakes = max(
        stake_candidates(ideal, total_stake, stake_increment), key=lambda s: evaluate(s)[1]
    )
    legs = tuple(
        ArbLeg(quote=quote, stake=stake) for quote, stake in zip(legs_quotes, best_stakes)
    )
    staked = sum(leg.stake for leg in legs)
    profits, _ = evaluate(best_stakes)

    notes: list[str] = []
    if any(label == "push" for label, _ in profits):
        if line is None:
            notes.append(
                f"{period.value} can end level and this is a two-way market, so a tie voids "
                "every leg — the guaranteed profit is therefore zero, not the margin"
            )
        else:
            notes.append(
                f"line {line:g} is a whole number, so the market can land exactly on it and "
                "refund every leg — the guaranteed profit is therefore zero"
            )
    if any(quote.is_alternate for quote in legs_quotes):
        notes.append("uses an alternate line, which is usually offered at a lower limit")
    if Selection.DRAW in shape:
        notes.append("three-way market: all three outcomes are covered")
    if market is not Market.MONEYLINE:
        if period is Period.FULL_GAME:
            notes.append(_SHORTENED_GAME_RISK.format(market=market.value))
        else:
            notes.append(
                f"partial-period {market.value} ({period.value}); confirm both books void "
                "it the same way before betting"
            )

    margin = 1.0 - sum(1.0 / odd for odd in odds)
    if margin > IMPLAUSIBLE_MARGIN:
        notes.append(
            f"margin of {margin * 100:.1f}% is implausibly large for a real cross-book "
            "price: suspect a stale quote, a palpable error the book will void, or two "
            "markets that are not in fact the same contract — verify before staking"
        )

    # A stake cap only binds if every book states one; an unstated limit is
    # unknown, not unlimited, so a partial answer would be misleading.
    limits = [quote.limit_amount for quote in legs_quotes]
    max_total_stake: float | None = None
    if any(limit is not None and limit <= 0 for limit in limits):
        # A stated limit of zero is "this book will not take the bet", which is
        # not the same fact as "no limit is stated" and must not be reported as it.
        max_total_stake = 0.0
        notes.append("a book states a limit of zero, so this position cannot be placed")
    elif all(limit is not None for limit in limits):
        total_implied = sum(1.0 / odd for odd in odds)
        # stake_i = T * (1/d_i)/S <= limit_i  =>  T <= limit_i * S * d_i
        cap = min(
            limit * total_implied * odd  # type: ignore[operator]
            for limit, odd in zip(limits, odds)
        )
        # Floored to whole betting units. The cap is derived from ideal stakes, so
        # staking it and then rounding to whole units pushes the binding leg a
        # fraction over the limit the book actually stated — and a leg the book
        # rejects is a leg that is not on, which is exactly when the position
        # stops being risk-free.
        if stake_increment > 0:
            cap = int(cap / stake_increment + 1e-9) * stake_increment
        max_total_stake = cap

    return Opportunity(
        event_key=event_key,
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
    quotes: Sequence[BaseballQuote],
) -> dict[MarketGroup, dict[Selection, BaseballQuote]]:
    """Best available price for every selection of every comparable market.

    Useful on its own: it is the line-shopping view, and it is what an
    opportunity is drawn from.
    """
    surface: dict[MarketGroup, dict[Selection, BaseballQuote]] = defaultdict(dict)
    for quote in quotes:
        if quote.status is not QuoteStatus.ACTIVE:
            continue
        bucket = surface[group_key(quote)]
        existing = bucket.get(quote.selection)
        if existing is None or quote.decimal_odds > existing.decimal_odds:
            bucket[quote.selection] = quote
    return dict(surface)
