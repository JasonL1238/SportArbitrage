"""The collection pipeline and its command line.

One run does the same things for every source, in order: fetch, persist the raw
bytes, parse those bytes, reconcile event identity across *all* sources, validate,
look for arbitrage, persist.  Nothing downstream ever sees data that skipped a
step, and nothing is reported as collected unless it survived validation.  The
raw bytes land on disk **before** anything interprets them, which is what makes a
run reproducible offline.

A source that fails does not abort the run — the other sources still collect and
the failure is recorded as unhealthy, because unattended operation is a
requirement.  A run is only ``ok`` when validation passes *and* at least two
sportsbooks actually produced data.

That last bar is now reported **per sport** as well as overall, because "two
books responded" and "two books priced this sport" are different facts, and only
the second one makes a sport usable.  Two numbers are printed for every sport,
not one: how many books contributed, and how many fixtures more than one of them
priced.  They come apart, and when they do the second is the one that matters —
on the captured slate the three books all produce hockey, but Pinnacle's is a
Belarusian friendly while the NHL openers come from the other two, so a book
count alone would call hockey comparable when the overlap is what decides it.
A league a source was configured for and returned nothing from is reported as a
gap for that source, which is how "Pinnacle has no NHL today" is stated as a fact
rather than left to be inferred from a smaller number.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import signature
from pathlib import Path
from typing import Sequence

from src import settings
from src.arb import ArbReport, best_prices, find_opportunities
from src.events import reconcile_event_keys
from src.leagues import LEAGUES, LEAGUES_BY_SPORT, is_known
from src.leagues import league as get_league
from src.raw_store import RawResponse, RawStore
from src.schema import Quote, Sport
from src.sources.base import OddsSource, ParseOutcome, SourceHealth
from src.sources.betrivers_kambi import BetRiversKambiAdapter
from src.sources.fanduel import FanDuelAdapter
from src.sources.guards import SourceError
from src.sources.pinnacle import PinnacleAdapter
from src.store import IncompatibleDatabase, MigrationError, Store, migrate_database
from src.validation import Severity, ValidationReport, validate

log = logging.getLogger("collector")

#: Every source is a public endpoint of the sportsbook's own web experience,
#: fetched directly. No third-party odds API, key, account, or paid service.
SOURCE_FACTORIES = {
    "fanduel": FanDuelAdapter,
    "pinnacle": PinnacleAdapter,
    "betrivers_kambi": BetRiversKambiAdapter,
}

#: The goal requires two independent sportsbooks; one is not a pipeline.
MIN_HEALTHY_SOURCES = 2

#: Leagues collected when none are named.  The NBA is registered but left out:
#: it is in its offseason, so every book returns futures containers only, and
#: asking for it would report a permanent gap that says nothing about the
#: pipeline.  ``--league NBA`` still works the day the season starts.
DEFAULT_LEAGUES: tuple[str, ...] = tuple(
    league.key for league in LEAGUES if league.key != "NBA"
)

ALL_SPORTS: tuple[str, ...] = tuple(sport.value for sport in Sport)
ALL_LEAGUES: tuple[str, ...] = tuple(league.key for league in LEAGUES)


# ── coverage ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LeagueCoverage:
    """What one source was asked for in one league, and what it returned."""

    source_key: str
    league: str
    sport: str
    quote_count: int
    event_count: int

    @property
    def is_gap(self) -> bool:
        """Configured, but returned nothing.

        Worth naming as its own concept: a league that is absent from the results
        is indistinguishable from a league nobody requested unless what was
        requested is recorded too.
        """
        return self.quote_count == 0


@dataclass(frozen=True)
class SportCoverage:
    """Whether one sport is actually usable on this run."""

    sport: str
    sources: tuple[str, ...]
    """Sources that produced at least one row for this sport."""
    quote_count: int
    event_count: int
    cross_book_events: int
    """Fixtures in this sport priced by two or more books.

    Reported alongside the book count because the two can disagree, and when they
    do it is the number that matters.  Two books can each produce plenty of hockey
    while having no fixture in common — one has the NHL openers, the other a
    Belarusian friendly — and there is then still nothing to compare.
    """
    leagues: tuple[str, ...]
    missing_leagues: tuple[str, ...]
    """Leagues some source was configured to collect that produced nothing."""

    @property
    def meets_two_book_bar(self) -> bool:
        """Two or more books priced this sport."""
        return len(self.sources) >= MIN_HEALTHY_SOURCES

    @property
    def is_comparable(self) -> bool:
        """...and at least one fixture is priced by more than one of them."""
        return self.meets_two_book_bar and self.cross_book_events > 0

    def summary(self) -> str:
        verdict = "USABLE" if self.is_comparable else "NOT COMPARABLE"
        books = ", ".join(self.sources) or "no books"
        line = (
            f"{self.sport:<11} {verdict:<15} {self.quote_count:>6} quotes "
            f"{self.event_count:>4} fixtures ({self.cross_book_events} cross-book)  "
            f"{len(self.sources)} book(s): {books}"
        )
        if not self.meets_two_book_bar:
            line += f" — needs {MIN_HEALTHY_SOURCES}"
        elif not self.cross_book_events:
            line += " — no fixture priced by two of them"
        if self.missing_leagues:
            line += f"\n            no data for configured league(s): {', '.join(self.missing_leagues)}"
        return line


def configured_leagues(source: OddsSource) -> tuple[str, ...]:
    """The league keys a source says it is collecting.

    Read off the adapter's ``leagues`` property, which is the contract in
    :mod:`src.sources.base`.  An adapter that does not declare them gets an empty
    tuple and a warning during the run, rather than having its coverage silently
    reported against whatever it happened to return — which would make every gap
    invisible by construction.
    """
    declared = getattr(source, "leagues", None)
    if not declared:
        return ()
    return tuple(str(getattr(key, "value", key)) for key in declared)


def league_coverage(
    source_key: str, quotes: Sequence[Quote], configured: Sequence[str]
) -> list[LeagueCoverage]:
    """Per-league counts for one source, including its configured-but-empty leagues."""
    quotes_by_league: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        quotes_by_league[quote.league].append(quote)

    coverage: list[LeagueCoverage] = []
    for key in sorted(set(configured) | set(quotes_by_league)):
        rows = quotes_by_league.get(key, [])
        sport = (
            rows[0].sport.value
            if rows
            else (get_league(key).sport.value if is_known(key) else "unknown")
        )
        coverage.append(
            LeagueCoverage(
                source_key=source_key,
                league=key,
                sport=sport,
                quote_count=len(rows),
                event_count=len({row.event_key for row in rows}),
            )
        )
    return coverage


def sport_coverage(
    quotes: Sequence[Quote], coverage: Sequence[LeagueCoverage]
) -> list[SportCoverage]:
    """Roll per-source league coverage up into a per-sport verdict.

    Both inputs are needed: *quotes* say what arrived, *coverage* says what was
    asked for.  A sport that was configured and produced nothing at all still
    gets a row, reported as priced by zero books — which is the honest answer,
    and the one an empty section of the report cannot give.
    """
    books: dict[str, set[str]] = defaultdict(set)
    seen_leagues: dict[str, set[str]] = defaultdict(set)
    rows: dict[str, list[Quote]] = defaultdict(list)
    books_per_event: dict[tuple[str, str], set[str]] = defaultdict(set)
    for quote in quotes:
        sport = quote.sport.value
        books[sport].add(quote.source)
        seen_leagues[sport].add(quote.league)
        rows[sport].append(quote)
        books_per_event[(sport, quote.event_key)].add(quote.source)

    requested: dict[str, set[str]] = defaultdict(set)
    for entry in coverage:
        requested[entry.sport].add(entry.league)

    result: list[SportCoverage] = []
    for sport in sorted(set(rows) | set(requested)):
        # A league one book covered is not a gap for the *sport* even if another
        # book returned nothing for it; that is a per-source gap, reported on its
        # own.  Here a gap means no source produced a row for it at all.
        missing = requested[sport] - seen_leagues[sport]
        result.append(
            SportCoverage(
                sport=sport,
                sources=tuple(sorted(books[sport])),
                quote_count=len(rows[sport]),
                event_count=len({row.event_key for row in rows[sport]}),
                cross_book_events=sum(
                    1
                    for (event_sport, _), sources in books_per_event.items()
                    if event_sport == sport and len(sources) >= MIN_HEALTHY_SOURCES
                ),
                leagues=tuple(sorted(seen_leagues[sport])),
                missing_leagues=tuple(sorted(missing)),
            )
        )
    return result


# ── one run ──────────────────────────────────────────────────────────────────


class RunResult:
    """What one collection run produced."""

    def __init__(
        self,
        run_id: int | None,
        quotes: list[Quote],
        health: list[SourceHealth],
        report: ValidationReport,
        arb: ArbReport | None = None,
        coverage: Sequence[SportCoverage] = (),
        league_coverage: Sequence[LeagueCoverage] = (),
        excluded_by_filter: int = 0,
    ) -> None:
        self.run_id = run_id
        self.quotes = quotes
        self.health = health
        self.report = report
        self.arb = arb
        self.coverage = list(coverage)
        self.league_coverage = list(league_coverage)
        self.excluded_by_filter = excluded_by_filter

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def usable_sports(self) -> list[str]:
        """Sports with two or more books *and* a fixture they both priced."""
        return [c.sport for c in self.coverage if c.is_comparable]

    @property
    def unusable_sports(self) -> list[str]:
        return [c.sport for c in self.coverage if not c.is_comparable]

    @property
    def sports_meeting_book_bar(self) -> list[str]:
        """Sports two or more books contributed to, whatever the overlap."""
        return [c.sport for c in self.coverage if c.meets_two_book_bar]

    def print_summary(self) -> None:
        print(f"\n=== run {self.run_id if self.run_id is not None else '(not stored)'} ===")
        for health in sorted(self.health, key=lambda h: h.source_key):
            print(f"  {health.summary()}")

        print("\n  per-sport coverage (two or more books is the bar for a sport being usable):")
        if not self.coverage:
            print("    nothing collected")
        for entry in self.coverage:
            print(f"    {entry.summary()}")
        if self.coverage:
            print(f"    two or more books contributed: {', '.join(self.sports_meeting_book_bar) or 'none'}")
            print(f"    usable (and a fixture in common): {', '.join(self.usable_sports) or 'none'}")
            print(f"    NOT comparable: {', '.join(self.unusable_sports) or 'none'}")

        gaps = [entry for entry in self.league_coverage if entry.is_gap]
        if gaps:
            print("\n  configured leagues that returned nothing:")
            for entry in sorted(gaps, key=lambda e: (e.sport, e.league, e.source_key)):
                print(f"    {entry.source_key:<16} {entry.league:<16} ({entry.sport})")

        if self.excluded_by_filter:
            print(
                f"\n  {self.excluded_by_filter:,} row(s) collected but excluded by the "
                "sport/league filter on this command"
            )

        print(f"\n  validation: {self.report.summary()}")
        for finding in self.report.errors[:20]:
            print(f"    ERROR   {finding.code}: {finding.message}")
        for finding in self.report.warnings[:10]:
            print(f"    warning {finding.code}: {finding.message}")
        extra_errors = len(self.report.errors) - 20
        extra_warnings = len(self.report.warnings) - 10
        if extra_errors > 0:
            print(f"    ... {extra_errors} more errors")
        if extra_warnings > 0:
            print(f"    ... {extra_warnings} more warnings")

        if self.arb is not None:
            print(f"  arbitrage: {self.arb.summary()}")
            for opportunity in self.arb.opportunities[:10]:
                print(opportunity.describe())
            # Rejections are printed because "found nothing" and "found
            # something and refused it for a stated reason" are different facts.
            rejected = Counter(d.code for d in self.arb.diagnostics)
            if rejected:
                print(f"    rejected: {dict(rejected)}")


def resolve_leagues(
    sports: Sequence[str] | None = None, leagues: Sequence[str] | None = None
) -> tuple[str, ...]:
    """Which leagues to configure the adapters for.

    Naming leagues wins; naming only sports takes every registered league of
    those sports (including ones left out of :data:`DEFAULT_LEAGUES`, since
    asking for the sport is asking for it); naming neither takes the defaults.
    """
    if leagues:
        unknown = [key for key in leagues if not is_known(key)]
        if unknown:
            raise SystemExit(
                f"unknown league(s): {unknown}; known: {list(ALL_LEAGUES)}"
            )
        selected = tuple(leagues)
        if sports:
            selected = tuple(key for key in selected if get_league(key).sport.value in sports)
            if not selected:
                raise SystemExit(
                    f"no league in {list(leagues)} belongs to sport(s) {list(sports)}"
                )
        return selected
    if sports:
        return tuple(
            league.key for sport in sports for league in LEAGUES_BY_SPORT[Sport(sport)]
        )
    return DEFAULT_LEAGUES


def build_sources(
    keys: Sequence[str] | None = None, *, leagues: Sequence[str] | None = None
) -> list[OddsSource]:
    """Instantiate the requested adapters, configured for *leagues*.

    With no *leagues*, each adapter keeps **its own** default.  That is not
    laziness: the three books cover different competitions — Pinnacle has a
    hockey catch-all neither of the others exposes — so a single list imposed
    from here would either be the intersection (silently narrowing every book to
    the least capable one) or a superset that makes adapters reject it outright.
    The adapter knows its own coverage; the collector overrides only when an
    operator explicitly asks for a scope.

    When leagues *are* named and a book cannot serve some of them, the book is
    configured for the subset it accepts and the shortfall is reported.  It is not
    silently dropped, and it does not take the whole run down either: a book with
    no tennis is a fact about that book, not an error in the command.
    """
    selected = list(keys) if keys else list(SOURCE_FACTORIES)
    unknown = [key for key in selected if key not in SOURCE_FACTORIES]
    if unknown:
        raise SystemExit(f"unknown source(s): {unknown}; known: {sorted(SOURCE_FACTORIES)}")

    built: list[OddsSource] = []
    for key in selected:
        factory = SOURCE_FACTORIES[key]
        if not _accepts_leagues(factory):
            log.warning(
                "%s does not accept a league list, so it will collect whatever it "
                "defaults to; its coverage is reported against what it declares",
                key,
            )
            built.append(factory(timeout=settings.HTTP_TIMEOUT))
            continue
        if not leagues:
            built.append(factory(timeout=settings.HTTP_TIMEOUT))
            continue
        source = _build_with_leagues(key, factory, tuple(leagues))
        if source is not None:
            built.append(source)
    if not built:
        raise SystemExit(
            f"none of {selected} can collect league(s) {list(leagues or DEFAULT_LEAGUES)}"
        )
    return built


def _accepts_leagues(factory: type) -> bool:
    try:
        return "leagues" in signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return False


def _build_with_leagues(key: str, factory: type, wanted: Sequence[str]):
    """Configure one adapter for as many of *wanted* as it will accept."""
    try:
        return factory(leagues=wanted, timeout=settings.HTTP_TIMEOUT)
    except (ValueError, KeyError) as exc:
        log.info("%s refused the requested league list (%s); narrowing", key, exc)

    accepted: list[str] = []
    for league_key in wanted:
        # Probed one at a time rather than by reading the adapter's internals:
        # which leagues a book covers is the adapter's business, and the only
        # part of that it publishes is whether it accepts the key.
        try:
            probe = factory(leagues=[league_key], timeout=settings.HTTP_TIMEOUT)
        except (ValueError, KeyError):
            continue
        probe.close()
        accepted.append(league_key)

    missing = [league_key for league_key in wanted if league_key not in accepted]
    if missing:
        log.warning("%s cannot collect %s; it will not be asked for them", key, missing)
    if not accepted:
        log.warning("%s cannot collect any of %s; leaving it out of this run", key, list(wanted))
        return None
    return factory(leagues=accepted, timeout=settings.HTTP_TIMEOUT)


def in_scope(
    quote: Quote, sports: Sequence[str] | None, leagues: Sequence[str] | None
) -> bool:
    if sports and quote.sport.value not in sports:
        return False
    if leagues and quote.league not in leagues:
        return False
    return True


def collect_once(
    sources: Sequence[OddsSource],
    *,
    raw_store: RawStore,
    store: Store | None,
    as_of: datetime | None = None,
    sports: Sequence[str] | None = None,
    leagues: Sequence[str] | None = None,
) -> RunResult:
    """Fetch, persist raw, parse, reconcile, validate, find arbitrage, persist.

    *as_of* is the moment arbitrage is judged against, defaulting to now.  It is
    injectable so a test can pin it rather than depending on the wall clock.

    *sports* and *leagues* narrow what is *kept*, and they are applied **after**
    reconciliation rather than before it.  Order matters: books disagree about
    league classification, so filtering first could remove the rows that let a
    fixture cluster correctly across books, and event identity would then depend
    on a command-line flag.  Rows dropped this way are counted and recorded on
    the run, never silently discarded.
    """
    started_at = datetime.now(UTC)
    run_id = store.start_run(started_at) if store else None

    all_quotes: list[Quote] = []
    health_reports: list[SourceHealth] = []
    coverage: list[LeagueCoverage] = []
    undeclared: list[str] = []

    for source in sources:
        health, outcome = _collect_source(source, raw_store=raw_store, store=store, run_id=run_id)
        health_reports.append(health)
        all_quotes.extend(outcome.quotes)

        declared = configured_leagues(source)
        if not declared:
            undeclared.append(source.source_key)
        per_league = league_coverage(source.source_key, outcome.quotes, declared)
        coverage.extend(per_league)

        if store is not None and run_id is not None:
            store.save_health(run_id, health)
            store.save_rejections(run_id, outcome.rejections)
            store.save_skipped(run_id, source.source_key, dict(outcome.skipped))
            store.save_league_coverage(
                run_id,
                source.source_key,
                [(e.league, e.sport, e.quote_count, e.event_count) for e in per_league],
            )

    # Event identity is settled across all sources at once, before anything is
    # validated or compared. Each adapter can only number doubleheaders over its
    # own slate, which makes "#2" a per-source ordinal rather than an identity;
    # left uncorrected, one book's game 2 joins onto another book's game 1.
    all_quotes, rekeys = reconcile_event_keys(all_quotes)

    excluded = 0
    if sports or leagues:
        kept = [q for q in all_quotes if in_scope(q, sports, leagues)]
        excluded = len(all_quotes) - len(kept)
        all_quotes = kept
        # Coverage is reported for the scope that was asked about.  Leaving the
        # out-of-scope leagues in would report every one of them as a gap, which
        # is the opposite of the truth: they were deliberately not kept.
        coverage = [
            entry
            for entry in coverage
            if (not sports or entry.sport in sports)
            and (not leagues or entry.league in leagues)
        ]

    report = validate(all_quotes)
    _check_source_count(health_reports, report)

    sports_seen = sport_coverage(all_quotes, coverage)
    _report_sport_coverage(sports_seen, report)
    for source_key in undeclared:
        report.add(
            Severity.WARNING,
            "source_declares_no_leagues",
            f"{source_key} does not declare which leagues it was configured to collect, "
            "so a league it silently returned nothing for cannot be told apart from one "
            "that was never requested",
            source=source_key,
        )
    for rekey in rekeys:
        report.add(
            Severity.WARNING,
            "event_key_reconciled",
            f"corrected a per-source doubleheader ordinal: {rekey}",
            source=rekey.source,
            event_key=rekey.now,
        )

    # Gated on the clock: a live run only cares about games not yet started.
    arb_report = find_opportunities(all_quotes, as_of=as_of or datetime.now(UTC))

    if store is not None and run_id is not None:
        # One unusable run must not end an unattended watch loop, and it must
        # not be recorded as though nothing happened either.
        try:
            store.save_quotes(run_id, all_quotes)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to persist quotes for run %s", run_id)
            report.add(
                Severity.ERROR,
                "quotes_not_persisted",
                f"{type(exc).__name__}: {exc} — the run's rows violate a storage "
                "invariant, most likely two prices for one selection",
            )
        store.save_findings(run_id, report.findings)
        store.finish_run(
            run_id,
            finished_at=datetime.now(UTC),
            report=report,
            note=_run_note(sports_seen, sports, leagues, excluded),
        )

    return RunResult(
        run_id,
        all_quotes,
        health_reports,
        report,
        arb_report,
        coverage=sports_seen,
        league_coverage=coverage,
        excluded_by_filter=excluded,
    )


def _run_note(
    coverage: Sequence[SportCoverage],
    sports: Sequence[str] | None,
    leagues: Sequence[str] | None,
    excluded: int,
) -> str:
    """One line stored with the run, so its scope is recoverable later."""
    parts = []
    usable = [c.sport for c in coverage if c.is_comparable]
    single = [c.sport for c in coverage if not c.is_comparable]
    parts.append(f"comparable sports (2+ books on one fixture): {', '.join(usable) or 'none'}")
    if single:
        parts.append(f"not comparable: {', '.join(single)}")
    if sports:
        parts.append(f"--sport {','.join(sports)}")
    if leagues:
        parts.append(f"--league {','.join(leagues)}")
    if excluded:
        parts.append(f"{excluded} rows excluded by filter")
    return "; ".join(parts)


def _collect_source(
    source: OddsSource,
    *,
    raw_store: RawStore,
    store: Store | None,
    run_id: int | None,
) -> tuple[SourceHealth, ParseOutcome]:
    checked_at = datetime.now(UTC)
    started = time.perf_counter()
    raws: list[RawResponse] = []

    try:
        raws = source.fetch_raw()
        latency_ms = (time.perf_counter() - started) * 1000
    except SourceError as exc:
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_kind=getattr(exc, "kind", "source_error"),
                error_message=str(exc),
            ),
            ParseOutcome(),
        )
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
        log.exception("unexpected failure fetching %s", source.source_key)
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_kind="unexpected",
                error_message=f"{type(exc).__name__}: {exc}",
            ),
            ParseOutcome(),
        )

    # Raw bytes land on disk before anything interprets them.
    unchanged_payloads = 0
    for raw in raws:
        path = raw_store.write(raw)
        unchanged = False
        if store is not None and run_id is not None:
            previous = store.previous_sha(raw.source, raw.endpoint, before_run_id=run_id)
            unchanged = previous is not None and previous == raw.sha256
            store.record_raw(run_id, raw, path, unchanged=unchanged)
        unchanged_payloads += int(unchanged)

    try:
        outcome = source.parse(raws)
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected failure parsing %s", source.source_key)
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                request_count=len(raws),
                raw_bytes=sum(raw.byte_size for raw in raws),
                latency_ms=latency_ms,
                unchanged_payloads=unchanged_payloads,
                error_kind=getattr(exc, "kind", "parse_error"),
                error_message=f"{type(exc).__name__}: {exc}",
            ),
            ParseOutcome(),
        )

    # A rejection means the source offered in-scope data this parser could not
    # represent faithfully, so it counts against health — but it must always
    # come with an explanation.
    error_kind: str | None = None
    error_message: str | None = None
    if not outcome.quotes:
        error_kind = "empty_after_parse"
        error_message = (
            f"parsed 0 quotes from {len(raws)} responses; skipped={dict(outcome.skipped)}"
        )
    elif outcome.rejections:
        reasons = Counter(rejection.reason for rejection in outcome.rejections)
        error_kind = "rejections"
        error_message = (
            f"{len(outcome.rejections)} record(s) rejected: {dict(reasons)}; "
            f"first: {outcome.rejections[0].detail}"
        )

    return (
        SourceHealth(
            source_key=source.source_key,
            ok=bool(outcome.quotes) and not outcome.rejections,
            checked_at=checked_at,
            request_count=len(raws),
            raw_bytes=sum(raw.byte_size for raw in raws),
            latency_ms=latency_ms,
            quote_count=len(outcome.quotes),
            event_count=len(outcome.event_keys),
            rejection_count=len(outcome.rejections),
            skipped_count=sum(outcome.skipped.values()),
            unchanged_payloads=unchanged_payloads,
            error_kind=error_kind,
            error_message=error_message,
        ),
        outcome,
    )


def _check_source_count(health: Sequence[SourceHealth], report: ValidationReport) -> None:
    producing = [h.source_key for h in health if h.quote_count > 0]
    if len(producing) < MIN_HEALTHY_SOURCES:
        report.add(
            Severity.ERROR,
            "insufficient_sources",
            f"only {len(producing)} source(s) produced data ({producing or 'none'}); "
            f"at least {MIN_HEALTHY_SOURCES} are required",
        )


def _report_sport_coverage(
    coverage: Sequence[SportCoverage], report: ValidationReport
) -> None:
    """State which sports cleared the two-book bar, and which did not.

    A warning rather than an error: a sport priced by one book is not a defect in
    the pipeline — today there genuinely is no cross-book hockey — but it does
    mean nothing in that sport can be compared, and a run that stayed silent
    about it would let a healthy overall row count imply otherwise.
    """
    for entry in coverage:
        if not entry.meets_two_book_bar:
            report.add(
                Severity.WARNING,
                "sport_below_two_books",
                f"{entry.sport}: only {len(entry.sources)} book "
                f"({', '.join(entry.sources) or 'none'}) priced it, so none of its "
                f"{entry.quote_count} rows can be compared across books",
            )
        elif not entry.cross_book_events:
            report.add(
                Severity.WARNING,
                "sport_without_cross_book_fixtures",
                f"{entry.sport}: {len(entry.sources)} books priced it but they have no "
                f"fixture in common across {entry.event_count} fixtures, so none of its "
                f"{entry.quote_count} rows can be compared",
            )
        for missing in entry.missing_leagues:
            report.add(
                Severity.WARNING,
                "league_returned_nothing",
                f"{missing} was configured for collection but no source returned a "
                "single row for it",
            )


# ── replay ───────────────────────────────────────────────────────────────────


def replay_run(
    run_id: int,
    *,
    store: Store,
    raw_store: RawStore,
    sports: Sequence[str] | None = None,
    leagues: Sequence[str] | None = None,
) -> tuple[bool, list[str]]:
    """Re-parse a stored run's raw responses and compare to what was stored.

    This is the offline reproduction guarantee: if parsing is not a pure function
    of the captured bytes, this fails.  A sport/league scope narrows *both* sides
    of the comparison, so replaying one sport out of a mixed run is not mistaken
    for the parser having lost every other sport's rows.

    Two things have to happen in the same order they happened during collection,
    or the comparison is not like-for-like and reports a failure that is really an
    artefact of the replay:

    **Reconciliation must be re-applied.**  ``reconcile_event_keys`` can rewrite
    ``event_key``, which is part of ``dedup_key``, so stored rows carry
    reconciled keys and freshly parsed ones do not.  Without this step replay
    failed whenever reconciliation had changed anything — a tennis match that
    Pinnacle timed 5½ hours before FanDuel came back as "lost" at one date and
    "invented" at the next, and a soccer fixture two books timed differently came
    back with a phantom ``#2``.  Both were correct behaviour being reported as
    corruption.

    **Reconciliation must run before the scope filter, over every source.**
    Clustering start times is a global operation: which fixture a row belongs to
    depends on what the *other* books said about it.  Filtering to one sport first
    would cluster a subset and could legitimately produce different keys from the
    ones stored.
    """
    problems: list[str] = []
    stored = store.load_quotes(run_id, sports=sports, leagues=leagues)
    by_source_paths: dict[str, list[Path]] = {}
    for source_key, path in store.raw_paths(run_id):
        by_source_paths.setdefault(source_key, []).append(path)

    if not by_source_paths:
        return False, [f"run {run_id} has no stored raw responses"]

    replayed: list[Quote] = []
    for source_key, paths in by_source_paths.items():
        factory = SOURCE_FACTORIES.get(source_key)
        if factory is None:
            problems.append(f"{source_key}: no adapter available to replay this source")
            continue
        source = factory()
        try:
            raws = [raw_store.read(path) for path in paths]
            # Unscoped on purpose — see the note above.  Scoping happens after
            # reconciliation, because clustering needs every source's view of a
            # fixture to land on the same key the collector stored.
            replayed.extend(source.parse(raws).quotes)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{source_key}: replay raised {type(exc).__name__}: {exc}")
        finally:
            source.close()

    replayed, _ = reconcile_event_keys(replayed)
    replayed = [quote for quote in replayed if in_scope(quote, sports, leagues)]

    stored_map = {q.dedup_key: q for q in stored}
    replay_map = {q.dedup_key: q for q in replayed}

    if len(stored) != len(replayed):
        problems.append(f"row count differs: stored {len(stored)}, replayed {len(replayed)}")

    missing = set(stored_map) - set(replay_map)
    added = set(replay_map) - set(stored_map)
    for key in sorted(missing)[:5]:
        problems.append(f"replay lost row: {key}")
    for key in sorted(added)[:5]:
        problems.append(f"replay invented row: {key}")

    for key in sorted(set(stored_map) & set(replay_map)):
        before, after = stored_map[key], replay_map[key]
        if abs(before.decimal_odds - after.decimal_odds) > 1e-9:
            problems.append(
                f"odds changed on replay for {key}: {before.decimal_odds} -> {after.decimal_odds}"
            )
        if before.observed_at != after.observed_at:
            problems.append(f"observed_at changed on replay for {key}")
        if len(problems) > 20:
            break

    return not problems, problems


# ── CLI ──────────────────────────────────────────────────────────────────────


def _filter_args(args: argparse.Namespace) -> tuple[list[str] | None, list[str] | None]:
    """The sport/league filter this invocation asked for."""
    return (getattr(args, "sport", None), getattr(args, "league", None))


def _scope_label(sports: Sequence[str] | None, leagues: Sequence[str] | None) -> str:
    parts = []
    if sports:
        parts.append(f"sport={','.join(sports)}")
    if leagues:
        parts.append(f"league={','.join(leagues)}")
    return f" [{'; '.join(parts)}]" if parts else ""


def _open_store() -> Store:
    """Open the configured database, turning a version clash into a clean exit."""
    try:
        return Store(settings.DB_PATH)
    except IncompatibleDatabase as exc:
        raise SystemExit(str(exc)) from None


def _cmd_collect(args: argparse.Namespace) -> int:
    sports, leagues = _filter_args(args)
    raw_store = RawStore(settings.RAW_DIR)
    sources = build_sources(args.source, leagues=resolve_leagues(sports, leagues))
    store = None if args.no_store else _open_store()
    exit_code = 0
    try:
        iteration = 0
        while True:
            iteration += 1
            result = collect_once(
                sources,
                raw_store=raw_store,
                store=store,
                sports=sports,
                leagues=leagues,
            )
            result.print_summary()
            exit_code = 0 if result.ok else 1
            if not args.watch:
                break
            if args.max_runs and iteration >= args.max_runs:
                break
            log.info("sleeping %ss before next run", args.interval)
            time.sleep(args.interval)
    finally:
        for source in sources:
            source.close()
        if store is not None:
            store.close()
    return exit_code


def _cmd_migrate(args: argparse.Namespace) -> int:
    """Upgrade an older database in place, keeping a backup."""
    path = Path(args.db) if args.db else settings.DB_PATH
    if not path.exists():
        print(f"{path} does not exist; nothing to migrate")
        return 1
    try:
        migration = migrate_database(path, backup=not args.no_backup)
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if migration.from_version == migration.to_version:
        print(f"{path} is already at schema version {migration.to_version}")
        return 0
    print(migration.summary())
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    sports, leagues = _filter_args(args)
    raw_store = RawStore(settings.RAW_DIR)
    with _open_store() as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs to replay")
            return 1
        ok, problems = replay_run(
            run_id, store=store, raw_store=raw_store, sports=sports, leagues=leagues
        )
        print(
            f"replay of run {run_id}{_scope_label(sports, leagues)}: "
            f"{'PASS' if ok else 'FAIL'}"
        )
        for problem in problems[:25]:
            print(f"  {problem}")
        return 0 if ok else 1


def _cmd_runs(args: argparse.Namespace) -> int:
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        rows = store.run_summaries(limit=args.limit, sports=sports, leagues=leagues)
        if not rows:
            print(f"no runs recorded{_scope_label(sports, leagues)}")
            return 1
        print(f"{'run':>4}  {'started':<26} {'ok':<3} {'quotes':>7} {'events':>7} {'err':>4} {'warn':>5}")
        for row in rows:
            print(
                f"{row['id']:>4}  {row['started_at']:<26} {str(bool(row['ok'])):<5}"
                f"{row['quote_count']:>7} {row['event_count']:>7}"
                f"{row['error_count']:>4} {row['warning_count']:>5}"
            )
            for health in store.health_for_run(row["id"]):
                status = "ok" if health["ok"] else f"FAILED[{health['error_kind']}]"
                print(
                    f"        {health['source_key']:<16} {status:<24}"
                    f"{health['quote_count']:>6} quotes {health['event_count']:>4} events"
                )
            # Per-sport, because "the run stored 12,000 rows" says nothing about
            # whether any one sport is comparable across books.
            cross = store.cross_book_event_counts(row["id"], min_books=MIN_HEALTHY_SOURCES)
            for entry in store.sport_coverage(row["id"]):
                if sports and entry["sport"] not in sports:
                    continue
                shared = cross.get(entry["sport"], 0)
                if entry["source_count"] < MIN_HEALTHY_SOURCES:
                    bar = "1 BOOK ONLY"
                elif not shared:
                    bar = "NO OVERLAP"
                else:
                    bar = "usable"
                print(
                    f"          {entry['sport']:<12} {bar:<12}"
                    f"{entry['quote_count']:>7} quotes {entry['event_count']:>4} fixtures "
                    f"({shared} cross-book) {entry['source_count']} book(s), "
                    f"{entry['league_count']} league(s)"
                )
            gaps = [g for g in store.league_coverage(row["id"]) if g["quote_count"] == 0]
            for gap in gaps:
                if sports and gap["sport"] not in sports:
                    continue
                if leagues and gap["league"] not in leagues:
                    continue
                print(
                    f"          gap: {gap['source_key']} returned nothing for "
                    f"{gap['league']} ({gap['sport']})"
                )
            if row["note"]:
                print(f"          note: {row['note']}")
        return 0


def _cmd_show(args: argparse.Namespace) -> int:
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs")
            return 1
        clause, params = _sql_scope(sports, leagues)
        rows = store.query(
            f"""SELECT source, sport, league, event_key, home_team, away_team, commence_time,
                       market, period, side, selection, line, decimal_odds, american_odds,
                       status, observed_at
                  FROM quote WHERE run_id = ?{clause}
                 ORDER BY sport, league, event_key, market, period, side, line, selection
                 LIMIT ?""",
            (run_id, *params, args.limit),
        )
        print(f"run {run_id}{_scope_label(sports, leagues)}: showing {len(rows)} rows")
        for row in rows:
            line = "" if row["line"] is None else f" {row['line']:+g}"
            side = f" {row['side']}" if row["side"] else ""
            print(
                f"  {row['source']:<16} {row['sport']:<11} {row['league']:<8}"
                f"{row['event_key']:<34} {row['market']:<11}"
                f"{row['period']:<16}{side:<5} {row['selection']:<6}{line:<7}"
                f" {row['decimal_odds']:>7.3f} {row['american_odds']:>+5d}  {row['status']}"
            )
        return 0


def _sql_scope(
    sports: Sequence[str] | None, leagues: Sequence[str] | None
) -> tuple[str, list[str]]:
    clause = ""
    params: list[str] = []
    if sports:
        clause += f" AND sport IN ({','.join('?' * len(sports))})"
        params.extend(sports)
    if leagues:
        clause += f" AND league IN ({','.join('?' * len(leagues))})"
        params.extend(leagues)
    return clause, params


def _cmd_arb(args: argparse.Namespace) -> int:
    """Re-run arbitrage detection over a stored run, without refetching."""
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs")
            return 1
        quotes, _ = reconcile_event_keys(
            store.load_quotes(run_id, sports=sports, leagues=leagues)
        )
        report = find_opportunities(
            quotes, total_stake=args.stake, min_margin=args.min_margin / 100.0
        )
        print(f"run {run_id}{_scope_label(sports, leagues)}: {report.summary()}")
        for opportunity in report.opportunities:
            print(opportunity.describe())
        rejected = Counter(d.code for d in report.diagnostics)
        if rejected:
            print(f"\nrejected: {dict(rejected)}")
            if args.verbose:
                for diagnostic in report.diagnostics:
                    print(f"  [{diagnostic.code}] {diagnostic.event_key} "
                          f"{diagnostic.market.value}/{diagnostic.period.value}: {diagnostic.detail}")
        return 0


def _cmd_lines(args: argparse.Namespace) -> int:
    """Best available price per selection, across books — the line-shopping view.

    This is the surface arbitrage is drawn from, so printing it is how you tell a
    genuine "no edge today" apart from a market nobody is actually comparing.
    """
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        run_id = args.run or store.latest_run_id()
        if run_id is None:
            print("no stored runs")
            return 1
        quotes, _ = reconcile_event_keys(
            store.load_quotes(run_id, sports=sports, leagues=leagues)
        )
        surface = best_prices(quotes)
        sport_of = {quote.event_key: (quote.sport.value, quote.league) for quote in quotes}

        shown = 0
        for key in sorted(surface, key=str):
            selections = surface[key]
            sources = {quote.source for quote in selections.values()}
            if args.cross_book_only and len(sources) < 2:
                continue
            event_key, market, period, side, line = key
            sport, league = sport_of.get(event_key, ("?", "?"))
            label = f"{market.value}/{period.value}" + (f"/{side.value}" if side else "")
            line_label = "" if line is None else f" @ {line:+g}"
            overround = sum(1.0 / q.decimal_odds for q in selections.values())
            print(
                f"[{sport}/{league}] {event_key} {label}{line_label}  (sum {overround:.4f})"
            )
            for selection, quote in sorted(selections.items(), key=lambda i: i[0].value):
                print(
                    f"    {selection.value:<6} {quote.decimal_odds:>7.3f} "
                    f"{quote.american_odds:>+6d}  {quote.source}"
                )
            shown += 1
            if shown >= args.limit:
                break
        print(f"\n{shown} market(s) shown of {len(surface)}{_scope_label(sports, leagues)}")
        return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Per-source health across recent runs, so a slow decay is visible."""
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        rows = store.run_summaries(limit=args.limit, sports=sports, leagues=leagues)
        if not rows:
            print(f"no runs recorded{_scope_label(sports, leagues)}")
            return 1
        per_source: dict[str, list[bool]] = {}
        for row in rows:
            for health in store.health_for_run(row["id"]):
                per_source.setdefault(health["source_key"], []).append(bool(health["ok"]))

        print(f"{'source':<18} {'runs':>5} {'ok':>5} {'rate':>6}  last")
        worst_ok = True
        for source_key, outcomes in sorted(per_source.items()):
            ok_count = sum(outcomes)
            rate = ok_count / len(outcomes)
            recent = "".join("." if ok else "X" for ok in outcomes)
            print(f"{source_key:<18} {len(outcomes):>5} {ok_count:>5} {rate * 100:>5.0f}%  {recent}")
            if rate < args.min_rate:
                worst_ok = False

        latest = rows[0]
        # Health per source is not health per sport: a book can be perfectly
        # healthy and still be the only book pricing a sport.
        print(f"\nlatest run {latest['id']} — which sports are comparable across books:")
        coverage = [
            entry
            for entry in store.sport_coverage(latest["id"])
            if not sports or entry["sport"] in sports
        ]
        cross = store.cross_book_event_counts(latest["id"], min_books=MIN_HEALTHY_SOURCES)
        if not coverage:
            print("  no rows stored for this scope")
        for entry in coverage:
            shared = cross.get(entry["sport"], 0)
            if entry["source_count"] < MIN_HEALTHY_SOURCES:
                bar = "1 BOOK ONLY"
            elif not shared:
                bar = "NO OVERLAP"
            else:
                bar = "usable"
            print(
                f"  {entry['sport']:<12} {bar:<12} {entry['source_count']} book(s), "
                f"{entry['quote_count']:>6} quotes, {entry['event_count']:>4} fixtures, "
                f"{shared} priced by two or more books"
            )
        by_sport: dict[str, list[str]] = {}
        for entry in store.health_by_sport(latest["id"], sports=sports):
            by_sport.setdefault(entry["sport"], []).append(
                f"{entry['source']} {entry['quote_count']}"
            )
        for sport, entries in sorted(by_sport.items()):
            print(f"    {sport:<12} {', '.join(entries)}")

        print(
            f"\nlatest run {latest['id']} at {latest['started_at']}: "
            f"{'ok' if latest['ok'] else 'FAILED'}, {latest['quote_count']} quotes, "
            f"{latest['error_count']} errors, {latest['warning_count']} warnings"
        )
        unusable = [
            entry["sport"]
            for entry in coverage
            if entry["source_count"] < MIN_HEALTHY_SOURCES
            or not cross.get(entry["sport"], 0)
        ]
        if unusable:
            print(f"not comparable across books: {', '.join(unusable)}")
        if not worst_ok:
            print(f"a source is below the {args.min_rate * 100:.0f}% success threshold")
        return 0 if worst_ok and latest["ok"] else 1


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    """``--sport``/``--league``, on every subcommand that reads or writes rows."""
    parser.add_argument(
        "--sport",
        action="append",
        choices=list(ALL_SPORTS),
        help="restrict to this sport; repeatable",
    )
    parser.add_argument(
        "--league",
        action="append",
        choices=list(ALL_LEAGUES),
        metavar="LEAGUE",
        help=f"restrict to this league; repeatable. known: {', '.join(ALL_LEAGUES)}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    notice = settings.deprecation_notice()
    if notice:
        log.warning("%s", notice)

    parser = argparse.ArgumentParser(
        prog="python -m src.collector",
        description=(
            "Collect and normalize real betting data for several sports from public "
            "sportsbook endpoints."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    collect = subparsers.add_parser("collect", help="run one collection pass (default)")
    collect.add_argument(
        "--source", action="append", choices=sorted(SOURCE_FACTORIES), help="repeatable"
    )
    collect.add_argument("--watch", action="store_true", help="collect repeatedly")
    collect.add_argument("--interval", type=int, default=settings.DEFAULT_INTERVAL_SECONDS)
    collect.add_argument("--max-runs", type=int, default=0, help="0 means unlimited")
    collect.add_argument("--no-store", action="store_true", help="skip the database, still store raw")
    _add_scope_arguments(collect)
    collect.set_defaults(func=_cmd_collect)

    replay = subparsers.add_parser("replay", help="re-parse a stored run's raw responses")
    replay.add_argument("--run", type=int, help="run id; defaults to the most recent")
    _add_scope_arguments(replay)
    replay.set_defaults(func=_cmd_replay)

    runs = subparsers.add_parser("runs", help="list recent runs and per-source health")
    runs.add_argument("--limit", type=int, default=10)
    _add_scope_arguments(runs)
    runs.set_defaults(func=_cmd_runs)

    show = subparsers.add_parser("show", help="print normalized rows from a run")
    show.add_argument("--run", type=int)
    show.add_argument("--limit", type=int, default=40)
    _add_scope_arguments(show)
    show.set_defaults(func=_cmd_show)

    arb = subparsers.add_parser("arb", help="find arbitrage in a stored run")
    arb.add_argument("--run", type=int, help="run id; defaults to the most recent")
    arb.add_argument("--stake", type=float, default=100.0, help="bankroll per position")
    arb.add_argument(
        "--min-margin", type=float, default=0.0, help="minimum edge to report, in percent"
    )
    arb.add_argument("--verbose", action="store_true", help="explain every rejected market")
    _add_scope_arguments(arb)
    arb.set_defaults(func=_cmd_arb)

    lines = subparsers.add_parser("lines", help="best price per market across books")
    lines.add_argument("--run", type=int, help="run id; defaults to the most recent")
    lines.add_argument("--limit", type=int, default=20)
    lines.add_argument(
        "--cross-book-only",
        action="store_true",
        help="only markets priced by more than one book",
    )
    _add_scope_arguments(lines)
    lines.set_defaults(func=_cmd_lines)

    health = subparsers.add_parser("health", help="per-source success rate across recent runs")
    health.add_argument("--limit", type=int, default=20, help="how many runs to look back over")
    health.add_argument("--min-rate", type=float, default=0.8, help="failure threshold, 0-1")
    _add_scope_arguments(health)
    health.set_defaults(func=_cmd_health)

    migrate = subparsers.add_parser(
        "migrate", help="upgrade an older database to the current schema"
    )
    migrate.add_argument("--db", help=f"database path; defaults to {settings.DB_PATH}")
    migrate.add_argument(
        "--no-backup", action="store_true", help="do not copy the file aside first"
    )
    migrate.set_defaults(func=_cmd_migrate)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["collect", *(argv or [])])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
