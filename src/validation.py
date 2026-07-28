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
from datetime import timedelta
from enum import StrEnum
from typing import Iterable, Sequence

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


def validate(quotes: Sequence[Quote]) -> ValidationReport:
    """Run every check over one run's worth of normalized rows."""
    report = ValidationReport(
        quote_count=len(quotes),
        event_count=len({q.event_key for q in quotes}),
        source_count=len({q.source for q in quotes}),
    )
    if not quotes:
        report.add(Severity.ERROR, "no_quotes", "validation received zero quotes")
        return report

    _check_rows(quotes, report)
    _check_duplicates(quotes, report)
    markets = _group_markets(quotes)
    report.market_count = len(markets)
    _check_markets(markets, report)
    _check_cross_source(quotes, report)
    _check_coverage(quotes, report)
    return report


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
    low, high = competition.plausible_total_range
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
    markets: dict[tuple[str, str, str], list[Quote]], report: ValidationReport
) -> None:
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
            report.add(
                Severity.ERROR,
                "draw_leg_missing",
                f"{label} prices only {sorted(s.value for s in selections)}, but a draw is "
                f"a settlement outcome of {first.sport.value}/{first.period.value} and the "
                "books price it — the draw leg was dropped, so this is not a fair two-way "
                "market and its probabilities do not sum to a market at all",
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
            if overround < MIN_OVERROUND:
                report.add(
                    Severity.ERROR,
                    "negative_overround",
                    f"{label}{line_label} implied probabilities sum to {overround:.4f} < 1.0 "
                    f"over {len(rows)} outcomes — a book does not price itself to lose, so "
                    "the prices or lines are mispaired",
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


# ── cross-source ─────────────────────────────────────────────────────────────


def _check_cross_source(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """The whole point of one schema is that sources agree on identity."""
    by_event: dict[str, dict[str, list[Quote]]] = defaultdict(lambda: defaultdict(list))
    for quote in quotes:
        by_event[quote.event_key][quote.source].append(quote)

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

        # Home/away is only checked where "home" names a real property of the
        # fixture.  In tennis there is no home player: each book orders the two
        # names however it likes and src.events.orient imposes its own ordering,
        # so comparing the books' orientations would report a disagreement that
        # does not exist.  The pair check above is what carries the weight there.
        if all(c.has_home_away for c in known):
            oriented = {
                source: (row.home_participant, row.away_participant)
                for source, row in rows.items()
            }
            if len(set(oriented.values())) > 1:
                report.add(
                    Severity.ERROR,
                    "home_away_disagreement",
                    "sources disagree on which participant is home: "
                    + "; ".join(
                        f"{s}: {away} @ {home}" for s, (home, away) in sorted(oriented.items())
                    ),
                    event_key=event_key,
                )

        # Books disagree about league classification constantly — the same tennis
        # match is "ATP Challenger Bonn - R1" at one book and "challenger" at
        # another — and league is deliberately not part of event identity, so this
        # is a warning about reporting, never an error about the join.
        leagues = {row.league for row in rows.values()}
        if len(leagues) > 1:
            report.add(
                Severity.WARNING,
                "league_disagreement",
                "sources classify this event under different leagues "
                f"({sorted(leagues)}); league is not part of event identity, so the join "
                "is unaffected, but coverage reporting will split",
                event_key=event_key,
            )

        # Start-time agreement, at the league's own tolerance.  20 minutes was
        # right for baseball and wrong for tennis, where each book publishes its
        # own "not before" estimate and the same match is routinely listed hours
        # apart.  The widest league present is used, matching what
        # src.events.reconcile_event_keys does when books classify one fixture
        # into two leagues.
        # The *reporting* threshold, not the clustering tolerance.  Clustering is
        # deliberately generous so a fixture two books time differently still
        # joins; reporting is strict so the disagreement is still visible once it
        # has.  Using the clustering width here would mean the wider a sport's
        # window got, the less it could ever notice — and soccer's window is 12
        # hours precisely because one book was three hours out on a kickoff.
        tolerance = max(c.time_disagreement_threshold for c in known)
        starts = {source: row.commence_time for source, row in rows.items()}
        spread = max(starts.values()) - min(starts.values())
        if spread > tolerance:
            report.add(
                # A warning, not an error: clustering has already decided these
                # rows describe one fixture and joined them, so the data is usable.
                # What is left to say is that one book's clock looks wrong — worth
                # surfacing, but it does not make the run unclean, and grading it an
                # error would fail every run containing a tennis match whose two
                # books published different "not before" estimates.
                Severity.WARNING,
                "start_time_disagreement",
                f"start times differ by {spread}, more than the {tolerance} that "
                f"{'/'.join(sorted({c.key for c in known}))} treats as one fixture: "
                + "; ".join(f"{s}: {t.isoformat()}" for s, t in sorted(starts.items())),
                event_key=event_key,
            )


#: Below this many events for one (source, sport), a per-event coverage gap
#: cannot be told apart from ordinary thin pricing, so it is graded a warning
#: rather than a rename.  Eight is roughly a day's slate in the leagues collected
#: here — enough for "most" and "a few" to mean something.
MIN_EVENTS_TO_DIAGNOSE_A_RENAME = 8


def _check_coverage(quotes: Sequence[Quote], report: ValidationReport) -> None:
    """Catch an upstream rename that turns a real market into a silent skip.

    Scoped per (source, sport), because the expected market set differs by sport:
    tennis carries only the moneyline, so demanding totals of it would report
    every tennis book as broken, while a football book that stopped returning
    point spreads must still be an error.
    """
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

    for (source, sport), present in sorted(by_source_sport.items(), key=lambda i: str(i[0])):
        missing = core_markets(sport) - present
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
            report.add(
                Severity.ERROR,
                "core_market_absent",
                f"no {sport.value} rows at all for "
                + ", ".join(f"{m.value}/{p.value}" for m, p in entirely_absent)
                + f" across {event_count} {sport.value} events, and this source prices "
                "that market in no other window either — expected every game to have "
                "these, so a source label has probably changed",
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
        for market, period in sorted(core_markets(sport) & present_markets):
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

    worst_source, worst_rate = max(rates.items(), key=lambda item: item[1])
    best_rate = min(rates.values())
    if worst_rate >= MAX_SUSPENDED_FRACTION and worst_rate - best_rate >= MIN_SUSPENSION_GAP:
        report.add(
            Severity.ERROR,
            "implausible_suspension_rate",
            f"{worst_rate * 100:.0f}% of rows are suspended while another source on the "
            f"same slate reports {best_rate * 100:.0f}% — only "
            f"{totals[worst_source] - suspended[worst_source]} of "
            f"{totals[worst_source]} rows are bettable, so this source cannot "
            "contribute to cross-book comparison; suspect the status derivation "
            "rather than the market",
            source=worst_source,
        )
