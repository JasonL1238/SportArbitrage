"""Validation of normalized rows — an attempt to prove the data wrong.

The schema already rejects individually malformed rows.  This module checks the
things a single row cannot know about itself: whether a market is internally
coherent, whether two books describe the same game the same way, whether the
prices imply a bookmaker margin that a real book would actually offer, and
whether an expected market has silently stopped arriving.

Findings are graded.  ``ERROR`` means the data is wrong or unusable and the run
should not be treated as clean.  ``WARNING`` means it is suspicious and worth
looking at — a real book occasionally does something unusual, and treating that
as corruption would make the pipeline cry wolf.

**Every threshold that differs by sport is looked up, never hardcoded.**  That is
the whole difference between this module and its baseball-only ancestor, and each
of the constants that used to be global was actively wrong for at least one of
the sports now collected:

* A 30-day futures horizon rejects *every* real NFL and NHL game, because the
  NFL's whole season is priced in July and NHL openers are two months out.  The
  horizon comes from :attr:`~src.leagues.League.max_schedule_horizon`.
* A 20-minute start-time tolerance splits nearly every tennis match into two
  events, because each book publishes its own "not before" estimate.  The
  tolerance comes from :attr:`~src.leagues.League.time_disagreement_threshold`,
  which is deliberately tighter than the clustering width used to *join* them.
* "A draw on a moneyline is a misparsed third runner" is true for a full-game
  baseball or hockey moneyline and false for a soccer 90-minute or a hockey
  regulation moneyline.  Draw legality comes from
  :func:`src.vocab.draw_is_priced`, which is also what decides how many outcomes
  a *complete* market has — and therefore what the overround must be summed over.
* "Home and away are facts the books agree on" is true in team sports and
  meaningless in tennis, where :func:`src.events.orient` imposes the ordering and
  the books have no opinion at all.  Checking agreement there would report a
  disagreement that does not exist.
* A plausible total is 4-20 runs in MLB and 20-80 points in the NFL.  One shared
  range cannot catch both an 8000-run total (Kambi sends lines in thousandths)
  and a 2.5-point NFL total (a market mapped to the wrong sport).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations
from enum import StrEnum
from statistics import median
from typing import Any, Collection, Iterable, Mapping, Sequence

from src.arb import MAX_OBSERVATION_SPREAD
from src.commission import net_decimal_odds
from src.leagues import League, is_known
from src.leagues import league as get_league
from src.normalize import (
    MAX_DECIMAL_ODDS,
    MIN_DECIMAL_ODDS,
    american_to_decimal,
    is_plausible_decimal_odds,
)
from src.participants import canonical_participant
from src.schema import (
    CORE_MARKETS_BY_SPORT,
    Market,
    Period,
    Quote,
    QuoteStatus,
    Selection,
    Sport,
    draw_is_priced,
    scoring_unit,
)

#: The scoring windows that cover a whole contest.  Both are "the game" as a book
#: settles it — ``FULL_GAME`` including overtime/shootout, ``REGULATION``
#: excluding it — and for both, the shape of the headline moneyline is not in
#: doubt: it is three-way exactly where the sport prices a draw.  A *partial*
#: window is different, because a two-way tie-void product genuinely exists there
#: (a first-five-innings moneyline is sold both ways), so a missing draw is only
#: suspicious rather than certainly wrong.
FULL_CONTEST_WINDOWS: frozenset[Period] = frozenset({Period.FULL_GAME, Period.REGULATION})


def core_markets(sport: Sport) -> frozenset[tuple[Market, Period]]:
    """Markets a book is expected to price for every game it lists, per sport.

    Taken from :data:`src.vocab.CORE_MARKETS_BY_SPORT` on the sport's full-game
    window.  If one vanishes entirely a criterion/marketType label has probably
    been renamed upstream, which would otherwise look like a quiet drop in row
    count.  Tennis carries only the moneyline, so it is not faulted for having no
    totals — the previous hardcoded ``{moneyline, run line, total runs}`` would
    have reported every tennis book as broken.
    """
    return frozenset(
        (market, Period.FULL_GAME) for market in CORE_MARKETS_BY_SPORT[sport]
    )


#: A complete market always prices in the book's favour.  A sum below 1.0 means
#: the book is offering a guaranteed loss to itself, which in practice means the
#: parser paired the wrong prices or lines — or dropped a leg.  How many legs
#: "complete" means is sport-dependent; see :func:`_required_selections`.
MIN_OVERROUND = 1.0
MAX_OVERROUND = 1.6

#: Float dust is not a losing price.  An exactly-fair pair — vi_hardrock's
#: TOR@HOU spread read ``away +1.5 −170 / home −1.5 +170`` mid-move, whose
#: implied probabilities are 17/27 and 10/27 — sums to 1.0 in arithmetic and
#: 0.9999999999999998 in floats, and the strict ``< MIN_OVERROUND`` comparison
#: produced an error whose own message refuted it: "sum to 1.0000 < 1.0".
#: Breaking even is not "pricing itself to lose"; the tolerance is the same
#: 1e-9 the source-contract suite uses, so the two judges of *that* question
#: cannot disagree at the boundary.  ``refuse_mid_move_pairings`` deliberately
#: asks a stricter one — an HTML tracker column must carry some margin to
#: publish at all — which is why an at-fair tracker pairing never reaches
#: this gate: the strict judge deleted it at parse.
OVERROUND_FLOAT_TOLERANCE = 1e-9

#: The same floor for an **order-driven** venue, where the reasoning above does
#: not hold.
#:
#: A sportsbook quotes both sides of a market itself and will not price itself to
#: lose, so a sum below 1.0 there is evidence about the *parser*.  An exchange or
#: prediction market publishes two independent books; nobody quoted them against
#: each other, and when the two best asks briefly cross, that is a real — small,
#: fleeting — intra-venue arbitrage rather than a mispairing.  Failing the run
#: over it would report a true observation as a parse fault, in the words "the
#: prices or lines are mispaired", which are false.
#:
#: The captured slate is already on the boundary: Kalshi's tightest two
#: moneylines sum to exactly 1.0000 and Matchbook's to 1.0023, so one cent of
#: movement on any of them turns a healthy run into a failed one.
#:
#: Not removed, only widened, because a *large* negative sum on an order book is
#: still evidence of a fault — a crossed market of more than a couple of percent
#: does not persist for the seconds it takes to collect it.
#: The systematic-mispricing gate.  A venue whose *typical* market prices below
#: fair is not a venue with thin books, it is a parser reading one side of the
#: order book where the other is meant — and the median says so where a rate
#: cannot.
#:
#: Measured on this session's live slates, honest median gross overround per
#: order-driven source: Kalshi 1.0100, SX Bet 1.0156-1.0200, Polymarket 1.0300,
#: Matchbook 1.0323-1.0346, Smarkets 1.1055-1.1121.  Every one holds a spread,
#: which is what a venue is.  A trap deep enough to manufacture a cross-book
#: edge drags the median under 1.0; the deep lay-read-as-back trap measured on
#: the fixtures puts 98% of markets below fair, so its median is far under.
#:
#: This replaced a *rate* gate — "more than a quarter of complete markets sum
#: below 0.99" — which the live data killed: honest sources run 15-20% by that
#: measure (Polymarket touched 20.2%) against a 25% threshold, and a realistic
#: 1.5% trap moves Matchbook's rate from 16% to 18%.  The honest band and the
#: trap band overlapped, so the gate could convict a healthy venue — failing the
#: run and telling the operator every price from it is suspect — while missing
#: the trap it was for.  The median has real separation on the same data.
#:
#: What this gate catches, measured by re-pricing one venue's whole book on a
#: captured 46,220-row slate (uniform shift, decimal and American kept
#: consistent):
#:
#: ==========  ======  ======  ======
#: venue       1.5%    3%      6%
#: ==========  ======  ======  ======
#: Kalshi      caught  caught  caught
#: Polymarket  --      caught  caught
#: SX Bet      --      caught  caught
#: Matchbook   --      (a)     caught
#: Smarkets    --      (a)     (a)
#: ==========  ======  ======  ======
#:
#: (a) not by this gate — by per-market ``negative_overround`` errors, which
#: fail the run for a different stated reason.
#:
#: **A uniform misread shallower than the venue's own spread is not detected by
#: anything, and no honest gate can be built from this data.** The obvious
#: candidate — a source's signed median deviation from the cross-source
#: consensus — was measured and rejected: honest venues on that slate run
#: SX Bet -0.0112, Polymarket -0.0102, Kalshi -0.0089, Matchbook -0.0055,
#: Smarkets -0.0042, while a 1.5% misread moves each by only ~0.006, so
#: *trapped Smarkets* (-0.0093) sits inside the honest band, below *honest
#: SX Bet*. Any flat threshold convicts a healthy venue before it catches a
#: trapped one. ``_check_price_agreement`` cannot see it either: its bar is a
#: 0.15 deviation in implied probability (:data:`MAX_PRICE_DEVIATION`, set where
#: it is because the largest honest deviation observed is 0.083) and a 1.5%
#: shift moves implied probability by about 0.007.
#:
#: The residual exposure is bounded and worth stating plainly: such a misread
#: inflates that venue's apparent edge by the size of the shift, so it can
#: manufacture *thin* phantom positions. It cannot manufacture large ones —
#: :data:`src.arb.REFUSE_MARGIN` refuses those — and the per-market net check
#: still fires wherever the shift crosses the book after commission.
#:
#: A second thing it cannot catch, for the same reason: a misread confined to
#: **one market type**. The median's premise is that a misread moves every
#: market the same way, and adapters parse each market type separately, so a
#: moneyline-only misread is at least as likely a parser bug as a whole-book
#: one. Trapping only Kalshi's moneyline series leaves its median at 1.0100 —
#: unmoved, because the other market types outvote it — and the gate stays
#: silent while the per-market check reports the individual crossings. Judging
#: the median per ``(source, market)`` instead was considered and not done: it
#: multiplies the number of small samples, which is precisely what the sample
#: floor above exists to keep from convicting a healthy venue.
#: ...over at least this many complete markets.  Twenty, not five: at five, a
#: venue with three markets crossed by half a cent — every one of them *benign*
#: after commission, and printed as such two lines above — had its median drag
#: under 1.0 and the run failed with "every price from this source is suspect".
#: A quiet scoped hour is not evidence about a parser.  Every honest source on
#: the live slates carries hundreds of complete markets, and so does any slate a
#: trap could hide in.
MIN_SUB_UNITY_MARKETS = 20

#: How deep a *net-of-commission* crossing an order book may show before it is
#: judged a fault rather than a market state.  A book crossed by less than a
#: cent is a real, fleeting thing two resting orders produce — this module's
#: notes record Kalshi's tightest live moneylines summing to exactly 1.0000,
#: one cent of movement away — while a crossing deeper than that does not
#: survive the seconds it takes to collect it.
ORDER_BOOK_CROSSING_TOLERANCE = 0.01

#: American and decimal odds are both published by some books, and the American
#: value is an integer, so the two can only agree to within that rounding.
#:
#: The tolerance applies to the *net payout* — ``decimal - 1`` — not to the
#: decimal price, because that is the quantity American odds actually encode.
#: A tolerance on the decimal price cannot be scale-correct at both ends: 2% of
#: 1.02 spans every price from -5000 to -2475, hiding a factor-of-two error in
#: the payout, while 2% of 25.0 spans 50 American points where one point is the
#: real granularity.  Against the net payout, one American point is worth about
#: 0.5% at even money and less everywhere else, so 1% catches genuine encoding
#: errors at every price and never fires on honest rounding.
AMERICAN_PAYOUT_TOLERANCE = 0.01

#: A source above this suspended fraction is contributing almost nothing that can
#: be bet, and needs to disagree with another source by at least the gap below
#: before that is called a fault rather than a genuinely closed slate.
MAX_SUSPENDED_FRACTION = 0.8
MIN_SUSPENSION_GAP = 0.5

#: A listed game should not already be long past.  Unlike the *horizon*, this one
#: is genuinely sport-independent: no book prices a game that finished yesterday,
#: in any sport.
MAX_COMMENCE_LOOKBACK = timedelta(hours=18)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    source: str | None = None
    event_key: str | None = None

    def __str__(self) -> str:
        where = " ".join(part for part in (self.source, self.event_key) if part)
        return f"[{self.severity.value}] {self.code}: {self.message}" + (f" ({where})" if where else "")


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    quote_count: int = 0
    market_count: int = 0
    event_count: int = 0
    source_count: int = 0

    def add(
        self,
        severity: Severity,
        code: str,
        message: str,
        *,
        source: str | None = None,
        event_key: str | None = None,
    ) -> None:
        self.findings.append(
            Finding(severity=severity, code=code, message=message, source=source, event_key=event_key)
        )

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        return (
            f"{verdict}: {self.quote_count} quotes, {self.market_count} markets, "
            f"{self.event_count} events, {self.source_count} sources, "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings"
        )


def validate(
    quotes: Sequence[Quote],
    *,
    capabilities: Mapping[tuple[str, str], frozenset[Market]] | None = None,
    order_book_sources: Collection[str] = (),
    consensus_sources: Collection[str] = (),
) -> ValidationReport:
    """Run every check over one run's worth of normalized rows.

    *capabilities* maps ``(source_key, league)`` to the markets that source
    **claimed** to price on this run, as published by
    :meth:`src.sources.base.OddsSource.capabilities`.  Supplying it stops the
    coverage check faulting a book for a market it never offered — Pinnacle
    prices no tennis totals, and a ``--tier core`` run deliberately does not ask
    FanDuel for soccer handicaps — while leaving the check's real job intact:
    a market a source *does* claim and has stopped returning is still an error.

    Left ``None``, every source is held to its sport's full core market set,
    which is the older and stricter behaviour.

    *order_book_sources* names the venues where a row exists **only because
    somebody offered it** — exchanges and prediction markets.  Two checks change
    meaning there, and both were written when every source was a sportsbook:

    * A book that prices a market prices all of it, so a missing draw leg is a
      leg the parser dropped.  On an exchange the draw runner is there and
      nobody has bid on it, which is an empty book rather than a parser fault.
    * A book that lists a fixture posts a spread on it.  An exchange lists the
      market and waits; on a thin slate the spread genuinely has no resting
      order.

    Reported as warnings rather than errors for those sources — still visible,
    because a persistently empty market is worth knowing about, but not a reason
    to call the run unclean.  A sportsbook is held to the original bar.

    *consensus_sources* names feeds whose numbers are **context, not a current
    price** — Action Network's Open column publishes the line a market opened
    at, and :mod:`src.arb` and :mod:`src.betlinks` already refuse it as a leg
    (``CONSENSUS_FEEDS``).  An opening line that differs from today's prices,
    or an opening handicap that favours the side the market has since moved
    away from, is what an opening line *is* — run 16 graded both as ERRORs and
    trained the operator to scroll past the codes that matter.  So these feeds
    are excluded from the two cross-source *price* checks (agreement and line
    orientation), on both sides: not judged, and not part of the consensus
    others are judged against.  Every identity check still sees them — a
    consensus feed disagreeing about who is playing is still a real signal —
    and their own rows are still held to row- and market-level coherence.
    """
    report = ValidationReport(
        quote_count=len(quotes),
        event_count=len({q.event_key for q in quotes}),
        source_count=len({q.source for q in quotes}),
    )
    if not quotes:
        report.add(Severity.ERROR, "no_quotes", "validation received zero quotes")
        return report

    order_driven = frozenset(order_book_sources)
    consensus_feeds = frozenset(consensus_sources)
    current_priced = (
        [quote for quote in quotes if quote.source not in consensus_feeds]
        if consensus_feeds
        else quotes
    )
    _check_rows(quotes, report)
    _check_duplicates(quotes, report)
    markets = _group_markets(quotes)
    report.market_count = len(markets)
    _check_markets(markets, report, order_driven)
    _check_cross_source(quotes, report)
    _check_split_fixtures(quotes, report)
    _check_line_orientation(current_priced, report)
    _check_price_agreement(current_priced, report, order_driven)
    _check_observation_window(quotes, report)
    _check_coverage(quotes, report, capabilities or {}, order_driven)
    return report


# ── consensus ────────────────────────────────────────────────────────────────


def _modal(values: Mapping[str, Any]) -> Any:
    """The value most sources agree on, ties broken deterministically.

    Every cross-source check below is an **order statistic** if it is phrased as
    "do any two sources differ", and an order statistic fires near-certainly once
    there are enough sources: with three books a league disagreement is worth
    reading, with thirty it is a warning on every shared event and it buries the
    findings that mean something.  Phrasing them as "which sources differ from
    the consensus" keeps the signal proportional to the number of *outliers*
    rather than to the number of sources.

    Ties are broken on the value's own text so the answer does not depend on
    dictionary order — two sources against two is not a consensus, and whichever
    way it resolves, it must resolve the same way twice.
    """
    counts = Counter(values.values())
    best = max(counts.values())
    return sorted((value for value, n in counts.items() if n == best), key=str)[0]


def _outliers(values: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    """``(consensus, {source: differing value})``."""
    consensus = _modal(values)
    return consensus, {
        source: value for source, value in values.items() if value != consensus
    }


# ── row-level ────────────────────────────────────────────────────────────────


def _competition(quote: Quote) -> League | None:
    """The row's league, or ``None`` when it names one nobody registered.

    An unknown league is reported once per row by :func:`_check_rows`; every
    league-dependent threshold is then skipped rather than defaulted, because a
    default here is a guess about how far ahead a fixture may be scheduled and
    how big a total may be — and guessing turns a real error into a pass.
    """
    return get_league(quote.league) if is_known(quote.league) else None


def _check_rows(quotes: Iterable[Quote], report: ValidationReport) -> None:
    # Resolution is pure and the same names recur on every row of an event, so
    # memoising keeps this linear in distinct participants rather than in rows.
    resolved: dict[tuple[str, str], str | None] = {}

    def participant_key(name: str, competition: League) -> str | None:
        cache_key = (name, competition.key)
        if cache_key not in resolved:
            found = canonical_participant(name, competition)
            resolved[cache_key] = None if found is None else found.key
        return resolved[cache_key]

    for quote in quotes:
        competition = _competition(quote)
        if competition is None:
            report.add(
                Severity.ERROR,
                "unknown_league",
                f"league {quote.league!r} is not registered in src.leagues, so nothing "
                "about this row's schedule, participants or plausible lines can be "
                "checked — and src.events leaves it out of reconciliation",
                source=quote.source,
                event_key=quote.event_key,
            )
        else:
            _check_identity(quote, competition, participant_key, report)
            _check_schedule(quote, competition, report)
            _check_line_plausibility(quote, competition, report)

        # Draw legality is a property of the scoring window, not of the market:
        # a draw on a full-game baseball or hockey moneyline is a misparsed third
        # runner, while a draw on a soccer 90-minute or a hockey regulation
        # moneyline is the market working correctly.  The schema enforces this at
        # construction; it is repeated here because rows also arrive from storage
        # and from ``model_copy``, neither of which re-runs the schema.
        if quote.selection is Selection.DRAW and not draw_is_priced(quote.sport, quote.period):
            report.add(
                Severity.ERROR,
                "illegal_draw",
                f"a draw is priced on {quote.sport.value}/{quote.period.value}, where a "
                "level result is not a settlement outcome — this is a third runner or a "
                "different market parsed as the moneyline",
                source=quote.source,
                event_key=quote.event_key,
            )

        _check_price_encoding(quote, report)

        if quote.last_change_at is not None and quote.last_change_at > quote.observed_at + timedelta(
            minutes=5
        ):
            report.add(
                Severity.ERROR,
                "last_change_in_future",
                f"source reports the price changed at {quote.last_change_at.isoformat()}, "
                f"after we observed it at {quote.observed_at.isoformat()}",
                source=quote.source,
                event_key=quote.event_key,
            )


def _check_identity(
    quote: Quote,
    competition: League,
    participant_key,
    report: ValidationReport,
) -> None:
    """Participants must resolve, and the row's keys must be what they resolve to.

    The keys are what everything joins on, so a display name that no longer
    resolves — or that resolves to a *different* competitor than the key on the
    row claims — breaks the join silently rather than loudly.
    """
    if quote.sport is not competition.sport:
        report.add(
            Severity.ERROR,
            "league_sport_mismatch",
            f"row says sport={quote.sport.value} but league {competition.key} is "
            f"{competition.sport.value} — one of the two is a mapping error, and every "
            "settlement rule is looked up by sport",
            source=quote.source,
            event_key=quote.event_key,
        )

    for role, name, claimed in (
        ("home", quote.home_team, quote.home_participant),
        ("away", quote.away_team, quote.away_participant),
    ):
        found = participant_key(name, competition)
        if found is None:
            report.add(
                Severity.ERROR,
                "unresolved_participant",
                f"{role} name {name!r} does not resolve to a competitor in "
                f"{competition.key} — a futures container or an unmapped spelling",
                source=quote.source,
                event_key=quote.event_key,
            )
        elif found != claimed:
            report.add(
                Severity.ERROR,
                "participant_key_mismatch",
                f"{role} name {name!r} resolves to {found} but the row carries "
                f"{claimed} — the display name and the join key describe different "
                "competitors",
                source=quote.source,
                event_key=quote.event_key,
            )

    # Where home advantage is not a property of the fixture, src.events.orient
    # imposes one deterministic order (by participant key) so that every book
    # lands on the same event key without having to agree on anything but the two
    # names.  A row that ignores it is not a disagreement between books — it is a
    # row that will never join, so it is checked per row rather than per event.
    if not competition.has_home_away and quote.away_participant > quote.home_participant:
        report.add(
            Severity.ERROR,
            "participant_order_not_canonical",
            f"{competition.key} has no home side, so src.events.orient orders the two "
            f"participants by key; this row has away={quote.away_participant} after "
            f"home={quote.home_participant}, so its event key cannot match another "
            "book's for the same match",
            source=quote.source,
            event_key=quote.event_key,
        )


def _check_schedule(quote: Quote, competition: League, report: ValidationReport) -> None:
    """Futures leakage, per league.

    The horizon has to come from the league: baseball books post lines a few days
    out, while the NFL's entire season is priced in July and NHL openers are two
    months away.  The single 30-day rule this replaces would have rejected every
    real NFL and NHL game collected.  It still catches the absurd, which is what
    it is for — FanDuel's "NFL Specials" event carries an openDate of 2030-12-01.
    """
    horizon = competition.max_schedule_horizon
    if quote.commence_time > quote.observed_at + horizon:
        ahead = quote.commence_time - quote.observed_at
        report.add(
            Severity.ERROR,
            "commence_time_too_far",
            f"game starts {quote.commence_time.isoformat()}, {ahead.days} days after "
            f"collection, but {competition.key} fixtures are never scheduled more than "
            f"{horizon.days} days ahead — likely a futures market parsed as a game",
            source=quote.source,
            event_key=quote.event_key,
        )
    elif quote.commence_time < quote.observed_at - MAX_COMMENCE_LOOKBACK:
        report.add(
            Severity.WARNING,
            "commence_time_stale",
            f"game started {quote.commence_time.isoformat()}, well before collection",
            source=quote.source,
            event_key=quote.event_key,
        )


def _check_line_plausibility(
    quote: Quote, competition: League, report: ValidationReport
) -> None:
    """A line outside the league's range is a units error or a wrong sport.

    Both failures are real and neither is visible on the row itself: Kambi sends
    odds *and lines* in thousandths, so a total of 8.0 arrives as ``8000``; and a
    market mapped to the wrong sport carries a perfectly well-formed line that is
    absurd for the sport it landed in — a 2.5-point NFL total is a soccer goals
    line wearing a football label.
    """
    if quote.line is None:
        return
    # The *ladder* bound, not the main-line band: a book quotes alternates well
    # past its headline number, and Kalshi's MLB totals reach 1.5 runs.  See
    # League.plausible_line_range for which end of it does the real work.
    low, high = competition.plausible_line_range
    unit = scoring_unit(quote.sport, quote.period)

    if quote.market in (Market.TOTAL, Market.TEAM_TOTAL):
        # The range is stated for a full-contest total.  A partial window scores
        # less, and one team scores less than both, so only the upper bound
        # carries over to those.
        if quote.market is Market.TEAM_TOTAL or quote.period not in FULL_CONTEST_WINDOWS:
            low = 0.0
        if not low - 1e-9 <= quote.line <= high + 1e-9:
            report.add(
                Severity.ERROR,
                "implausible_total",
                f"{quote.market.value}/{quote.period.value} line {quote.line:g} is outside "
                f"the plausible {competition.key} range of {low:g}-{high:g} {unit} — a "
                "units/scaling error (Kambi sends lines in thousandths) or a market mapped "
                "to the wrong sport",
                source=quote.source,
                event_key=quote.event_key,
            )
        return

    if quote.market is Market.SPREAD and abs(quote.line) > high + 1e-9:
        report.add(
            Severity.ERROR,
            "line_out_of_range",
            f"{quote.market.value} line {quote.line:g} exceeds what a whole "
            f"{competition.key} game scores ({high:g} {unit}) — check for a units/scaling "
            "error",
            source=quote.source,
            event_key=quote.event_key,
        )


def _check_price_encoding(quote: Quote, report: ValidationReport) -> None:
    expected_prob = 1.0 / quote.decimal_odds
    if abs(quote.implied_probability - expected_prob) > 1e-6:
        report.add(
            Severity.ERROR,
            "implied_probability_mismatch",
            f"implied_probability {quote.implied_probability:.6f} does not match "
            f"1/{quote.decimal_odds:.4f} = {expected_prob:.6f}",
            source=quote.source,
            event_key=quote.event_key,
        )

    if not is_plausible_decimal_odds(quote.decimal_odds):
        report.add(
            Severity.ERROR,
            "implausible_odds",
            f"decimal_odds {quote.decimal_odds} is outside the range any book "
            f"publishes ({MIN_DECIMAL_ODDS}-{MAX_DECIMAL_ODDS}) — check for a "
            "units or scaling error",
            source=quote.source,
            event_key=quote.event_key,
        )

    if quote.american_odds == 0:
        report.add(
            Severity.ERROR,
            "zero_american_odds",
            "american_odds is 0, which is not a price",
            source=quote.source,
            event_key=quote.event_key,
        )
        return

    # Compared on net payout rather than on the decimal price, so the tolerance
    # means the same thing for a -5000 favourite as for a +2400 longshot. Guarded
    # because an out-of-domain American value is itself the corruption being
    # looked for.
    try:
        from_american = american_to_decimal(quote.american_odds)
    except ValueError as exc:
        report.add(
            Severity.ERROR,
            "american_odds_not_a_price",
            f"american_odds {quote.american_odds:+d} is not a price: {exc}",
            source=quote.source,
            event_key=quote.event_key,
        )
        return

    payout = quote.decimal_odds - 1.0
    drift = abs((from_american - 1.0) - payout) / payout
    if drift > AMERICAN_PAYOUT_TOLERANCE:
        report.add(
            Severity.ERROR,
            "odds_format_mismatch",
            f"american {quote.american_odds:+d} implies a net payout of "
            f"{from_american - 1.0:.4f} but decimal_odds {quote.decimal_odds:.4f} "
            f"implies {payout:.4f} — {drift * 100:.1f}% apart",
            source=quote.source,
            event_key=quote.event_key,
        )


def _check_duplicates(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """Two rows for the same priced selection from one book is a parser fault.

    It is how pitcher-conditional moneylines or alternate-line markets leak in
    as if they were the primary market.
    """
    seen: dict[tuple[str, ...], Quote] = {}
    for quote in quotes:
        key = quote.dedup_key
        previous = seen.get(key)
        if previous is None:
            seen[key] = quote
            continue
        if previous.decimal_odds == quote.decimal_odds:
            report.add(
                Severity.WARNING,
                "duplicate_quote",
                f"identical row repeated for {'/'.join(key[1:])}",
                source=quote.source,
                event_key=quote.event_key,
            )
        else:
            report.add(
                Severity.ERROR,
                "conflicting_duplicate",
                f"two different prices for the same selection {'/'.join(key[1:])}: "
                f"{previous.decimal_odds:.4f} (market {previous.source_market_id}) vs "
                f"{quote.decimal_odds:.4f} (market {quote.source_market_id})",
                source=quote.source,
                event_key=quote.event_key,
            )


# ── market-level ─────────────────────────────────────────────────────────────


def _group_markets(quotes: Iterable[Quote]) -> dict[tuple[str, str, str], list[Quote]]:
    grouped: dict[tuple[str, str, str], list[Quote]] = defaultdict(list)
    for quote in quotes:
        grouped[quote.market_key].append(quote)
    return grouped


def _required_selections(sport: Sport, market: Market, period: Period) -> set[Selection]:
    """The selections a *complete* version of this market prices.

    This is the sport-aware replacement for "a moneyline has two sides unless a
    draw happens to be present".  A soccer 90-minute moneyline and a hockey
    regulation moneyline have three outcomes; every other moneyline collected has
    two.  Deriving the expectation from the rows that arrived can never detect a
    dropped leg — and a two-way market missing a leg is otherwise indistinguish-
    able from a three-way market, since both hold two rows.
    """
    if market is Market.MONEYLINE:
        required = {Selection.HOME, Selection.AWAY}
        if draw_is_priced(sport, period):
            required.add(Selection.DRAW)
        return required
    if market is Market.SPREAD:
        return {Selection.HOME, Selection.AWAY}
    return {Selection.OVER, Selection.UNDER}


def _check_markets(
    markets: dict[tuple[str, str, str], list[Quote]],
    report: ValidationReport,
    order_book_sources: frozenset[str] = frozenset(),
) -> None:
    # Per order-driven source: (complete two-way-or-more markets, of which
    # gross-sub-1.0).  A *systematically* sub-1.0 source is the bid-read-as-ask
    # parser trap; a sporadic one is a thin book — the distinction lives at the
    # source level, not per market, and is judged after the loop.
    sub_unity: dict[str, list[int]] = {}
    for rows in markets.values():
        first = rows[0]
        source = first.source
        selections = {row.selection for row in rows}
        label = (
            f"{first.sport.value} {first.market.value}/{first.period.value}"
            + (f"/{first.side.value}" if first.side else "")
        )
        line_label = "" if first.line is None else f" @ {first.line:g}"

        # Homogeneity, before anything is inferred from row zero. Every check
        # below reads the market's identity off the first row and applies it to
        # the group, so a group that is not one market makes all of them
        # meaningless — and the failure is silent, because a fused group still
        # looks complete and still has a plausible-looking line set.  ``sport`` is
        # part of this now: the settlement rules every other check consults are
        # looked up by sport, so a group spanning two sports is a group whose
        # rules are unknowable.
        mixed = {
            name
            for name in ("sport", "market", "period", "side")
            if len({getattr(row, name) for row in rows}) > 1
        }
        if mixed:
            report.add(
                Severity.ERROR,
                "heterogeneous_market",
                f"one source_market_id groups rows that differ in {sorted(mixed)}: "
                + "; ".join(
                    sorted(
                        {
                            f"{row.sport.value} {row.market.value}/{row.period.value}"
                            f"{'/' + row.side.value if row.side else ''}"
                            for row in rows
                        }
                    )
                )
                + " — these are different markets and must not share an id",
                source=source,
                event_key=first.event_key,
            )
            continue

        # Two rows for one selection means the group holds more than one market
        # (typically a primary line fused with an alternate), which would make
        # the completeness and overround checks below read arbitrary rows.
        repeated = sorted(
            {
                selection.value
                for selection in selections
                if sum(1 for row in rows if row.selection is selection) > 1
            }
        )
        if repeated:
            report.add(
                Severity.ERROR,
                "repeated_selection_within_market",
                f"{label} prices {repeated} more than once under one market id, so it "
                f"holds several markets: lines {sorted({row.line for row in rows})}",
                source=source,
                event_key=first.event_key,
            )
            continue

        required = _required_selections(first.sport, first.market, first.period)
        missing = required - selections
        if missing == {Selection.DRAW} and first.period in FULL_CONTEST_WINDOWS:
            # The headline moneyline of a whole contest in a draw-pricing sport is
            # three-way, always: FanDuel sells it as WIN-DRAW-WIN, Kambi as "Full
            # Time", Pinnacle as period 0 (soccer) or period 6 (hockey) with a
            # ``draw`` designation.  Two rows there is a dropped leg, and calling
            # it a two-way market is the expensive reading: the two prices sum
            # below 1.0, so it looks either like free money or like the book
            # pricing itself to lose, when the truth is that a third of the
            # probability is missing from the sum.
            # On an exchange the draw contract exists and nobody has offered on
            # it, which is an empty book rather than a dropped leg — so it is
            # said, not failed.  A sportsbook that prices a market prices all of
            # it, and there a missing draw really is a third of the probability
            # gone from the sum.
            order_driven = source in order_book_sources
            report.add(
                Severity.WARNING if order_driven else Severity.ERROR,
                "draw_leg_missing",
                f"{label} prices only {sorted(s.value for s in selections)}, but a draw is "
                f"a settlement outcome of {first.sport.value}/{first.period.value} and the "
                "books price it — "
                + (
                    "on an order-driven venue that means nobody is currently offering "
                    "the draw, so the market is incomplete rather than mis-parsed"
                    if order_driven
                    else "the draw leg was dropped, so this is not a fair two-way market "
                    "and its probabilities do not sum to a market at all"
                ),
                source=source,
                event_key=first.event_key,
            )
        elif missing:
            report.add(
                Severity.WARNING,
                "incomplete_market",
                f"{label}{line_label} is missing {sorted(s.value for s in missing)}",
                source=source,
                event_key=first.event_key,
            )

        # Line agreement. A spread must mirror; everything else shares one line
        # outright. Both are checked — spreads were previously exempt from the
        # line-set check, so a group holding -1.5/+1.5 and -2.5/+2.5 passed as
        # mirrored.
        if first.market is Market.SPREAD:
            by_selection = {row.selection: row for row in rows}
            home, away = by_selection.get(Selection.HOME), by_selection.get(Selection.AWAY)
            if home is not None and away is not None and home.line is not None and away.line is not None:
                if abs(home.line + away.line) > 1e-9:
                    report.add(
                        Severity.ERROR,
                        "spread_not_mirrored",
                        f"home line {home.line:g} and away line {away.line:g} are not opposites",
                        source=source,
                        event_key=first.event_key,
                    )
        else:
            lines = {row.line for row in rows if row.line is not None}
            if len(lines) > 1:
                report.add(
                    Severity.ERROR,
                    "line_mismatch_within_market",
                    f"{label} groups rows with different lines: {sorted(lines)}",
                    source=source,
                    event_key=first.event_key,
                )

        # Overround: does the book actually hold an edge?  Summed over the
        # *sport's* outcome count, which is why the completeness check above has
        # to run first: two legs of a three-way market sum to less than 1.0 on
        # perfectly good prices, and reporting that as "the book prices itself to
        # lose" blames the prices for a missing row.
        active = [row for row in rows if row.status is QuoteStatus.ACTIVE]
        if not missing and len(active) == len(rows) and len(rows) >= 2:
            overround = sum(row.implied_probability for row in rows)
            order_driven = source in order_book_sources
            if order_driven:
                sub_unity.setdefault(source, []).append(overround)
            if overround < MIN_OVERROUND - OVERROUND_FLOAT_TOLERANCE:
                # On an order-driven venue the boundary is the **net** sum: the
                # two sides are separate books, and "crossed" only means
                # anything if somebody could actually take both at a profit —
                # which is a question about prices *after the venue's own
                # commission*.  A Kalshi market resting at 49¢/49¢ sums to 0.98
                # gross and ~1.015 net of its ~1.75¢-per-side contract fee, so
                # makers legitimately sit there; a flat 0.99 gross floor filed
                # it as "a book does not price itself to lose" — a sportsbook
                # sentence about a venue that is not a book — and failed the
                # run.  The parser trap the flat floor was calibrated against
                # (bid read as ask, which sub-1.0s *most* of a source's
                # markets) is caught after the loop, at the source level, where
                # it actually lives.
                net = (
                    sum(
                        1.0 / net_decimal_odds(source, row.decimal_odds)
                        for row in rows
                    )
                    if order_driven
                    else overround
                )
                benign = order_driven and net >= MIN_OVERROUND - ORDER_BOOK_CROSSING_TOLERANCE

                report.add(
                    Severity.WARNING if benign else Severity.ERROR,
                    "negative_overround",
                    f"{label}{line_label} implied probabilities sum to {overround:.4f} < 1.0 "
                    f"over {len(rows)} outcomes — "
                    + (
                        (
                            "the two sides are separate order books, and after the "
                            f"venue's own commission they sum to {net:.4f}, so nobody "
                            "can take both at a profit: a thin market whose sides "
                            "have not met, not a mispairing"
                            if net >= MIN_OVERROUND
                            else "the two sides are separate order books, crossed by "
                            f"under a cent after the venue's own commission ({net:.4f}) "
                            "— a real, fleeting state two resting orders can produce, "
                            "not a mispairing"
                        )
                        if benign
                        else (
                            "even after the venue's own commission both sides "
                            f"could be taken at a profit (net sum {net:.4f}) — "
                            "a real but extraordinary crossed book, or a "
                            "mispairing; verify before trusting either reading"
                            if order_driven
                            else "a book does not price itself to lose, so the "
                            "prices or lines are mispaired"
                        )
                    ),
                    source=source,
                    event_key=first.event_key,
                )
            elif overround > MAX_OVERROUND:
                report.add(
                    Severity.WARNING,
                    "extreme_overround",
                    f"{label}{line_label} implied probabilities sum to {overround:.4f}",
                    source=source,
                    event_key=first.event_key,
                )

    # The lay-read-as-back parser trap, judged where it lives: at the source, on
    # the **median**.  Reading one side of the book where the other is meant
    # moves every market the same way, so the venue stops holding a spread — and
    # a venue that does not hold a spread is not a venue.  See
    # :data:`MIN_SUB_UNITY_MARKETS` for why this is a median and not a rate, and
    # for what it deliberately cannot catch.
    for source_key, overrounds in sorted(sub_unity.items()):
        if len(overrounds) < MIN_SUB_UNITY_MARKETS:
            continue  # too few markets for a middle to mean anything
        middle = median(overrounds)
        # This gate and the per-market lines above it answer **different
        # questions**, and the message says so rather than appearing to argue
        # with them.  Per market: "can both sides be taken at a profit right
        # now?" — a money question, judged after the venue's commission.  Here:
        # "does this venue hold a spread at all?" — a parser question, judged on
        # the quoted prices, because a commission is not part of whether the
        # parser read the right side of the book.
        #
        # Requiring executable evidence as well was tried and rejected: it kept
        # detection of a 6% misread on every venue but lost 3% on SX Bet,
        # Matchbook and Polymarket, whose own commissions are large enough that
        # a 3% shift never crosses their own book — while still inflating their
        # prices against *other* books, which is where the phantom position
        # would be built.
        if middle < MIN_OVERROUND - OVERROUND_FLOAT_TOLERANCE:
            report.add(
                Severity.ERROR,
                "systematic_sub_unity_pricing",
                f"{source_key}'s typical market prices below fair — the median of "
                f"its {len(overrounds)} complete markets sums to {middle:.4f}, under "
                "1.0 on the quoted prices.  A venue holds a spread; one that does "
                "not is a parser reading one side of the order book where the other "
                "is meant, and every price from this source is suspect.  This is a "
                "question about the parser, not about takeability: individual "
                "markets above may still be reported as fine, because after this "
                "venue's commission nobody could take both of their sides",
                source=source_key,
            )


# ── cross-source ─────────────────────────────────────────────────────────────


#: A source whose main handicap sits on the *opposite side of zero* from the
#: consensus this often is not disagreeing about the number, it is hanging the
#: handicap on the wrong competitor.
#:
#: Measured on the live slate: the worst honest source with a usable sample is
#: Matchbook at 14% of 79 fixtures, and flipping any source's sign puts it at
#: 72–98%.  Sixty per cent sits in the empty space between.
MAX_LINE_SIGN_DISAGREEMENT_RATE = 0.60

#: Below this the disagreement is ordinary: two books either side of a half-point
#: on a near-pick'em fixture.  Reported above it so a partial fault is visible
#: before it becomes a total one.
NOTABLE_LINE_SIGN_DISAGREEMENT_RATE = 0.10

#: ...over at least this many shared fixtures.  Below it the rate is noise —
#: Kalshi shares 15 main handicaps on the live slate and scores 47% honestly.
MIN_LINE_SIGN_FIXTURES = 20


#: How far one source's implied probability may sit from the consensus before it
#: is a different opinion about the *bet* rather than about the price.
#:
#: Books disagree about a price by a point or two; on the live slate the largest
#: deviation any source shows from the median of three or more is **0.083**, and
#: not one of the ten exceeds 0.15 on a single market.  Negating one source's
#: spread lines — which moves every price onto the opposite handicap — puts 17.5%
#: of its markets past it, with a maximum of 0.60.
MAX_PRICE_DEVIATION = 0.15

#: ...and the share of a source's markets that may exceed it.  Two per cent is
#: above the noise (which is zero) and far below a systematic fault.
MAX_PRICE_OUTLIER_RATE = 0.02

#: ...over at least this many outliers.
#:
#: Without a floor the *rate* alone condemned a thin source on one price:
#: Smarkets shares 46 markets on the live slate, so a single wide quote is 2.17%
#: and an ERROR.  On an order-driven venue that is the venue working normally —
#: a lone resting order far from fair — and this pipeline has already taken
#: Smarkets down once for exactly that.
#:
#: A *market-count* floor was the first attempt and was worse than the problem.
#: At 100 it exempted Smarkets permanently — 46 compared markets — from the only
#: check that can see a source whose prices are attached to the wrong side while
#: its participants are correct.  Mirroring Smarkets' prices produced **12
#: positions up to a 24.97% "guaranteed" margin and a byte-identical validation
#: report**.  The outlier floor alone does the whole job: one wide quote is 1 and
#: passes, a mirror is 26 of 46 and does not.
MIN_PRICE_OUTLIERS = 5

#: Below three sources there is no consensus to deviate from — a median of two is
#: their midpoint, and both are equally far from it.
MIN_SOURCES_FOR_PRICE_CONSENSUS = 3


#: Two prices further apart than this could not have been available at the same
#: moment, so they are not the two legs of anything, however real both are.
#:
#: **The detector's own bound, imported rather than restated.**  This check
#: exists to explain a refusal that ``src.arb`` makes silently; a second copy of
#: the number could drift from it, and a report that disagrees with the gate it
#: describes is worse than no report.
#:
#: It bites because a source paced to its own published rate limit can take
#: longer than this to finish its pass: Smarkets makes 155 requests 3.1 s apart
#: and takes 7m40s, against 20 s or less for the other nine.  Most of its
#: selections therefore cannot pair with anything.  Those rows are real and
#: useful for line shopping; they are not arbitrage legs, and the run said
#: nothing to distinguish the two.
MAX_USABLE_OBSERVATION_GAP = MAX_OBSERVATION_SPREAD


def _closest_observation_gap(
    left: Sequence[datetime], right: Sequence[datetime]
) -> timedelta:
    """Smallest ``|a - b|`` for ``a`` in *left*, ``b`` in *right*.

    Both sequences must already be sorted ascending.  Two pointers, not the
    cartesian product: equal-length lists of *n* stamps cost ``O(n)`` here and
    ``O(n²)`` in the nested generator this replaced.
    """
    i = j = 0
    best = abs(left[0] - right[0])
    while i < len(left) and j < len(right):
        gap = abs(left[i] - right[j])
        if gap < best:
            best = gap
        if left[i] <= right[j]:
            i += 1
        else:
            j += 1
    return best


def _check_observation_window(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """Which sources were collected too far apart to be compared with each other.

    Not a fault in any source — it is a property of the pass.  Reported because
    the alternative is a coverage number that looks like comparison surface and
    is not: the detector refuses these pairs, correctly and silently, and an
    operator reading "8 sources on this fixture" has no way to know that two of
    them can never be legs of one position.

    Measured **pairwise**, between the two prices that would actually be the
    legs.  Two earlier forms of this check were both wrong, in opposite
    directions, and each looked right on the data that motivated it:

    Against the run's *consensus*, the limit is silently doubled — two sources
    179 seconds either side of the middle are 358 seconds apart, are refused by
    the detector on every market they share, and both clear a 180-second test
    comfortably.

    Against each source's *median* observation time, the limit is applied to a
    number that describes no actual price.  Nine of the ten sources finish
    inside 20 seconds, so their median stands in for every quote they hold;
    Smarkets takes 7m40s, and its median says nothing about when any particular
    market was read.  On live data that form reported all 996 of Smarkets'
    shared selections as lost when 72 of them were comparable, and raised a
    warning against polymarket, which had lost nothing at all.

    So: actual observation times, compared against the bound the detector
    itself applies.
    """
    groups: dict[tuple, dict[str, list[datetime]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for quote in quotes:
        groups[
            (quote.event_key, quote.market, quote.period, quote.side,
             quote.line, quote.selection)
        ][quote.source].append(quote.observed_at)

    shared: Counter[str] = Counter()
    stranded: Counter[str] = Counter()
    # The partner a stranded source came closest to reaching, which is the one
    # worth naming: it says how far the pass would have to move, and which two
    # sources to move.
    nearest: dict[str, tuple[timedelta, str]] = {}
    for by_source in groups.values():
        if len(by_source) < 2:
            continue
        # Sorted once per source in the group: closest gap between two lists is
        # then a linear two-pointer walk rather than the cartesian product of
        # observation times (which bites when a source posts several rows for
        # one selection).
        sorted_times = {
            source: sorted(times) for source, times in by_source.items()
        }
        for source, times in sorted_times.items():
            shared[source] += 1
            closest = min(
                (_closest_observation_gap(times, other), partner)
                for partner, other in sorted_times.items()
                if partner != source
            )
            if closest[0] <= MAX_USABLE_OBSERVATION_GAP:
                continue  # some partner here is close enough to pair with
            stranded[source] += 1
            held = nearest.get(source)
            if held is None or closest < held:
                nearest[source] = closest

    for source in sorted(stranded):
        gap, partner = nearest[source]
        count = stranded[source]
        report.add(
            Severity.WARNING,
            "collected_outside_the_comparable_window",
            f"{count} of {source}'s {shared[source]} shared market(s) have no partner "
            f"priced close enough in time to be taken together — the nearest, "
            f"{partner}, is {describe_gap(gap)} away at its closest, further than two "
            f"prices may be apart, so those markets cannot be compared however many "
            f"sources the coverage grid shows on them",
            source=source,
        )


def describe_gap(gap: timedelta) -> str:
    seconds = int(gap.total_seconds())
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{seconds // 60} minutes"


def _check_price_agreement(
    quotes: Sequence[Quote],
    report: ValidationReport,
    order_book_sources: frozenset[str] = frozenset(),
) -> None:
    """Does each source price the same bet roughly as the others do?

    The general net beneath the specific checks.  ``_check_line_orientation``
    names a handicap hung on the wrong competitor, but it can only judge a source
    that publishes **one** primary line per fixture — and four of the ten publish
    a symmetric ladder instead.  For those, negating every line maps the set of
    lines onto itself while moving each *price* onto the opposite handicap, which
    is invisible to a check that compares only the numbers.  Flipping Matchbook's
    spreads that way added **58 reported positions**, the largest at ``margin
    24.90%, guaranteed +32.81 on 100``, and produced a byte-identical validation
    report.

    Comparing prices catches it, and is not specific to that fault: a selection
    mapped to the wrong participant, a market that is not the market it claims to
    be, a stale or unit-wrong price all show up the same way — as one source
    disagreeing with everybody about what a bet is worth.

    Against the **median** of three or more, because two sources have no
    consensus between them: their median is the midpoint and each is equally far
    from it, so one flipped book would indict the honest one just as hard.
    """
    # Which sources price a draw on each moneyline, so a two-way contract is
    # never compared against a three-way one *whose draw row parsed*.
    #
    # They are different bets and their prices are not commensurable: on the
    # 2026-08-08 Pennsylvania board, Caesars posted a **two-way** first-inning
    # moneyline (home -210, away +170) while BetRivers, betPARX and theScore
    # posted a **three-way** one (home +210, away +400, draw -129).  Both are
    # coherent books — 4.7% and 8.6% vig respectively — but the two-way home
    # price implies 0.677 and the three-way home price implies 0.323, because
    # the three-way one loses when the inning is scoreless and the two-way one
    # does not.  Compared as one market that is a 0.35 deviation, and this check
    # reported all 30 first-inning moneylines as Caesars pricing a bet wrongly.
    #
    # :mod:`src.arb` already draws this distinction — ``contract_shape`` keeps
    # the shapes in separate groups and ``ambiguous_tie_settlement`` refuses the
    # two-way one outright in a draw-pricing window — so no position could be
    # built from the pair.  The defect was confined to this report, and that is
    # bad in its own way: an ERROR that fires every run on a known-benign cause
    # is how a report stops being read.
    #
    # ``side`` rides along to keep the tuple congruent with the grouping key
    # below, and today it is constant: the schema forbids ``side`` on anything
    # but TEAM_TOTAL, so on a moneyline it is always ``None``.  It starts
    # mattering only if a sided market ever prices a draw.
    #
    # The ``is MONEYLINE`` guard below states the intent and cannot currently
    # change an answer: ``prices_draw`` is built from ``DRAW`` rows, which are
    # moneyline rows, so the market is already in the tuple and a spread never
    # matches.  Removing it is a no-op today and a defect the moment anything
    # else carries a ``DRAW``.
    # Judged from *all* rows including suspended ones, same as
    # ``arb.contract_shape``: suspending a price does not change which outcomes
    # the market settles on.  Requiring ACTIVE here made a three-way book whose
    # draw was momentarily suspended grade as two-way, which pooled its
    # home/away prices with the genuine two-way book and indicted *that* book —
    # the exact false ERROR this key exists to remove, resurrected through the
    # status filter.  A draw row that never parses at all still misgrades this
    # way; validation cannot see rows that do not exist, so that residue is a
    # stated limit, not a keying bug.
    prices_draw: set[tuple[str, str, Market, Period, Any]] = set()
    for quote in quotes:
        if quote.selection is Selection.DRAW:
            prices_draw.add(
                (quote.source, quote.event_key, quote.market, quote.period, quote.side)
            )

    groups: dict[tuple, dict[str, float]] = defaultdict(dict)
    for quote in quotes:
        if quote.status is not QuoteStatus.ACTIVE:
            continue
        shape = quote.market is Market.MONEYLINE and (
            quote.source, quote.event_key, quote.market, quote.period, quote.side
        ) in prices_draw
        key = (
            quote.event_key, quote.market, quote.period, quote.side,
            quote.line, quote.selection, shape,
        )
        # Best price per source, not first: ``is_alternate`` is not in the key,
        # so a source with a main and an extra row at one number would otherwise
        # contribute whichever arrived first.  ``src.arb`` takes the better one.
        held = groups[key].get(quote.source)
        if held is None or quote.implied_probability < held:
            groups[key][quote.source] = quote.implied_probability

    outliers: dict[str, list[tuple[str, float]]] = defaultdict(list)
    compared: Counter[str] = Counter()
    # ...and the same two counts per sport.  A source mirrored in **one** sport
    # is diluted below the bar by the sports it gets right: BetRivers mirrored in
    # basketball alone is 17% of its basketball markets and 1.99% overall, just
    # under a 2% threshold, for 58 phantom positions and no finding.  Orientation
    # already grades this way; this check is the one that catches a price mirror
    # at all, so it needs it more.
    by_sport: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    by_sport_compared: Counter[tuple[str, str]] = Counter()
    sport_of: dict[tuple, str] = {}
    for quote in quotes:
        # Same key as ``groups``, shape included.  A six-element key here would
        # miss every lookup and silently grade every outlier under sport ``""``,
        # which disables the per-sport rate entirely — the half of this check
        # that catches a source mirrored in one sport only.
        shape = quote.market is Market.MONEYLINE and (
            quote.source, quote.event_key, quote.market, quote.period, quote.side
        ) in prices_draw
        sport_of.setdefault(
            (quote.event_key, quote.market, quote.period, quote.side,
             quote.line, quote.selection, shape),
            quote.sport.value,
        )
    # Per (source, market) rather than per selection, so an order-driven venue
    # can be asked whether it disagrees about the *market* or about one side of
    # it.  See below.
    sides_outlying: Counter[tuple[str, tuple]] = Counter()
    pending: list[tuple[str, tuple, str, str, float]] = []

    for key, per_source in groups.items():
        if len(per_source) < MIN_SOURCES_FOR_PRICE_CONSENSUS:
            continue
        consensus = median(per_source.values())
        sport = sport_of.get(key, "")
        # Everything but the selection, with the line stated from **one side's**
        # perspective so a contract's two halves group and two contracts do not.
        #
        # A spread states its halves at opposite numbers — measured, 960 of 960
        # two-sided groups in the captured corpus are mirrored — so keyed on the
        # raw line, home -1.5 and away +1.5 never shared a key.  Every
        # (order-driven source, spread) pair then had exactly one side *by
        # construction*, the one-outlying-side exemption applied unconditionally,
        # and a swapped spread at an exchange was silent while the detector
        # reported 10 positions at 7.5%.
        #
        # Taking the magnitude fixed that and broke the other half: ``home -1.5 /
        # away +1.5`` and ``home +1.5 / away -1.5`` are **different contracts**,
        # and 133 fixtures in the corpus have a source posting both.  Collapsing
        # them gave a venue thin on one side of each a second outlying side and
        # an ERROR it had not earned — the exemption's own case, refused.
        #
        # Negating the away side states both halves from the home side and keeps
        # the two contracts apart.  Totals are untouched: OVER and UNDER already
        # share one number, and neither is ``AWAY``.
        event_key, market, period, side, line, selection, shape = key
        canonical = (
            -line if line is not None and selection is Selection.AWAY else line
        )
        market_key = (event_key, market, period, side, canonical, shape)
        for source, implied in per_source.items():
            compared[source] += 1
            by_sport_compared[(source, sport)] += 1
            gap = abs(implied - consensus)
            if gap > MAX_PRICE_DEVIATION:
                sides_outlying[(source, market_key)] += 1
                pending.append((source, market_key, key[0], sport, gap))

    for source, market_key, event_key, sport, gap in pending:
        # On an order book, one outlying side is not evidence about the parser.
        #
        # A sportsbook posts both sides of a market it is making, so a price
        # nothing like the consensus there means the row is not the bet it
        # claims to be — which is what this check says in its own message. An
        # exchange posts whatever somebody left resting, with nobody
        # market-making to hold it near fair value, so its best *takeable* price
        # on a side nobody wants can be anything at all.  Live: Matchbook's
        # Mannarino/Michelsen moneyline, home 1.400 against a 1.408/1.417/1.423
        # consensus and away 1.05 against ~2.9 — with 1660.56 resting behind the
        # home price and 6.64 behind the away one.  That is a thin book, and it
        # failed the run.
        #
        # The discriminator is already in the data and needs no new threshold:
        # every fault this check names — a handicap on the wrong competitor, a
        # selection mapped to the wrong side, a market filed as another market —
        # moves **at least two** sides together, because it is a permutation.  A
        # single outlying side beside sides that agree to three decimal places
        # is the one thing none of them can produce.
        #
        # *Exactly one*, not "fewer than all".  A swap on a **three-way** market
        # moves home and away and leaves the draw invariant, so "fewer than all"
        # read 2 of 3 as a thin book and exempted the swap outright: on a
        # constructed EPL slate with matchbook's home and away exchanged, the
        # run reported **10 positions at margin 13.57%, guaranteed +14.27 on
        # 100** and the validation report held no price-agreement finding at
        # all.  ``margin_implausibly_large`` does not catch it either — the
        # whole band a 0.15 deviation produces sits under ``REFUSE_MARGIN``.
        #
        # It also makes the rule apply where its rationale is strongest and
        # "fewer than all" could not reach: a venue quoting **one** side of a
        # market others price fully is the thinnest book there is, and 122 of
        # the 447 order-driven (source, market) pairs on the captured slate are
        # exactly that.
        if source in order_book_sources and sides_outlying[(source, market_key)] == 1:
            continue
        outliers[source].append((event_key, gap))
        by_sport[(source, sport)].append((event_key, gap))

    for source, entries in sorted(outliers.items()):
        total = compared[source]
        rate = len(entries) / total
        worst_rate = max(
            (
                len(rows) / max(by_sport_compared[(key, sport)], 1)
                for (key, sport), rows in by_sport.items()
                if key == source and len(rows) >= MIN_PRICE_OUTLIERS
            ),
            default=0.0,
        )
        if (
            max(rate, worst_rate) <= MAX_PRICE_OUTLIER_RATE
            or len(entries) < MIN_PRICE_OUTLIERS
        ):
            continue
        worst = max(gap for _, gap in entries)
        examples = ", ".join(sorted({event for event, _ in entries})[:_EXAMPLES])
        report.add(
            Severity.ERROR,
            "prices_disagree_with_every_other_source",
            f"{source} prices {len(entries)} of {total} shared market(s) more than "
            f"{MAX_PRICE_DEVIATION:.2f} of implied probability away from what the other "
            f"sources say (worst {worst:.2f}; e.g. {examples}) — books differ by a point "
            "or two, not by this, so these rows are not the bet they claim to be: a "
            "handicap on the wrong competitor, a selection mapped to the wrong side, or "
            "a market that is not the one it is filed under",
            source=source,
        )


def _check_line_orientation(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """Do the books agree about *which competitor* the handicap favours?

    Nothing checked this.  ``_check_group`` verifies that a spread mirrors within
    one source's own market — which a sign flip satisfies perfectly, since both
    of its sides flip together — and the cross-source checks compare sport,
    participants, orientation, league and start time, never the line.

    So an adapter attaching the handicap to the wrong side produced **no finding
    at all**.  On the live slate, flipping one source's spread signs adds up to
    185 positions that exist only because of the flip, median margin 17.9%, the
    largest printed as ``margin 24.86%, guaranteed +33.02 on 100``.  Most carry
    the detector's implausible-margin note, but not all of them do, and a note is
    not a finding.

    Compared on the **main** line only and against the median rather than
    pairwise: books genuinely differ by half a point or so, and a book that is
    merely half a point off must not be indicted for it.  What no honest source
    does is put the favourite on the other side, over and over.
    """
    # Only sources that publish **one** primary handicap per scoring window
    # are judged.
    #
    # A source that publishes a symmetric ladder has no side to be wrong about:
    # Kalshi lists "PIT wins by over 1.5/2.5/3.5" *and* "AZ wins by over
    # 1.5/2.5/3.5", which is home -1.5,-2.5,-3.5 and home +1.5,+2.5,+3.5, and
    # flipping every sign maps that set onto itself.  There is nothing there to
    # check, and including it by picking one row arbitrarily does not add
    # information — it adds noise, and it dilutes the sources where the check
    # does work.
    #
    # The window is (event, WINDOW CLASS), where the class merges the
    # full-contest periods and keeps every other period its own.  Three
    # defect generations decided this shape:
    # * grouped by event alone, a source posting a full-game spread beside a
    #   first-5-innings or regulation one — betrivers_kambi's ordinary slate
    #   — held two lines per group, tripped the one-line ladder exemption,
    #   and was never judged (0 of 15 baseball, 0 of 7 hockey cells on run
    #   32), while honest single-window sources were charged cross-window
    #   disagreements they never made;
    # * grouped by raw period, the secondary windows that fix created hold
    #   exactly TWO judgeable sources in production, and a median of two is
    #   a midpoint: a symmetric flip cancels to 0 and reads as a pick'em
    #   (blind), and an asymmetric flip hands the midpoint the flipped sign
    #   and CONVICTS THE HONEST SOURCE — a false systematic ERROR;
    # * FULL_GAME and REGULATION share a favourite (FULL_CONTEST_WINDOWS
    #   codifies it), so splitting them starved hockey's window of the
    #   full-game majority that could have judged it.
    # Within the merged full-contest class a source may post both spellings;
    # the full-game line speaks for it rather than disqualifying it as a
    # ladder.  And a window is judged only with THREE OR MORE voters: two
    # have no majority, only a midpoint, and both of its failure shapes
    # above are worse than saying nothing.
    #
    # Honest coverage, measured 2026-08-10 so the record cannot overclaim:
    # run 32's baseball cells grade 15/15 per source (the original 0/15
    # darkness is gone), but its hockey stays ungraded (two networks — the
    # majority floor refuses), a wholesale flip on a sub-20-fixture slate
    # caps at WARNING (MIN_LINE_SIGN_FIXTURES), and single-book runs have
    # no orientation coverage at all.  Silence over false conviction is the
    # deliberate trade throughout; the price-agreement check remains the
    # net under every one of these refusals and independently names flipped
    # sources.
    _FULL = "full"
    laddered: dict[tuple[str, str], dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for quote in quotes:
        if (
            quote.market is not Market.SPREAD
            or quote.is_alternate
            or quote.selection is not Selection.HOME
            or quote.line is None
            or quote.status is not QuoteStatus.ACTIVE
        ):
            continue
        window = _FULL if quote.period in FULL_CONTEST_WINDOWS else quote.period.value
        laddered[quote.event_key, window][quote.source][quote.period.value].append(
            quote.line
        )
    main: dict[tuple[str, str], dict[str, float]] = {}
    for window_key, per_source in laddered.items():
        chosen: dict[str, float] = {}
        for source, per_period in per_source.items():
            if any(len(lines) > 1 for lines in per_period.values()):
                continue  # a symmetric ladder — nothing to judge
            if Period.FULL_GAME.value in per_period:
                chosen[source] = per_period[Period.FULL_GAME.value][0]
            else:
                chosen[source] = next(iter(per_period.values()))[0]
        main[window_key] = chosen

    # One VOTE per adapter network, not per registered tenant.  This check
    # hunts PARSER bugs — a handicap attached to the wrong side by the code
    # that reads the feed — and ``betrivers_kambi`` / ``leovegas_kambi`` are
    # one parser registered twice (both descriptors construct
    # ``BetRiversKambiAdapter``), as are the Action Network and VegasInsider
    # tenant families.  Counted per tenant, a sign flip in the shared Kambi
    # adapter flipped two of three "voters" at once: measured on run 25, the
    # pair is a strict majority in 29 of pinnacle's 31 three-source soccer
    # windows, and simulating the flip convicted HONEST pinnacle with the
    # systematic ERROR at 69% while both flipped tenants walked with
    # sub-threshold warnings — the one check whose message names a culprit
    # named the wrong one.  A network's vote is the median of its tenants'
    # lines; every tenant is still judged and named individually against
    # the consensus.  Sources outside the registry (every synthetic test
    # book) are their own network.
    from src.sources.registry import BY_BASE_KEY

    def _network(source: str) -> str:
        descriptor = BY_BASE_KEY.get(source)
        return descriptor.adapter.__name__ if descriptor is not None else source

    # Rates are judged per (source, window class): pooled across windows, a
    # wholesale flip of one window on a two-window book capped at 50% and
    # could never reach the systematic threshold, and the message counted
    # windows as "fixtures".
    disagreements: dict[tuple[str, str], list[str]] = defaultdict(list)
    shared: Counter[tuple[str, str]] = Counter()
    for (event_key, window), per_source in main.items():
        networks: dict[str, list[float]] = defaultdict(list)
        for source, line in per_source.items():
            networks[_network(source)].append(line)
        if len(networks) < 3:
            continue
        consensus = median(median(lines) for lines in networks.values())
        if consensus == 0:
            # A pick'em says nothing about which side is favoured.
            continue
        for source, line in per_source.items():
            shared[source, window] += 1
            if line != 0 and (line > 0) != (consensus > 0):
                disagreements[source, window].append(
                    event_key if window == _FULL else f"{event_key} ({window})"
                )

    for (source, window), events in sorted(disagreements.items()):
        total = shared[source, window]
        rate = len(events) / total
        if rate < NOTABLE_LINE_SIGN_DISAGREEMENT_RATE:
            # Books really do land either side of a pick'em, and saying so on
            # every run would drown the case this check exists for.
            continue
        systematic = (
            total >= MIN_LINE_SIGN_FIXTURES
            and rate > MAX_LINE_SIGN_DISAGREEMENT_RATE
        )
        window_label = (
            "" if window == _FULL else f" (the {window} window)"
        )
        report.add(
            Severity.ERROR if systematic else Severity.WARNING,
            "line_favours_the_other_competitor",
            f"{source}'s main handicap{window_label} favours the opposite competitor "
            f"from the other sources on {len(events)} of {total} shared fixture(s) "
            f"({rate * 100:.0f}%; e.g. {', '.join(sorted(events)[:_EXAMPLES])})"
            + (
                " — that is the whole slate, not a difference of opinion about the "
                "number: the handicap is attached to the wrong side, and every "
                "cross-book position built on it is backing the same competitor twice"
                if systematic
                else " — within what books genuinely differ by, but worth a look"
            ),
            source=source,
        )


def _check_orientation(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """Which participant each source calls home, compared across sources.

    Grouped on the **unordered** participant pair, not on ``event_key``, and that
    is the whole point.  The key *is* ``away@home:date`` and reconciliation
    rebuilds it from the participants each source reported, so a source with home
    and away the wrong way round does not land in a group and disagree — it lands
    in a group of its own, with nobody to disagree with.  The check ran on every
    run and could never fire.

    Measured on the live slate with one book's team-sport rows reversed: 103
    ``home_away_disagreement`` errors before reconciliation, **zero** after it,
    the run passing, and that book dropping from 133 shared fixtures to 30 — the
    only trace being 110 warnings describing a completely reversed matchup as "a
    per-source doubleheader ordinal" correction.

    Kept separate from the other cross-source checks rather than replacing their
    grouping, because the two need opposite things: this one must put both
    orientations of a fixture together, while ``participant_pair_disagreement``
    must put two *different* matchups that share a key together.  One grouping
    cannot do both.

    Only where "home" names a real property of the fixture.  In tennis there is
    no home player: each book orders the two names however it likes and
    :func:`src.events.orient` imposes its own ordering, so comparing the books'
    orientations would report a disagreement that does not exist.
    """
    by_pair: dict[tuple[frozenset[str], str], dict[str, Quote]] = defaultdict(dict)
    for quote in quotes:
        competition = _competition(quote)
        if competition is None or not competition.has_home_away:
            continue
        _, _, tail = quote.event_key.partition(":")
        identity = (frozenset({quote.home_participant, quote.away_participant}), tail)
        by_pair[identity].setdefault(quote.source, quote)

    outliers: dict[str, list[tuple[str, tuple, tuple | None]]] = defaultdict(list)
    shared: Counter[str] = Counter()
    # ...and the same two counts split by sport.  Each adapter reads home and
    # away by a different per-sport convention, so the realistic bug is a source
    # reversed in *one* sport — and that is exactly what an overall rate hides:
    # reversing one book's baseball rows alone scores 9% against a 60% bar,
    # while being 100% wrong on every baseball fixture it shares.
    by_sport_out: dict[tuple[str, str], list[tuple[str, tuple, tuple | None]]] = defaultdict(list)
    by_sport_shared: Counter[tuple[str, str]] = Counter()
    for (_, _), rows in by_pair.items():
        if len(rows) < 2:
            continue
        shared.update(rows.keys())
        sport = next(iter(rows.values())).sport.value
        by_sport_shared.update((source, sport) for source in rows)
        oriented = {
            source: (row.home_participant, row.away_participant)
            for source, row in rows.items()
        }
        if len(set(oriented.values())) < 2:
            continue
        # Aggregated by source rather than reported per fixture, because the two
        # things that produce this differ in scale and not in kind.  One fixture
        # oriented two ways is a neutral-venue judgement — the live slate has
        # exactly one, a pre-season friendly where FanDuel writes "Fulham @ Al
        # Ahli" and Pinnacle writes the reverse, and both price Fulham the
        # favourite at 1.67/1.70, so neither is wrong.  A source reversed on
        # *many* fixtures is an adapter reading a venue's own side labels as this
        # pipeline's orientation, which is the defect that put two legs of a
        # "guaranteed" position on the same competitor.
        example = min(row.event_key for row in rows.values())
        # With no majority — most often two sources, one each way — nobody is the
        # outlier and the disagreement is recorded against both.  ``_outliers``
        # cannot express that: ``_modal`` breaks the tie on the value, so at two
        # sources "consensus" is whichever pair of participant keys sorts first
        # and the *other* book is indicted.  Blame decided by team name is worse
        # than no blame: with one book genuinely reversed it produced an
        # ERROR-graded accusation against the correct one on a fifth of its
        # fixtures.
        counts = Counter(oriented.values())
        top = max(counts.values())
        undecided = sum(1 for value in counts.values() if value == top) > 1
        if undecided:
            for source, value in oriented.items():
                outliers[source].append((example, value, None))
                by_sport_out[(source, sport)].append((example, value, None))
        else:
            consensus_side, side_odd = _outliers(oriented)
            for source, value in side_odd.items():
                outliers[source].append((example, value, consensus_side))
                by_sport_out[(source, sport)].append((example, value, consensus_side))

    _report_orientation_outliers(
        outliers, shared, report, by_sport=by_sport_out, by_sport_shared=by_sport_shared
    )


#: How near two kickoffs must be before two event keys sharing one participant
#: are read as one fixture split by a spelling.  Wide enough to absorb the
#: minutes by which books round a start time, narrow enough that one club's two
#: real fixtures in a day — or one tennis player's two rounds — never qualify:
#: no competitor genuinely starts two events half an hour apart.
SPLIT_FIXTURE_KICKOFF_TOLERANCE = timedelta(minutes=30)

#: Splits that are the cost of a refused merge, not a defect.  FanDuel writes
#: the Spanish club as bare "Deportivo", and ``_SOCCER_ALIASES``' own comment
#: records that on a live capture "Deportivo" is Deportivo Pasto — so an alias
#: claiming it is La Coruna is the false merge that table exists to avoid.  One
#: unjoined source is recoverable; two clubs' prices on one fixture is not.
#: The committed-capture sweep in ``tests/test_adversarial_findings.py`` reads
#: this same set, so the decision is recorded once.
DELIBERATE_SPLITS: frozenset[frozenset[str]] = frozenset(
    {frozenset({"SOCCER-deportivo", "SOCCER-deportivolacoruna"})}
)


def _check_split_fixtures(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """One real fixture under two event keys, because a spelling did not join.

    Every other cross-source check groups by ``event_key`` or by the
    participant-key set, and a split differs in both — so a book whose spelling
    of one side lands on its own key simply never meets the others: no price
    comparison, no orientation vote, no arbitrage leg, and nothing anywhere to
    say so.  The class was found by a test over committed captures, which
    cannot see a live slate.

    The shape that only a split produces: two event keys that share exactly one
    participant and start together.  Two *real* fixtures can share a competitor
    on one date — a tennis player's two rounds, a doubleheader — but not the
    same half hour, and a doubleheader shares both sides rather than one.

    Reported per ``(source, sport)`` rather than per fixture, and that is a
    tuning decision made against real volume: re-validating the 2026-08-14
    Illinois run, the per-pair form produced **566** warnings — smarkets'
    short-form club names alone split it from hundreds of minor-soccer
    fixtures — which is the ``incomplete_market`` failure mode, a finding the
    operator learns to scroll past.  What the operator actually acts on is
    *which adapter needs participant rules*, so each source that sits alone on
    its own key while other books share the kickoff gets one warning with its
    count and worked examples.  Graded WARNING because the run's data is
    right, merely unjoined.
    """
    times: dict[str, set[datetime]] = defaultdict(set)
    sources_of: dict[str, set[str]] = defaultdict(set)
    sport_of_key: dict[str, str] = {}
    participants_of: dict[str, frozenset[str]] = {}
    spelling: dict[tuple[str, str], str] = {}
    sides: dict[str, set[str]] = defaultdict(set)
    for quote in quotes:
        key = quote.event_key
        times[key].add(quote.commence_time)
        sources_of[key].add(quote.source)
        sport_of_key.setdefault(key, quote.sport.value)
        participants_of.setdefault(
            key, frozenset({quote.away_participant, quote.home_participant})
        )
        spelling.setdefault((key, quote.away_participant), quote.away_team)
        spelling.setdefault((key, quote.home_participant), quote.home_team)
        sides[quote.away_participant].add(key)
        sides[quote.home_participant].add(key)

    # Union split keys into components, so a three-spelling chain is one
    # fixture rather than three pairwise reports.
    tolerance = SPLIT_FIXTURE_KICKOFF_TOLERANCE.total_seconds()
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        root = key
        while parent.get(root, root) != root:
            root = parent[root]
        parent[key] = root
        return root

    for keys in sides.values():
        if len(keys) < 2:
            continue
        for first, second in combinations(sorted(keys), 2):
            differing = participants_of[first] ^ participants_of[second]
            # Zero differing sides is a doubleheader's two halves; two whole
            # matchups differing is just the slate.  Exactly one side apart
            # is the near-identity only a spelling produces.
            if len(differing) != 2 or frozenset(differing) in DELIBERATE_SPLITS:
                continue
            gap = min(
                abs((a - b).total_seconds())
                for a in times[first]
                for b in times[second]
            )
            if gap > tolerance:
                continue
            parent[find(first)] = find(second)

    components: dict[str, set[str]] = defaultdict(set)
    for key in parent:
        components[find(key)].add(key)

    # Within a component the key most books agree on is the reference; every
    # other key's sources are the ones sitting out of the comparison, and the
    # example names the spelling pair a participant rule would join.
    unjoined: dict[tuple[str, str], list[str]] = defaultdict(list)
    for keys in components.values():
        if len(keys) < 2:
            continue
        reference = max(sorted(keys), key=lambda key: len(sources_of[key]))
        for key in sorted(keys):
            if key == reference:
                continue
            differing = participants_of[key] ^ participants_of[reference]
            if len(differing) != 2:
                # A chain can union two keys that differ from each other on
                # both sides; the reference still shares one side with each.
                continue
            odd = next(iter(differing & participants_of[key]))
            ref_odd = next(iter(differing & participants_of[reference]))
            example = (
                f"its {spelling.get((key, odd))!r} ({odd}) against "
                f"{spelling.get((reference, ref_odd))!r} ({ref_odd}) at "
                + ", ".join(sorted(sources_of[reference]))
                + f" on {key}"
            )
            for source in sources_of[key]:
                unjoined[(source, sport_of_key[key])].append(example)

    for (source, sport), examples in sorted(unjoined.items()):
        report.add(
            Severity.WARNING,
            "split_fixture_suspected",
            f"{len(examples)} {sport} fixture(s) sit on this source's own event "
            f"key while other books price the same kickoff under another "
            f"spelling (e.g. {'; '.join(examples[:_EXAMPLES])}) — a split "
            "fixture joins no cross-source comparison at all, so each spelling "
            "pair needs a participant rule before this book can meet the others",
            source=source,
        )


def _check_cross_source(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """The whole point of one schema is that sources agree on identity."""
    by_event: dict[str, dict[str, list[Quote]]] = defaultdict(lambda: defaultdict(list))
    for quote in quotes:
        by_event[quote.event_key][quote.source].append(quote)

    league_outliers: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    time_outliers: dict[str, list[tuple[str, timedelta, timedelta]]] = defaultdict(list)


    for event_key, by_source in by_event.items():
        if len(by_source) < 2:
            continue
        rows = {source: source_rows[0] for source, source_rows in by_source.items()}

        sports = {row.sport for row in rows.values()}
        if len(sports) > 1:
            report.add(
                Severity.ERROR,
                "sport_disagreement",
                "sources disagree on which sport this event is: "
                + "; ".join(f"{s}: {row.sport.value}" for s, row in sorted(rows.items())),
                event_key=event_key,
            )
            continue

        # The two books must be talking about the same two competitors.  This is
        # checked as an unordered *pair* first, and separately as an ordering,
        # because the two failures are not the same thing and only one of them
        # exists in every sport.
        pairs = {
            source: frozenset({row.home_participant, row.away_participant})
            for source, row in rows.items()
        }
        if len(set(pairs.values())) > 1:
            report.add(
                Severity.ERROR,
                "participant_pair_disagreement",
                "sources under one event key describe different matchups: "
                + "; ".join(
                    f"{s}: {row.away_participant} v {row.home_participant} "
                    f"({row.away_team} v {row.home_team})"
                    for s, row in sorted(rows.items())
                ),
                event_key=event_key,
            )
            continue

        competitions = [_competition(row) for row in rows.values()]
        known = [c for c in competitions if c is not None]
        if not known:
            continue


        # Books disagree about league classification constantly — the same tennis
        # match is "ATP Challenger Bonn - R1" at one book and "challenger" at
        # another — and league is deliberately not part of event identity, so this
        # is a warning about reporting, never an error about the join.
        #
        # Recorded per outlier and reported once at the end rather than once per
        # event.  Phrased as "any two differ", this fired on essentially every
        # shared event as soon as there were more than a handful of sources, and
        # findings are stored one row per occurrence with the dashboard showing
        # the first 500 — so the check drowned the errors it sits beside.
        consensus_league, league_odd = _outliers(
            {source: row.league for source, row in rows.items()}
        )
        for source, value in league_odd.items():
            league_outliers[(source, value, consensus_league)].append(event_key)

        # Start-time agreement, at the league's own tolerance.  20 minutes was
        # right for baseball and wrong for tennis, where each book publishes its
        # own "not before" estimate and the same match is routinely listed hours
        # apart.  The widest league present is used, matching what
        # src.events.reconcile_event_keys does when books classify one fixture
        # into two leagues.
        #
        # The *reporting* threshold, not the clustering tolerance.  Clustering is
        # deliberately generous so a fixture two books time differently still
        # joins; reporting is strict so the disagreement is still visible once it
        # has.  Using the clustering width here would mean the wider a sport's
        # window got, the less it could ever notice — and soccer's window is 30
        # hours precisely because books list a kickoff a day apart.
        #
        # Measured against the **median** start rather than against the widest
        # pair.  max-minus-min grows with the number of sources by construction,
        # so one book three hours out used to indict the whole event; the median
        # names the book that is actually wrong and leaves the others alone.
        tolerance = max(c.time_disagreement_threshold for c in known)
        starts = {source: row.commence_time for source, row in rows.items()}
        if len(starts) == 2:
            # Two sources have no consensus between them: their median is the
            # midpoint, so each sits at half the gap and the tolerance is
            # silently doubled.  ``_check_price_agreement`` documents exactly
            # this and guards on three sources for it; this check guarded on
            # two, so it could not see any gap under 2x its own threshold.
            #
            # 40% of shared events (44 of 111 on the captured slate) have
            # exactly two sources, and the one event whose span exceeds its
            # threshold — a 6h30m tennis disagreement between FanDuel and
            # Matchbook — is also the top-ranked position the detector reports.
            # The check that exists to say "one of these books may be describing
            # a different fixture" was silent on the fixture the money was on.
            #
            # Both sources are named, because with two there is no way to say
            # which one is wrong — and that is the finding.
            (first, when_first), (second, when_second) = starts.items()
            span = abs(when_first - when_second)
            if span > tolerance:
                for source in (first, second):
                    time_outliers[source].append((event_key, span, tolerance))
        elif len(starts) >= 3:
            # Three or more: measured against the **median** rather than the
            # widest pair, so one book three hours out is named and the others
            # are left alone.
            consensus_start = _median_time(list(starts.values()))
            for source, moment in starts.items():
                delta = abs(moment - consensus_start)
                if delta > tolerance:
                    time_outliers[source].append((event_key, delta, tolerance))

    _report_league_outliers(league_outliers, report)
    _report_time_outliers(time_outliers, report)
    _check_orientation(quotes, report)


def _median_time(moments: Sequence[datetime]) -> datetime:
    """The middle start time, as the consensus about when a fixture begins.

    Taken on offsets from the earliest moment because ``datetime`` has no
    arithmetic mean; the median is what is wanted anyway, since one book three
    hours out must not drag the consensus toward itself the way a mean would.
    """
    base = min(moments)
    return base + timedelta(seconds=median((m - base).total_seconds() for m in moments))


#: How many example fixtures an aggregated cross-source finding names.  Enough to
#: go and look at, few enough that the message stays readable.
_EXAMPLES = 3


#: A source disagreeing about home and away on more than this share of the
#: fixtures it shares is not making a judgement call about a neutral venue; it is
#: reading its own side labels as this pipeline's orientation.
#:
#: Set high on purpose.  Where a fixture has only two sources there is no
#: majority, so the disagreement is recorded against **both** — the alternative,
#: picking a "consensus" by sorting the participant keys, decides blame by team
#: name and indicts the correct book about half the time.  Recording both means
#: an innocent source accumulates marks in proportion to how many two-source
#: fixtures it shares with the broken one: reversing Matchbook on the live slate
#: puts Matchbook at 88 of 88 and drags Pinnacle to 48 of 175.
#:
#: Only the reversed source approaches *every* shared fixture, so a rate this
#: high separates the fault from its collateral: 100% against 27% and 55% in the
#: two live reversals, and 0.6% for the worst honest source.
MAX_ORIENTATION_DISAGREEMENT_RATE = 0.60

#: ...and below this many fixtures no rate is meaningful, so it stays a warning
#: however it divides.
MIN_ORIENTATION_DISAGREEMENTS = 3

#: A *sport* is only graded on its own once this many fixtures are shared in it.
#: Per-sport denominators are thin — five shared hockey fixtures for one source
#: on the live slate — and because a two-source fixture with no majority is
#: charged to both, four legitimate neutral-venue disagreements out of five would
#: otherwise read as 80% and fail the run for both books.
#:
#: Ten, not twenty.  At twenty the escalation was inert exactly where it was
#: needed: 28 of the 36 (source, sport) pairs on a live slate fell below it, so
#: reversing one source's baseball — the case the per-sport grade was added for,
#: 16 of 16 fixtures wrong — still came out a warning.  Ten admits baseball (26
#: shared) and football (17) and still excludes hockey and basketball (7 each),
#: which are the thin ones a neutral venue could carry.
#:
#: The residual is real and is a limit of the data rather than of the rule: a
#: source reversed *only* in a sport it shares seven fixtures in stays a warning,
#: because seven-of-seven reversed and seven neutral venues are the same
#: observation.  A source reversed in more than one sport, or in a sport with a
#: real slate, is caught either per sport or on the overall rate.
MIN_ORIENTATION_SPORT_FIXTURES = 10


def _report_orientation_outliers(
    outliers: Mapping[str, Sequence[tuple[str, tuple, tuple | None]]],
    shared: Mapping[str, int],
    report: ValidationReport,
    *,
    by_sport: Mapping[tuple[str, str], Sequence[tuple[str, tuple, tuple | None]]],
    by_sport_shared: Mapping[tuple[str, str], int],
) -> None:
    """Who has home and away the wrong way round, and how badly.

    Graded on the rate rather than on the occurrence, because both readings are
    real.  Two books can legitimately disagree about which side is "home" for a
    match on neutral ground, and the pipeline is safe when they do: the two
    orientations produce different event keys, so the rows do not join and no
    position is built across them.  What is lost is the comparison, which is
    worth a warning and not a failed run.

    A source that is reversed *systematically* is a different animal.  It stops
    joining almost everything it prices, and the loss is invisible — reconciliation
    rebuilds the key from the participants each source reported, so the reversed
    rows quietly become their own fixtures and the disagreement never reaches the
    check written for it.  Measured on the live slate with one book's team-sport
    rows reversed: 103 errors before reconciliation, zero after, the run passing,
    the book dropping from 133 shared fixtures to 30, and the only trace 110
    warnings describing it as a doubleheader ordinal correction.
    """
    for source, entries in sorted(outliers.items()):
        total = max(shared.get(source, 0), 1)
        rate = len(entries) / total
        # The worst *sport*, as well as the average.  Each adapter reads home and
        # away by a different per-sport convention, so the realistic bug is a
        # source reversed in one sport — which an overall rate hides behind the
        # sports it gets right: one book reversed in baseball alone scores 9%
        # against a 60% bar while being wrong on every baseball fixture.
        worst_sport, worst_rate = "", 0.0
        for (key, sport), sport_entries in by_sport.items():
            if key != source:
                continue
            sport_total = max(by_sport_shared.get((key, sport), 0), 1)
            if (
                len(sport_entries) >= MIN_ORIENTATION_DISAGREEMENTS
                and sport_total >= MIN_ORIENTATION_SPORT_FIXTURES
                and len(sport_entries) / sport_total > worst_rate
            ):
                worst_sport, worst_rate = sport, len(sport_entries) / sport_total
        systematic = (
            len(entries) >= MIN_ORIENTATION_DISAGREEMENTS
            and max(rate, worst_rate) > MAX_ORIENTATION_DISAGREEMENT_RATE
        )
        examples = ", ".join(sorted(key for key, _, _ in entries)[:_EXAMPLES])
        report.add(
            Severity.ERROR if systematic else Severity.WARNING,
            "home_away_disagreement",
            f"{source} disagrees with the other sources about which participant is "
            f"home on {len(entries)} of {total} shared fixture(s) "
            f"({rate * 100:.0f}%; e.g. {examples})"
            + (
                (
                    f" — {worst_rate * 100:.0f}% of them in {worst_sport} alone;"
                    if worst_sport and worst_rate > rate
                    else " —"
                )
                + " that is systematic rather than a neutral-venue judgement, so its "
                "orientation is wrong: reconciliation rebuilds the event key from the "
                "participants each source reports, which means these rows silently "
                "become their own fixtures and stop joining anything"
                if systematic
                else " — the two orientations produce different event keys, so nothing "
                "is mispaired, but the fixture is not compared across these sources"
            ),
            source=source,
        )


def _report_league_outliers(
    outliers: Mapping[tuple[str, str, str], Sequence[str]], report: ValidationReport
) -> None:
    for (source, claimed, consensus), events in sorted(outliers.items()):
        report.add(
            Severity.WARNING,
            "league_disagreement",
            f"{source} classifies {len(events)} shared fixture(s) as {claimed} where the "
            f"other sources say {consensus} (e.g. {', '.join(sorted(events)[:_EXAMPLES])})"
            " — league is not part of event identity, so the join is unaffected, but "
            "coverage reporting will split",
            source=source,
        )


def _report_time_outliers(
    outliers: Mapping[str, Sequence[tuple[str, timedelta, timedelta]]],
    report: ValidationReport,
) -> None:
    for source, entries in sorted(outliers.items()):
        worst_event, worst_delta, tolerance = max(entries, key=lambda item: item[1])
        report.add(
            # A warning, not an error: clustering has already decided these rows
            # describe one fixture and joined them, so the data is usable.  What
            # is left to say is that one book's clock looks wrong — worth
            # surfacing, but it does not make the run unclean, and grading it an
            # error would fail every run containing a tennis match whose books
            # published different "not before" estimates.
            Severity.WARNING,
            "start_time_disagreement",
            # Worded for both readings.  Where three or more sources priced the
            # fixture this is the distance from their median; where two did it
            # is the gap between them, and neither can be called the outlier —
            # saying "median start" there would have named a consensus that does
            # not exist.
            # The tolerance belongs to the worst entry, and entries for one
            # source can come from leagues with different thresholds — a 7-hour
            # tennis disagreement beside a 25-minute baseball one printed "2
            # fixtures, by more than 6:00:00", which is false of the second.  So
            # the threshold is quoted against the fixture it belongs to.
            f"{source} disagrees with the other sources about when {len(entries)} "
            f"fixture(s) start; worst is {worst_delta} on {worst_event}, against a "
            f"{tolerance} tolerance for that competition",
            source=source,
            event_key=worst_event,
        )


#: Below this many events for one (source, sport), a per-event coverage gap
#: cannot be told apart from ordinary thin pricing, so it is graded a warning
#: rather than a rename.  Eight is roughly a day's slate in the leagues collected
#: here — enough for "most" and "a few" to mean something.
MIN_EVENTS_TO_DIAGNOSE_A_RENAME = 8


def _expected_markets(
    quotes: Sequence[Quote],
    capabilities: Mapping[tuple[str, str], frozenset[Market]],
):
    """``(source, sport) -> {(market, FULL_GAME)}`` a source should have produced.

    A source's claim is taken as the union over the leagues it collects *of that
    sport*, intersected with the sport's core set: a claim can narrow what is
    expected, never widen it into markets the pipeline does not model.  A source
    that claims nothing at all is held to the full core set, which is the older
    and stricter reading and the safe direction to fail in.
    """
    leagues_by_source_sport: dict[tuple[str, Sport], set[str]] = defaultdict(set)
    for quote in quotes:
        leagues_by_source_sport[(quote.source, quote.sport)].add(quote.league)
    claimed_leagues: dict[str, set[str]] = defaultdict(set)
    for source, league in capabilities:
        claimed_leagues[source].add(league)

    def expected(source: str, sport: Sport) -> frozenset[tuple[Market, Period]]:
        core = core_markets(sport)
        if source not in claimed_leagues:
            return core
        relevant = leagues_by_source_sport[(source, sport)] & claimed_leagues[source]
        if not relevant:
            # The source claims leagues, but none of this sport's rows came from
            # one of them — so there is no claim to narrow by and the sport's own
            # core set stands.
            return core
        declared: set[Market] = set()
        for league in relevant:
            declared |= capabilities[(source, league)]
        return frozenset((market, period) for market, period in core if market in declared)

    return expected


def _check_coverage(
    quotes: Sequence[Quote],
    report: ValidationReport,
    capabilities: Mapping[tuple[str, str], frozenset[Market]],
    order_book_sources: frozenset[str] = frozenset(),
) -> None:
    """Catch an upstream rename that turns a real market into a silent skip.

    Scoped per (source, sport), because the expected market set differs by sport:
    tennis carries only the moneyline, so demanding totals of it would report
    every tennis book as broken, while a football book that stopped returning
    point spreads must still be an error.

    Narrowed further by what each source *claimed*, when it publishes claims.
    The distinction the claim buys is between "this market has disappeared" and
    "this source never offered it": a prediction market that prices moneylines
    and nothing else is not a broken sportsbook, and a ``--tier core`` run that
    deliberately skipped FanDuel's per-event soccer pages has not lost its
    handicaps.  Both used to be reported as *core_market_absent* — "a source
    label has probably changed" — which sends someone looking for a parsing fault
    that is not there.

    Republished mirrors (``an_*``, ``vi_*``, ``vsin_circa``) are held to the
    same softer bar as order books: the republisher often omits a market the
    book itself prices, and that is thin upstream coverage rather than a
    renamed label in *this* parser.  Taken from the registry's own
    classification rather than derived from :data:`src.redundancy.REDUNDANT_PAIRS`
    tuple positions, because position encodes which feed backs up which — not
    what a feed *is*: ``an_fanatics`` sits in the primary slot of its only pair
    (Fanatics has no first-party adapter) and was graded ERROR for that book's
    own market menu.  ``an_bet365`` was the second example until 2026-08-15,
    when bet365 gained a first-party adapter and its two feeds became the usual
    triangle — which is exactly why this reads the registry's classification
    rather than a tuple position that changes when a book gains a route.
    """
    from src.sources.registry import REPUBLISHED_SOURCE_KEYS

    soft_coverage = frozenset(order_book_sources) | REPUBLISHED_SOURCE_KEYS
    expected = _expected_markets(quotes, capabilities)
    by_source_sport: dict[tuple[str, Sport], set[tuple[Market, Period]]] = defaultdict(set)
    events_by_source_sport: dict[tuple[str, Sport], set[str]] = defaultdict(set)
    by_source_event: dict[tuple[str, Sport, str], set[tuple[Market, Period]]] = defaultdict(set)
    events_by_source: dict[str, set[str]] = defaultdict(set)
    events_by_sport: dict[Sport, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for quote in quotes:
        by_source_sport[(quote.source, quote.sport)].add((quote.market, quote.period))
        events_by_source_sport[(quote.source, quote.sport)].add(quote.event_key)
        by_source_event[(quote.source, quote.sport, quote.event_key)].add(
            (quote.market, quote.period)
        )
        events_by_source[quote.source].add(quote.event_key)
        events_by_sport[quote.sport][quote.source].add(quote.event_key)

    # Driven from the *declared* scopes as well as the arriving ones.
    #
    # Indexing only what arrived made the most important case unreachable: a
    # ``(source, sport)`` with no rows is not a key, so the loop never ran for
    # it and a whole sport disappearing was silent.  Ten such pairs sat in one
    # live capture — Smarkets returning nothing for four of its six sports among
    # them — and validation reported PASS with zero findings naming any of them.
    # The docstring above says this check exists to catch "an upstream rename
    # that turns a real market into a silent skip"; the rename that turns a whole
    # sport into a silent skip is precisely the one it could not see.
    declared_scopes = {
        (source, get_league(league).sport)
        for source, league in capabilities
        if is_known(league)
    }
    scopes = set(by_source_sport) | declared_scopes
    for source, sport in sorted(scopes, key=str):
        present = by_source_sport.get((source, sport), set())
        if not present and not events_by_source.get(source):
            # This source produced nothing at all.  It is already named by
            # ``source_unhealthy:<kind>`` and ``configured_sources_produced_nothing``
            # with its actual cause; adding one warning per declared sport that
            # offers "an off day, or a renamed scope" as the dichotomy buries the
            # real diagnosis under six wrong ones — on the failure that most
            # needs to read clearly.
            continue
        if not present:
            # Nothing at all, where something was promised.  Graded a warning
            # rather than an error because a single run cannot tell an offseason
            # from a broken feed — NHL in July is legitimately empty for months,
            # and a check that always says FAIL carries no signal.  What tells
            # them apart is history, and that is `source_stopped_producing`'s
            # job, one level coarser.
            report.add(
                Severity.WARNING,
                "declared_sport_produced_nothing",
                f"{source} declares {sport.value} and returned no rows for it at all "
                "— an off day, or a feed whose scope has been renamed out from "
                "under it; the two look identical in one run",
                source=source,
            )
            continue
        missing = expected(source, sport) - present
        if not missing:
            continue
        # A missing (market, period) has two very different causes, and only one of
        # them is a rename.
        #
        # If the source prices that *market* for this sport in some other scoring
        # window, nothing has been renamed — it has made a different choice about
        # which window to price.  Pinnacle's hockey friendlies are the live case:
        # the puck line and the total exist, but only for `regulation`, while the
        # moneyline is priced for `full_game`.  Reporting that as "a source label
        # has probably changed" sends someone looking for a parsing fault that is
        # not there, and it fails every run over a book's pricing decision.
        #
        # If the market type is absent from the sport altogether, that is the
        # rename this check exists to catch.
        periods_by_market: dict[Market, set[Period]] = defaultdict(set)
        for market, period in present:
            periods_by_market[market].add(period)
        event_count = len(events_by_source_sport[(source, sport)])

        elsewhere = sorted(
            (m, p) for m, p in missing if periods_by_market.get(m)
        )
        entirely_absent = sorted((m, p) for m, p in missing if not periods_by_market.get(m))

        if entirely_absent:
            # An exchange lists a market and waits for somebody to offer on it.
            # A whole sport's spreads with no resting order is a thin slate, not
            # a renamed label, and grading it an error fails the run over the
            # state of somebody else's order book.
            #
            # And a handful of events is not evidence of a rename either: the
            # per-event sibling below refuses to diagnose one under eight
            # events, while this branch would call a single spread-less hockey
            # friendly an ERROR.  Same claim, same evidence bar.
            small = event_count < MIN_EVENTS_TO_DIAGNOSE_A_RENAME
            report.add(
                Severity.WARNING if source in soft_coverage or small else Severity.ERROR,
                "core_market_absent",
                f"no {sport.value} rows at all for "
                + ", ".join(f"{m.value}/{p.value}" for m, p in entirely_absent)
                + f" across {event_count} {sport.value} events, and this source prices "
                "that market in no other window either — "
                + (
                    f"though at {event_count} event(s) that is one thin fixture, "
                    "not evidence of a renamed label"
                    if small
                    else "expected every game to have these, so a source label "
                    "has probably changed"
                ),
                source=source,
            )
        for market, period in elsewhere:
            other = ", ".join(sorted(p.value for p in periods_by_market[market]))
            report.add(
                Severity.WARNING,
                "core_market_in_a_different_period",
                f"{market.value} is priced for {sport.value} but at {other}, not at "
                f"{period.value}, across {event_count} events — the market exists, so "
                "this is a pricing choice by the book rather than a renamed label, but "
                f"it cannot be compared against another book's {period.value} price",
                source=source,
            )

    # Per event, not just per slate. A source that prices a core market for one
    # game out of sixteen satisfies the slate-wide check above while having lost
    # fifteen games' worth of markets — the shape of a *partial* upstream rename,
    # and invisible unless coverage is checked per game.
    #
    # Scoped to markets the source does emit somewhere for that sport, so a source
    # missing a market entirely is reported once by the check above rather than
    # twice, and graded per market by which way round the anomaly points:
    #
    # * present for most events, absent for a few → the market is what this source
    #   normally returns, so an absence is an error and looks like a rename.
    # * present for a few events, absent for most → the *presence* is the unusual
    #   thing, which is what thin pricing on distant fixtures looks like, and what
    #   a collector that only fetches per-event detail pages for a subset produces.
    #   FanDuel's soccer handicaps and totals live on the event-detail page and
    #   exist for one captured fixture out of 127; calling that an error would fail
    #   every run over a collection decision rather than over a data fault.
    for (source, sport), present_markets in sorted(
        by_source_sport.items(), key=lambda i: str(i[0])
    ):
        events = events_by_source_sport[(source, sport)]
        for market, period in sorted(expected(source, sport) & present_markets):
            have = {
                event_key
                for event_key in events
                if (market, period) in by_source_event[(source, sport, event_key)]
            }
            missing = events - have
            if not missing:
                continue
            fraction = len(have) / len(events)
            # A per-event gap is graded a WARNING, not an ERROR, and the reason is
            # that a single run cannot tell the two causes apart.
            #
            # A renamed label and ordinary market heterogeneity look identical from
            # here.  Live, this fired on Pinnacle lacking a handicap for 17 of 80
            # soccer fixtures and Kambi for 16 of 127 — which is simply what soccer
            # is: a Premier League match carries an Asian handicap and a Faroese
            # cup tie does not.  Graded ERROR, every real run failed validation
            # permanently, and a report that always says FAIL carries no signal at
            # all; the two genuine errors in that same run were buried under it.
            #
            # What *is* still an error is the market disappearing from a sport
            # entirely, which is what `core_market_absent` above reports.
            # Distinguishing a rename from thin pricing needs run-to-run history —
            # a spike against this source's own past coverage — which the store has
            # and this function, seeing one run, does not.
            if fraction >= 0.5 and len(events) >= MIN_EVENTS_TO_DIAGNOSE_A_RENAME:
                report.add(
                    Severity.WARNING,
                    "core_market_absent_for_event",
                    f"{len(missing)} of {len(events)} {sport.value} events lack "
                    f"{market.value}/{period.value}, which this source prices for the other "
                    f"{len(have)} (e.g. {sorted(missing)[0]}) — either those fixtures are "
                    "priced thinly or a market label has changed for some games; comparing "
                    "against this source's past coverage is what tells the two apart",
                    source=source,
                )
            else:
                report.add(
                    Severity.WARNING,
                    "core_market_thinly_offered",
                    f"{market.value}/{period.value} is present for only {len(have)} of "
                    f"{len(events)} {sport.value} events — either the book prices it thinly "
                    "on distant fixtures or the collector only fetches it for some events, "
                    "so this sport is largely uncomparable on that market",
                    source=source,
                )

    if len(events_by_source) > 1:
        # Cross-checking needs an event priced by two books, not by all of them:
        # the books' slates legitimately differ in size.
        per_event = Counter(event for events in events_by_source.values() for event in events)
        shared = [event for event, count in per_event.items() if count >= 2]
        if not shared:
            report.add(
                Severity.ERROR,
                "no_shared_events",
                "no event was priced by more than one source, so nothing can be cross-checked: "
                + "; ".join(f"{s}: {len(e)} events" for s, e in sorted(events_by_source.items())),
            )
        else:
            # Per sport this is a warning, not an error, and the distinction is a
            # fact about the calendar rather than about the code: on the day this
            # was built no hockey fixture was priced by two of the three books at
            # once, because one book had the season's openers and another had
            # club friendlies.  A sport nobody can cross-check contributes no
            # arbitrage, which is worth saying out loud without failing the run.
            for sport, by_source in sorted(events_by_sport.items(), key=lambda i: i[0].value):
                counts = Counter(event for events in by_source.values() for event in events)
                if not any(count >= 2 for count in counts.values()):
                    report.add(
                        Severity.WARNING,
                        "no_shared_events_for_sport",
                        f"no {sport.value} event was priced by two sources, so nothing in "
                        f"{sport.value} can be cross-checked (arbitrage needs two books on "
                        "one event): "
                        + "; ".join(f"{s}: {len(e)} events" for s, e in sorted(by_source.items())),
                    )

    _check_availability(quotes, report)


def _check_availability(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """Compare how much of each source is bettable.

    A source that marks nearly every row suspended contributes nothing
    downstream, because arbitrage can only be taken on active prices — and it
    does so while still reporting a healthy row count, so the loss is invisible
    in every other metric.  The tell is disagreement: books looking at the same
    slate at the same moment do not differ by 80 percentage points on whether it
    is open for betting.  A wrong ``status`` derivation (a timestamp field read
    as a boolean, say) looks exactly like this.
    """
    totals: Counter[str] = Counter()
    suspended: Counter[str] = Counter()
    for quote in quotes:
        totals[quote.source] += 1
        if quote.status is QuoteStatus.SUSPENDED:
            suspended[quote.source] += 1

    rates = {
        source: suspended[source] / count for source, count in totals.items() if count >= 20
    }
    if len(rates) < 2:
        return

    # Every source that clears the bar is named, and the reference is the
    # **least** suspended source rather than the median.
    #
    # Both halves of that are corrections, in opposite directions, of forms this
    # check has already had:
    #
    # * Naming only ``max(rates)`` reported one broken source when several shared
    #   a fault — four of five invisible.  Hence the loop.
    # * Comparing against the *median* then made it silent in exactly the case
    #   that matters most: once the broken sources are the majority they define
    #   the consensus, and nothing fires at all.  Measured: two of three sources
    #   at 100% suspended produced zero findings, and six of ten produced zero.
    #   A shared ``status`` bug across the five order-driven adapters would have
    #   passed in silence with healthy-looking row counts.
    #
    # The minimum is the right reference because the claim being made is
    # existential, not central: *some* source on this slate is finding it open,
    # so a source finding it 80% closed is not describing the same slate.  It is
    # not an order statistic that drifts with the source count either — the
    # absolute 80% floor is what carries the weight, and the gap only rules out
    # the case where every source agrees the slate really is shut.
    reference = min(rates.values())
    for source, rate in sorted(rates.items()):
        if rate < MAX_SUSPENDED_FRACTION or rate - reference < MIN_SUSPENSION_GAP:
            continue
        report.add(
            Severity.ERROR,
            "implausible_suspension_rate",
            f"{rate * 100:.0f}% of {source}'s rows are suspended while the most open "
            f"source on the same slate reports {reference * 100:.0f}% — only "
            f"{totals[source] - suspended[source]} of {totals[source]} rows are "
            "bettable, so this source cannot contribute to cross-book comparison; "
            "suspect the status derivation rather than the market",
            source=source,
        )
