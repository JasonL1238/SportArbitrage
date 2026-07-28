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
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Iterable, Sequence

from src.normalize import (
    MAX_DECIMAL_ODDS,
    MIN_DECIMAL_ODDS,
    american_to_decimal,
    is_plausible_decimal_odds,
)
from src.schema import (
    PERIODS_ALLOWING_DRAW,
    BaseballQuote,
    Market,
    Period,
    QuoteStatus,
    Selection,
)
from src.teams import canonical_team

#: Full-game markets every sportsbook in scope is expected to price for every
#: game it lists.  If one vanishes entirely, a criterion/marketType label has
#: probably been renamed upstream — which would otherwise look like a quiet
#: drop in row count.
CORE_MARKETS: frozenset[tuple[Market, Period]] = frozenset(
    {
        (Market.MONEYLINE, Period.FULL_GAME),
        (Market.RUN_LINE, Period.FULL_GAME),
        (Market.TOTAL_RUNS, Period.FULL_GAME),
    }
)

#: A complete two-way market always prices in the book's favour.  A sum below
#: 1.0 means the book is offering a guaranteed loss to itself, which in practice
#: means the parser paired the wrong prices or lines.
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

#: How far apart two books may place the same game's start time.
MAX_START_TIME_DISAGREEMENT = timedelta(minutes=20)

#: A source above this suspended fraction is contributing almost nothing that can
#: be bet, and needs to disagree with another source by at least the gap below
#: before that is called a fault rather than a genuinely closed slate.
MAX_SUSPENDED_FRACTION = 0.8
MIN_SUSPENSION_GAP = 0.5

#: A listed game should not be further out than this, nor already long past.
MAX_COMMENCE_HORIZON = timedelta(days=30)
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


def validate(quotes: Sequence[BaseballQuote]) -> ValidationReport:
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


def _check_rows(quotes: Iterable[BaseballQuote], report: ValidationReport) -> None:
    for quote in quotes:
        # Teams must still resolve; a passthrough string would break joining.
        for role, name in (("home", quote.home_team), ("away", quote.away_team)):
            if canonical_team(name) is None:
                report.add(
                    Severity.ERROR,
                    "unknown_team",
                    f"{role} team {name!r} is not an MLB club",
                    source=quote.source,
                    event_key=quote.event_key,
                )

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

        if quote.american_odds != 0:
            # Compared on net payout rather than on the decimal price, so the
            # tolerance means the same thing for a -5000 favourite as for a
            # +2400 longshot. Guarded because an out-of-domain American value is
            # itself the corruption being looked for.
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
            else:
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
        else:
            report.add(
                Severity.ERROR,
                "zero_american_odds",
                "american_odds is 0, which is not a price",
                source=quote.source,
                event_key=quote.event_key,
            )

        if quote.commence_time > quote.observed_at + MAX_COMMENCE_HORIZON:
            report.add(
                Severity.ERROR,
                "commence_time_too_far",
                f"game starts {quote.commence_time.isoformat()}, "
                f"{(quote.commence_time - quote.observed_at).days} days after collection — "
                "likely a futures market parsed as a game",
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

        if quote.line is not None and abs(quote.line) > 60:
            report.add(
                Severity.ERROR,
                "line_out_of_range",
                f"{quote.market} line {quote.line} is not a plausible baseball line — "
                "check for a units/scaling error",
                source=quote.source,
                event_key=quote.event_key,
            )


def _check_duplicates(quotes: Sequence[BaseballQuote], report: ValidationReport) -> None:
    """Two rows for the same priced selection from one book is a parser fault.

    It is how pitcher-conditional moneylines or alternate-line markets leak in
    as if they were the primary market.
    """
    seen: dict[tuple[str, ...], BaseballQuote] = {}
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


def _group_markets(
    quotes: Iterable[BaseballQuote],
) -> dict[tuple[str, str, str], list[BaseballQuote]]:
    grouped: dict[tuple[str, str, str], list[BaseballQuote]] = defaultdict(list)
    for quote in quotes:
        grouped[quote.market_key].append(quote)
    return grouped


def _check_markets(
    markets: dict[tuple[str, str, str], list[BaseballQuote]], report: ValidationReport
) -> None:
    for rows in markets.values():
        first = rows[0]
        source = first.source
        selections = {row.selection for row in rows}
        label = f"{first.market}/{first.period}" + (f"/{first.side}" if first.side else "")
        line_label = "" if first.line is None else f" @ {first.line:g}"

        # Homogeneity, before anything is inferred from row zero. Every check
        # below reads the market's identity off the first row and applies it to
        # the group, so a group that is not one market makes all of them
        # meaningless — and the failure is silent, because a fused group still
        # looks complete and still has a plausible-looking line set.
        mixed = {
            field
            for field in ("market", "period", "side")
            if len({getattr(row, field) for row in rows}) > 1
        }
        if mixed:
            report.add(
                Severity.ERROR,
                "heterogeneous_market",
                f"one source_market_id groups rows that differ in {sorted(mixed)}: "
                + "; ".join(
                    sorted(
                        {
                            f"{row.market.value}/{row.period.value}"
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

        # Completeness. Decided by how many selections the group has, not by
        # whether the draw is present — "require a draw only when a draw is
        # already there" can never detect a dropped draw leg, and a three-way
        # market missing its draw then fails the overround check instead, which
        # reports mispaired prices when the real fault is a missing row.
        if first.market is Market.MONEYLINE:
            required = {Selection.HOME, Selection.AWAY}
            if first.period in PERIODS_ALLOWING_DRAW and len(rows) >= 3:
                required = {Selection.HOME, Selection.AWAY, Selection.DRAW}
        elif first.market is Market.RUN_LINE:
            required = {Selection.HOME, Selection.AWAY}
        else:
            required = {Selection.OVER, Selection.UNDER}

        missing = required - selections
        if missing:
            report.add(
                Severity.WARNING,
                "incomplete_market",
                f"{label}{line_label} is missing {sorted(s.value for s in missing)}",
                source=source,
                event_key=first.event_key,
            )

        # Line agreement. A run line must mirror; everything else shares one
        # line outright. Both are checked — run lines were previously exempt
        # from the line-set check, so a group holding -1.5/+1.5 and -2.5/+2.5
        # passed as mirrored.
        if first.market is Market.RUN_LINE:
            by_selection = {row.selection: row for row in rows}
            home, away = by_selection.get(Selection.HOME), by_selection.get(Selection.AWAY)
            if home is not None and away is not None and home.line is not None and away.line is not None:
                if abs(home.line + away.line) > 1e-9:
                    report.add(
                        Severity.ERROR,
                        "run_line_not_mirrored",
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

        # Overround: does the book actually hold an edge?
        active = [row for row in rows if row.status is QuoteStatus.ACTIVE]
        if not missing and len(active) == len(rows) and len(rows) >= 2:
            overround = sum(row.implied_probability for row in rows)
            if overround < MIN_OVERROUND:
                report.add(
                    Severity.ERROR,
                    "negative_overround",
                    f"{label}{line_label} implied probabilities sum to {overround:.4f} < 1.0 — "
                    "a book does not price itself to lose, so the prices or lines are mispaired",
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


def _check_cross_source(quotes: Sequence[BaseballQuote], report: ValidationReport) -> None:
    """The whole point of one schema is that sources agree on identity."""
    by_event: dict[str, dict[str, list[BaseballQuote]]] = defaultdict(lambda: defaultdict(list))
    for quote in quotes:
        by_event[quote.event_key][quote.source].append(quote)

    for event_key, by_source in by_event.items():
        if len(by_source) < 2:
            continue
        teams = {
            source: (rows[0].home_team, rows[0].away_team) for source, rows in by_source.items()
        }
        if len({value for value in teams.values()}) > 1:
            report.add(
                Severity.ERROR,
                "home_away_disagreement",
                "sources disagree on the teams or which is home: "
                + "; ".join(f"{s}: {a} @ {h}" for s, (h, a) in teams.items()),
                event_key=event_key,
            )

        starts = {source: rows[0].commence_time for source, rows in by_source.items()}
        spread = max(starts.values()) - min(starts.values())
        if spread > MAX_START_TIME_DISAGREEMENT:
            report.add(
                Severity.ERROR,
                "start_time_disagreement",
                f"start times differ by {spread}: "
                + "; ".join(f"{s}: {t.isoformat()}" for s, t in starts.items()),
                event_key=event_key,
            )


def _check_coverage(quotes: Sequence[BaseballQuote], report: ValidationReport) -> None:
    """Catch an upstream rename that turns a real market into a silent skip."""
    by_source: dict[str, set[tuple[Market, Period]]] = defaultdict(set)
    events_by_source: dict[str, set[str]] = defaultdict(set)
    by_source_event: dict[tuple[str, str], set[tuple[Market, Period]]] = defaultdict(set)
    for quote in quotes:
        by_source[quote.source].add((quote.market, quote.period))
        events_by_source[quote.source].add(quote.event_key)
        by_source_event[(quote.source, quote.event_key)].add((quote.market, quote.period))

    for source, present in by_source.items():
        missing = CORE_MARKETS - present
        if missing:
            report.add(
                Severity.ERROR,
                "core_market_absent",
                "no rows at all for "
                + ", ".join(f"{m.value}/{p.value}" for m, p in sorted(missing))
                + f" across {len(events_by_source[source])} events — expected every game to have "
                "these, so a source label has probably changed",
                source=source,
            )

    # Per event, not just per slate. A source that prices the core markets for
    # one game out of sixteen satisfies the slate-wide check above while having
    # lost fifteen games' worth of markets — the shape of a *partial* upstream
    # rename, and invisible unless coverage is checked per game.
    #
    # Scoped to markets the source does emit somewhere, so a source missing a
    # market entirely is reported once by the check above rather than twice.
    incomplete: dict[str, list[str]] = defaultdict(list)
    for (source, event_key), present in by_source_event.items():
        expected = CORE_MARKETS & by_source[source]
        if expected - present:
            incomplete[source].append(event_key)
    for source, events in sorted(incomplete.items()):
        total = len(events_by_source[source])
        report.add(
            Severity.ERROR,
            "core_market_absent_for_event",
            f"{len(events)} of {total} events lack a core full-game market that this "
            f"source prices for other games (e.g. {sorted(events)[0]}) — a market label "
            "has probably changed for some games only",
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
                + "; ".join(f"{s}: {len(e)} events" for s, e in events_by_source.items()),
            )

    _check_availability(quotes, report)


def _check_availability(quotes: Sequence[BaseballQuote], report: ValidationReport) -> None:
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
