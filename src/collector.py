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
**venues** actually produced data — any two, not two sportsbooks: two exchanges
can be compared with each other, and refusing that would throw away a usable
slate.  A run where *no* sportsbook produced is warned about by name, because a
book's posted price and an order book's resting offer are different things.

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
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path
from typing import Any, Callable, Collection, Iterator, Mapping, Sequence
from uuid import uuid4

from src import settings
from src.alerts import notify_opportunities
from src.arb import (
    STAKE_INCREMENT,
    MAX_OBSERVATION_SPREAD,
    MAX_PRICE_AGE,
    ArbReport,
    _counterparties,
    best_prices,
    counterparty_groups,
    describe_age,
    find_opportunities,
    merge_counterparty_groups,
)
from src.settlement import mismatch as settlement_mismatch
from src.commission import commission_for, net_decimal_odds
from src.distinctness import (
    MIRROR_AGREEMENT_RATE,
    Agreement,
    compare_all,
    find_mirrors,
)
from src.coverage import LocalityMarking, check_book_coverage, locality_marking
from src.redundancy import check_redundancy, is_redundant_pair
from src.events import reconcile_event_keys
from src.leagues import LEAGUES, LEAGUES_BY_SPORT, is_known
from src.leagues import league as get_league
from src.normalize import decimal_to_american
from src.raw_store import RawResponse, RawStore
from src.schema import Market, Quote, QuoteStatus, Sport
from src.sources import registry
from src.sources._common import Tier
from src.sources.base import OddsSource, ParseOutcome, SourceHealth
from src.sources.guards import SourceError
from src.store import IncompatibleDatabase, MigrationError, Store, migrate_database
from src.validation import Severity, ValidationReport, validate

log = logging.getLogger("collector")

#: How to build each registered source, derived from
#: :data:`src.sources.registry.SOURCES`.
#:
#: Every source is a public endpoint of the venue's own web experience, fetched
#: directly. No third-party odds API, key, account, or paid service.
#:
#: The values are ``functools.partial`` objects with the instance's configuration
#: already bound — the Kambi operator token, say — which is what makes a *source*
#: an instance rather than a module.  A bare class could not express two books
#: served by one adapter, and two books served by one adapter is what the Kambi
#: platform is.  Kept as a plain mapping because it is also the seam tests inject
#: a fake source through.
SOURCE_FACTORIES: dict[str, Callable[..., OddsSource]] = {
    entry.key: entry.factory() for entry in registry.SOURCES
}


def replay_factory(stored_state: str, source_key: str) -> Callable[..., OddsSource]:
    """The adapter to re-parse a stored run's captures with.

    Resolved from the **run's** jurisdiction, never from ``settings.STATE``.  A
    republisher's config *is* a state licence — ``an_parx`` is ``book_id 74`` in
    Pennsylvania and ``1929`` in New Jersey — and :data:`SOURCE_FACTORIES` holds the
    base descriptors, whose config is whichever state the base entry happens to
    name.  ``replay`` used to short-circuit to those whenever the run's state
    matched this process's, so the *matching* case re-parsed PA captures under NJ
    book ids while a mismatched box got it right: a PASS/FAIL verdict that turned on
    an environment variable.

    The lax ``replay_descriptor_for_state`` is correct here, unlike on a fetch path.
    Replay enumerates whatever the run stored and opens no socket, so a base
    descriptor for a key the state does not license is the honest way to say "no
    exact route for this one" rather than a substitution.

    Scoped to the sources whose configuration *is* a state licence — the exact-state
    retail routes and the state-licensed republishers.  Everything else is
    state-neutral (Pinnacle asks the same question in every jurisdiction), so it
    comes from :data:`SOURCE_FACTORIES`, which keeps working as the injection seam
    for global sources and for the synthetic keys tests build.  Resolving *every*
    key through the registry would refuse a key the registry has never heard of,
    which is not a locality problem.

    A single seam on purpose: it is the one place a test can inject a replay-only
    adapter for a state-sensitive source without having to know how state config is
    resolved.
    """
    state_sensitive = (
        registry.RETAIL_SOURCE_KEYS | registry.STATE_LICENSED_REPUBLISHER_KEYS
    )
    if stored_state in ("", "GLOBAL") or source_key not in state_sensitive:
        return SOURCE_FACTORIES[source_key]
    return registry.replay_descriptor_for_state(stored_state, source_key).factory()


class CachedOddsSource:
    """Delegate parsing/capabilities while fetching one batch's globals once."""

    def __init__(self, source: OddsSource, *, capture_id: str) -> None:
        self._source = source
        self._capture_id = capture_id
        self._raws: list[RawResponse] | None = None

    @property
    def source_key(self) -> str:
        return self._source.source_key

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        if self._raws is None:
            self._raws = [
                replace(raw, capture_id=raw.capture_id or self._capture_id)
                for raw in _fetch(self._source, tier)
            ]
        return list(self._raws)

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return self._source.parse(raws)

    def close(self) -> None:
        self._source.close()

#: Comparison needs two counterparties; one is not a pipeline.  This is the
#: *comparability* bar and nothing else — whether the run as a whole is healthy is
#: judged against what was configured and against this source's own recent
#: history, because "two of thirty sources answered" is not a healthy run even
#: though it clears this floor.  See :func:`_check_source_health`.
MIN_HEALTHY_SOURCES = 2

#: Below this share of the configured venues answering, a run is broken rather
#: than thin — however many of the survivors can still be compared with each
#: other.  Half is deliberately generous: it passes a bad day and fails a
#: pipeline that has lost most of its sources.  Eight of ten geo-blocked on a
#: fresh database used to report zero errors and exit 0, because the floor above
#: was clear and the regression check needs a history it did not have.
MIN_PRODUCING_SHARE = 0.5

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
    """Fixtures in this sport priced by two or more **counterparty** books.

    Reported alongside the book count because the two can disagree, and when they
    do it is the number that matters.  Two books can each produce plenty of hockey
    while having no fixture in common — one has the NHL openers, the other a
    Belarusian friendly — and there is then still nothing to compare.

    View-only feeds do not count toward it, matching
    :meth:`src.store.Store.cross_book_event_counts` — a mirror is not a
    counterparty, so a fixture priced by ten mirrors is still uncompared.  The
    two definitions diverged on the first all-republisher run, whose stored
    note said "comparable sports: baseball…" two lines below a summary reading
    "0 cross-book", and whose no-overlap warning stayed silent.
    """
    counterparty_sources: tuple[str, ...]
    """The subset of :attr:`sources` a bet could actually be placed against."""
    leagues: tuple[str, ...]
    missing_leagues: tuple[str, ...]
    """Leagues some source was configured to collect that produced nothing."""

    @property
    def meets_two_book_bar(self) -> bool:
        """Two or more counterparty books priced this sport."""
        return len(self.counterparty_sources) >= MIN_HEALTHY_SOURCES

    @property
    def is_comparable(self) -> bool:
        """...and at least one fixture is priced by more than one of them."""
        return self.meets_two_book_bar and self.cross_book_events > 0

    def summary(self) -> str:
        verdict = "USABLE" if self.is_comparable else "NOT COMPARABLE"
        books = ", ".join(self.sources) or "no feeds"
        # "feed(s)": ``sources`` counts every feed with rows, view-only mirrors
        # included; a bar-meeting sport with 2 counterparties and 8 mirrors
        # printed "10 book(s)" unqualified.
        line = (
            f"{self.sport:<11} {verdict:<15} {self.quote_count:>6} quotes "
            f"{self.event_count:>4} fixtures ({self.cross_book_events} cross-book)  "
            f"{len(self.sources)} feed(s): {books}"
        )
        if not self.meets_two_book_bar:
            view_only_count = len(self.sources) - len(self.counterparty_sources)
            line += f" — needs {MIN_HEALTHY_SOURCES} counterparties"
            if view_only_count:
                line += f" ({view_only_count} of the feeds are view-only)"
        elif not self.cross_book_events:
            line += " — no fixture priced by two counterparties"
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
    quotes: Sequence[Quote],
    coverage: Sequence[LeagueCoverage],
    *,
    view_only: frozenset[str] = frozenset(),
) -> list[SportCoverage]:
    """Roll per-source league coverage up into a per-sport verdict.

    Both inputs are needed: *quotes* say what arrived, *coverage* says what was
    asked for.  A sport that was configured and produced nothing at all still
    gets a row, reported as priced by zero books — which is the honest answer,
    and the one an empty section of the report cannot give.

    *view_only* is the run's non-counterparty set
    (:func:`src.sources.registry.view_only_for_run`): those feeds appear in the
    inventory but never in the comparability verdict, the same exclusion
    ``runs`` and ``arb`` apply.  Without it the first all-republisher run
    recorded "comparable sports (2+ books on one fixture): baseball" about a
    slate on which ``arb`` correctly found 0 cross-book markets.
    """
    books: dict[str, set[str]] = defaultdict(set)
    counterparties: dict[str, set[str]] = defaultdict(set)
    seen_leagues: dict[str, set[str]] = defaultdict(set)
    rows: dict[str, list[Quote]] = defaultdict(list)
    books_per_event: dict[tuple[str, str], set[str]] = defaultdict(set)
    for quote in quotes:
        sport = quote.sport.value
        books[sport].add(quote.source)
        seen_leagues[sport].add(quote.league)
        rows[sport].append(quote)
        if quote.source not in view_only:
            counterparties[sport].add(quote.source)
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
                counterparty_sources=tuple(sorted(counterparties[sport])),
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
        marking: LocalityMarking | None = None,
    ) -> None:
        self.run_id = run_id
        self.quotes = quotes
        self.health = health
        self.report = report
        self.arb = arb
        self.coverage = list(coverage)
        self.league_coverage = list(league_coverage)
        self.excluded_by_filter = excluded_by_filter
        self.marking = marking

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
        """Sports two or more *counterparty* books contributed to.

        Counterparties, not feeds, since the two-book bar became
        counterparty-aware — a 10-mirror sport is not two books anyone can
        bet both sides at, and the label printing this list says the same.
        """
        return [c.sport for c in self.coverage if c.meets_two_book_bar]

    def print_summary(self) -> None:
        print(f"\n=== run {self.run_id if self.run_id is not None else '(not stored)'} ===")
        for health in sorted(self.health, key=lambda h: h.source_key):
            print(f"  {health.summary()}")

        print(
            "\n  per-sport coverage (two or more counterparty books is the bar "
            "for a sport being usable):"
        )
        if not self.coverage:
            print("    nothing collected")
        for entry in self.coverage:
            print(f"    {entry.summary()}")
        if self.coverage:
            print(
                "    two or more counterparty books: "
                f"{', '.join(self.sports_meeting_book_bar) or 'none'}"
            )
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
            # The same labels ``arb`` prints for the same run: this summary is
            # the *live* path's only rendering, so leaving the tags off here
            # showed a wholly-foreign position as though it were the state's.
            marking = self.marking
            note = (
                None
                if marking is None
                else (lambda s: "" if marking.leg_is_local(s) else f"  [{marking.label()}]")
            )
            for opportunity in self.arb.opportunities[:10]:
                print(opportunity.describe(leg_note=note))
                if marking is not None and not marking.has_local_leg(opportunity):
                    print(
                        f"    no leg reachable from {marking.state} — "
                        f"informational, not {marking.state} prices"
                    )
            # Accounted out loud: the flagged finding says every position is
            # shown and labelled, and a silent [:10] slice would make that
            # false for whatever ranked eleventh.
            overflow = len(self.arb.opportunities) - 10
            if overflow > 0:
                print(
                    f"    ... {overflow} more position(s); "
                    "`collector arb --run <this run>` prints all of a stored run"
                )
            # Rejections are printed because "found nothing" and "found
            # something and refused it for a stated reason" are different facts.
            rejected = Counter(d.code for d in self.arb.diagnostics)
            if rejected:
                print(f"    rejected: {dict(rejected)}")


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    detected_state: str
    runs: tuple[tuple[str, RunResult], ...]

    @property
    def ok(self) -> bool:
        return bool(self.runs) and all(result.ok for _, result in self.runs)


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
    keys: Sequence[str] | None = None,
    *,
    leagues: Sequence[str] | None = None,
    state: str | None = None,
    route_scope: str = "all",
    allow_empty: bool = False,
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
    if state is None and route_scope == "all":
        factories = SOURCE_FACTORIES
    else:
        if route_scope == "global":
            descriptors = registry.global_sources()
        elif route_scope == "state":
            if state is None:
                raise ValueError("state is required for state-scoped source construction")
            descriptors = registry.state_sources_for_state(state)
        elif route_scope == "state_republished":
            if state is None:
                raise ValueError(
                    "state is required for state-republished source construction"
                )
            descriptors = registry.republished_sources_for_state(state)
        elif route_scope == "all":
            if state is None:
                raise ValueError("state is required for explicit all-scope construction")
            # "All" has to mean all three groups.  Splitting the republishers out
            # of ``global_sources()`` narrowed this branch to the retail routes
            # plus the twelve state-licensed republishers, silently dropping every
            # global venue — Pinnacle, the exchanges, the prediction markets.  No
            # caller reaches it today (``collect_batch_once`` asks for each scope
            # by name), which is exactly why it would be believed if one did.
            # No dedup filter between the three groups, because they are disjoint by
            # construction: ``global_sources()`` excludes both
            # ``RETAIL_SOURCE_KEYS`` and ``STATE_LICENSED_REPUBLISHER_KEYS``, and
            # ``republished_sources_for_state`` returns only keys from the latter.  A
            # filter here would never fire, and a guard that cannot fire is a
            # standing claim that these sets might overlap — the opposite of the
            # invariant the split exists to hold.  It is *asserted* just below
            # instead, because the dict comprehension that follows would otherwise
            # absorb an overlap silently, last-wins, having already built and
            # abandoned the losing adapter with its transport open.
            descriptors = (
                *registry.state_sources_for_state(state),
                # The state's own ids, not the base config: a republisher's book id
                # is a state licence, so applying it here is what stops the run
                # storing another state's books under this state's keys.
                *registry.republished_sources_for_state(state),
                *registry.global_sources(),
            )
        else:
            raise ValueError(f"unknown route scope {route_scope!r}")
        # Before ``factory()`` is called, so a duplicate cannot leak an instance.
        seen_keys = Counter(entry.key for entry in descriptors)
        clashing = sorted(key for key, count in seen_keys.items() if count > 1)
        if clashing:
            raise ValueError(
                f"route scope {route_scope!r} in {state} built {clashing} more than "
                "once; the scope groups are meant to be disjoint, and one instance "
                "would silently replace the other"
            )
        factories = {entry.key: entry.factory() for entry in descriptors}

    known = {entry.key for entry in registry.SOURCES}
    unknown = [key for key in (keys or ()) if key not in known]
    if unknown:
        raise SystemExit(f"unknown source(s): {unknown}; known: {sorted(known)}")
    selected = [key for key in (keys or factories) if key in factories]
    # Slow sources last.  Every price in a run has to be comparable with the
    # others, and a source paced to its own rate limit can take minutes — so
    # where it sits in the order decides how many *other* sources it pushes out
    # of that window.  Smarkets in the middle cost Kalshi and Polymarket 847
    # comparable markets between them, purely by being ahead of them.
    selected.sort(key=lambda key: key in registry.SLOW_SOURCES)
    built: list[OddsSource] = []
    for key in selected:
        factory = factories[key]
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
    if not built and not allow_empty:
        # Which question failed matters, because the two answers are different
        # commands.  Asking for a source this scope does not build is a scope
        # mismatch: ``build_sources(["an_caesars"], route_scope="global")`` died with
        # "none of [] can collect league(s) ['MLB', ...]", which blames the league
        # list for a key the scope had already filtered out, and sends the reader to
        # look at leagues.  Only the leftover case keeps the league message.
        outside = [key for key in (keys or ()) if key not in factories]
        if outside and not selected:
            where = ", ".join(
                f"{key} is built by route scope {_scope_that_builds(key)!r}"
                for key in outside
            )
            raise SystemExit(
                f"route scope {route_scope!r}"
                + (f" in {state}" if state else "")
                + f" builds none of the requested source(s) {outside} — {where}. "
                "That is a scope mismatch, not a league one"
            )
        raise SystemExit(
            f"none of {selected} can collect league(s) {list(leagues or DEFAULT_LEAGUES)}"
        )
    return built


def _scope_that_builds(key: str) -> str:
    """The ``route_scope`` that instantiates *key*, for error messages.

    Three disjoint groups, and the split is not obvious from a source key: the
    Action Network mirrors look global — one host, no proxy — but their book id is a
    state licence, so they are built per state.
    """
    if key in registry.RETAIL_SOURCE_KEYS:
        return "state"
    if key in registry.STATE_LICENSED_REPUBLISHER_KEYS:
        return "state_republished"
    return "global"


def _accepts_leagues(factory: Callable[..., OddsSource]) -> bool:
    return registry.accepts_leagues(factory)


def _build_with_leagues(key: str, factory: Callable[..., OddsSource], wanted: Sequence[str]):
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
    tier: Tier = Tier.FULL,
    on_progress: Callable[[Mapping[str, Any]], None] | None = None,
    alert: bool = True,
    jurisdiction: str | None = None,
    batch_id: str | None = None,
    route_scope: str = "legacy",
    state_source_keys: Sequence[str] = (),
) -> RunResult:
    """Fetch, persist raw, parse, reconcile, validate, find arbitrage, persist.

    *as_of* is the moment arbitrage is judged against, defaulting to now.  It is
    injectable so a test can pin it rather than depending on the wall clock.

    *tier* is the request budget.  ``core`` asks every source only for the
    endpoints that return a whole league at once, which is a few requests per
    source and is what makes a short polling interval defensible; ``full`` adds
    the per-event follow-ups.  It is recorded on the run and reflected in what
    each source *claims* to price, so a market missing because it was not asked
    for is never reported as a market that disappeared.

    *sports* and *leagues* narrow what is *kept*, and they are applied **after**
    reconciliation rather than before it.  Order matters: books disagree about
    league classification, so filtering first could remove the rows that let a
    fixture cluster correctly across books, and event identity would then depend
    on a command-line flag.  Rows dropped this way are counted and recorded on
    the run, never silently discarded.

    *on_progress*, when set, is called with small status dicts as the pass moves
    through books and post-fetch work.  Callers that serve a UI use this to show
    live progress without coupling the pipeline to HTTP.
    """
    def _progress(payload: Mapping[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(payload)
        except Exception:  # noqa: BLE001 — progress must never abort a collect
            log.debug("on_progress failed", exc_info=True)

    run_state = (jurisdiction or settings.STATE).strip().upper()
    started_at = datetime.now(UTC)
    run_id = (
        store.start_run(
            started_at,
            sports=sports,
            leagues=leagues,
            jurisdiction=run_state,
            batch_id=batch_id,
            route_scope=route_scope,
        )
        if store
        else None
    )
    # Identifies this pass on every response it captures.  A run id would do
    # where there is a store, but a store-less collection has none, and the
    # value only ever has to be *different* between passes.
    capture_id = uuid4().hex[:16]

    all_quotes: list[Quote] = []
    health_reports: list[SourceHealth] = []
    coverage: list[LeagueCoverage] = []
    undeclared: list[str] = []
    claimed: dict[tuple[str, str], frozenset[Market]] = {}
    source_list = list(sources)
    total_sources = len(source_list)
    _progress({
        "phase": "starting",
        "message": f"Starting scrape of {total_sources} feed(s)",
        "done": 0,
        "total": total_sources,
        "quote_count": 0,
    })

    for index, source in enumerate(source_list, start=1):
        _progress({
            "phase": "fetching",
            "message": f"Fetching {source.source_key} ({index}/{total_sources})",
            "source": source.source_key,
            "done": index - 1,
            "total": total_sources,
            "quote_count": len(all_quotes),
        })
        health, outcome = _collect_source(
            source,
            raw_store=raw_store,
            store=store,
            run_id=run_id,
            tier=tier,
            capture_id=capture_id,
        )
        health_reports.append(health)
        all_quotes.extend(outcome.quotes)
        claimed.update(_declared_markets(source, tier))

        declared = configured_leagues(source)
        if not declared:
            undeclared.append(source.source_key)
        per_league = league_coverage(source.source_key, outcome.quotes, declared)
        coverage.extend(per_league)

        if store is not None and run_id is not None:
            store.save_health(run_id, health)
            store.save_rejections(run_id, outcome.rejections)
            store.save_skipped(run_id, source.source_key, dict(outcome.skipped))
            store.save_repaired(run_id, source.source_key, dict(outcome.repaired))
            store.save_league_coverage(
                run_id,
                source.source_key,
                [(e.league, e.sport, e.quote_count, e.event_count) for e in per_league],
            )
        flag = "ok" if health.ok else "failed"
        _progress({
            "phase": "fetched",
            "message": (
                f"{source.source_key} {flag} — "
                f"{len(outcome.quotes):,} prices ({index}/{total_sources})"
            ),
            "source": source.source_key,
            "source_ok": health.ok,
            "done": index,
            "total": total_sources,
            "quote_count": len(all_quotes),
        })

    # Event identity is settled across all sources at once, before anything is
    # validated or compared. Each adapter can only number doubleheaders over its
    # own slate, which makes "#2" a per-source ordinal rather than an identity;
    # left uncorrected, one book's game 2 joins onto another book's game 1.
    _progress({
        "phase": "reconciling",
        "message": f"Reconciling {len(all_quotes):,} prices across books",
        "done": total_sources,
        "total": total_sources,
        "quote_count": len(all_quotes),
    })
    all_quotes, rekeys = reconcile_event_keys(all_quotes)

    # Measured on **everything collected**, before any scope filter, and passed
    # explicitly from here on.
    #
    # ``find_opportunities`` measures the gate from the rows it is handed, so a
    # narrowed call measures it from narrowed evidence — and narrowing can only
    # ever weaken it.  Two Kambi tenants that are one counterparty across 30 ATP
    # moneylines share 18 WTA selections, which is under
    # ``MIN_SHARED_SELECTIONS``: ``arb --league WTA`` therefore measured
    # UNDECIDED, opened the gate, and reported a position with both legs at one
    # book and no diagnostic at all.  This is what ``counterparty_groups``' own
    # docstring calls the most expensive defect this gate has had — round 16
    # fixed where a mirror is *filed* and left where it is *measured*.
    # Measured once and shared: ``counterparty_groups`` and the filing check
    # below both need the same mirror list, and each used to call
    # ``find_mirrors`` on its own — a second full pairwise scan of the slate.
    # ...and measured under the **run's** view-only set, not the process's.
    # ``collect_batch_once`` collects the detected state and ODDS_STATE's in
    # one process, so a box detected in IL with ODDS_STATE=PA was measuring
    # the IL pass's mirrors with PA's ambient set while ``find_opportunities``
    # admitted legs under the IL set — with alerts on.
    measured_mirrors = find_mirrors(
        all_quotes, view_only=registry.view_only_for_run(run_state)
    )
    measured_counterparties = counterparty_groups(all_quotes, mirrors=measured_mirrors)
    # Kept for the distinctness *filing* below, for the same reason the gate is
    # measured here: the evidence does not stop existing because this command
    # was asked about one league.  ``_check_distinctness`` ran on the filtered
    # rows, so ``collect --sport tennis`` over two sources mirrored on 24 MLB
    # selections stayed green — the gate still blocked positions, but the
    # finding that tells a person to remove one of the two from the registry
    # was hidden on exactly the narrowed runs an operator uses day to day.
    unfiltered_quotes = all_quotes

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

    _progress({
        "phase": "validating",
        "message": f"Validating {len(all_quotes):,} prices",
        "done": total_sources,
        "total": total_sources,
        "quote_count": len(all_quotes),
    })
    report = validate(
        all_quotes,
        capabilities=claimed,
        order_book_sources=_order_book_sources(sources),
    )
    _check_source_health(
        health_reports,
        report,
        configured=[source.source_key for source in sources],
        expected=_sources_covering(coverage, sports, leagues),
        store=store,
        run_id=run_id,
        narrowed=bool(sports or leagues),
        view_only=registry.view_only_for_run(run_state),
    )
    if route_scope == "state":
        native_producing = {
            health.source_key
            for health in health_reports
            if health.ok and health.quote_count and health.source_key in state_source_keys
        }
        if len(native_producing) < MIN_HEALTHY_SOURCES:
            report.add(
                Severity.ERROR,
                "state_native_sources_below_two",
                f"{run_state}: only {len(native_producing)} exact-state first-party "
                f"source(s) produced ({', '.join(sorted(native_producing)) or 'none'}); "
                "global venues cannot make a state slate healthy by themselves",
            )
        # Every required book either fetched first-party or watched by two
        # agreeing republishers.  Measured on ``unfiltered_quotes``: a run
        # narrowed to one sport still collected the whole board for the books it
        # asked, and judging coverage on the filtered rows would report a book as
        # unobserved because the operator asked for tennis.
        check_book_coverage(
            unfiltered_quotes,
            run_state,
            report,
            configured=[source.source_key for source in sources],
        )

    sports_seen = sport_coverage(
        all_quotes, coverage, view_only=registry.view_only_for_run(run_state)
    )
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
    _check_distinctness(
        unfiltered_quotes,
        report,
        mirrors=measured_mirrors,
        state=None if run_state == "GLOBAL" else run_state,
        configured=[source.source_key for source in sources],
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
    # The mirror gate is measured inside ``find_opportunities`` from these same
    # rows, so the live verdict and a later re-analysis of the stored rows agree
    # without either caller having to remember it.
    _progress({
        "phase": "arb",
        "message": "Scanning for arbitrage",
        "done": total_sources,
        "total": total_sources,
        "quote_count": len(all_quotes),
    })
    arb_report = find_opportunities(
        all_quotes,
        as_of=as_of or datetime.now(UTC),
        one_counterparty=measured_counterparties,
        view_only_sources=registry.view_only_for_run(run_state),
    )
    # Unconditional, because ``locality_marking`` owns the decision: gated here
    # on ``state_source_keys`` its predecessor read *what was built* and skipped
    # entirely for a run that built no PA book — the run that then reported, and
    # texted, a "PA" arbitrage with no PA leg.  Gated on ``route_scope`` it
    # disagreed with the dashboard about a ``legacy``-scope PA run, and its
    # ``!= "GLOBAL"`` test let an unrecognised jurisdiction through to a
    # ``KeyError`` that aborted the pass.  Licence, not inventory, and one
    # condition in one place.  Positions are kept and labelled, not withheld.
    marking = locality_marking(run_state, route_scope=route_scope)
    flagged = marking.count_without_local_leg(arb_report.opportunities)
    if flagged:
        report.add(
            Severity.WARNING,
            "non_local_positions_flagged",
            f"{flagged} position(s) have no leg at a venue reachable from "
            f"{run_state}; shown and labelled as non-local — every leg is a book "
            f"with no {run_state} licence and no nationwide US access, so nothing "
            f"in them can be staked from {run_state}",
        )

    if store is not None and run_id is not None:
        _progress({
            "phase": "saving",
            "message": f"Saving {len(all_quotes):,} prices",
            "done": total_sources,
            "total": total_sources,
            "quote_count": len(all_quotes),
        })
        # One unusable run must not end an unattended watch loop, and it must
        # not be recorded as though nothing happened either.
        #
        # Persisted per source, so one adapter emitting a duplicate costs that
        # adapter's rows rather than the whole run's.  With three books the
        # difference was a bad day; with ten it is the difference between a
        # reported fault and a lost slate.
        try:
            _, failures = store.save_quotes_by_source(run_id, all_quotes)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to persist quotes for run %s", run_id)
            report.add(
                Severity.ERROR,
                "quotes_not_persisted",
                f"{type(exc).__name__}: {exc} — the run's rows violate a storage "
                "invariant, most likely two prices for one selection",
            )
        else:
            for source_key, reason in sorted(failures.items()):
                report.add(
                    Severity.ERROR,
                    "quotes_not_persisted",
                    f"{reason} — {source_key}'s rows violate a storage invariant, most "
                    "likely two prices for one selection; every other source's rows "
                    "were stored",
                    source=source_key,
                )
        store.save_findings(run_id, report.findings)
        store.finish_run(
            run_id,
            finished_at=datetime.now(UTC),
            report=report,
            note=_run_note(sports_seen, sports, leagues, excluded, tier),
            excluded=excluded,
            counterparties=measured_counterparties,
        )

    # Fail-soft: missing Twilio credentials skip; a Twilio error must not
    # abort an otherwise good collect (especially ``--watch``).
    if alert and arb_report.opportunities:
        notified = notify_opportunities(arb_report.opportunities, marking=marking)
        if notified:
            log.info("texted %d arb(s) at >= %.1f%% ROI", len(notified), settings.ALERT_MIN_ROI * 100.0)

    return RunResult(
        run_id,
        all_quotes,
        health_reports,
        report,
        arb_report,
        coverage=sports_seen,
        league_coverage=coverage,
        excluded_by_filter=excluded,
        marking=marking,
    )


def _check_distinctness(
    quotes: Sequence[Quote],
    report: ValidationReport,
    *,
    mirrors: Sequence[Agreement] | None = None,
    state: str | None = None,
    configured: Collection[str] | None = None,
) -> list[Agreement]:
    """Refuse a run in which two registered sources are one counterparty.

    The gate exists because a mirror is invisible to every other check here: it
    satisfies the source contract, emits valid rows, raises the cross-source
    coverage counts, and clears ``require_distinct_sources`` in :mod:`src.arb` —
    which compares source *keys*, and two keys is exactly what a mirror has.
    What it does not have is two counterparties, so an "arbitrage" between the
    two is a position nobody can hold, sitting at the top of the report and
    indistinguishable from a real one.

    It used to be measured and then not consulted: :mod:`src.distinctness` had no
    caller outside the tests, so the registry could gain a mirror and nothing
    would say so.  Measuring without acting is the same as not measuring.

    Graded an **error**, because the consequence is a false position rather than
    a reporting inaccuracy — and the remedy is a person removing one of the two
    from the registry, which needs the run to stop being green.  "Not enough
    shared selections to tell" is deliberately not an error: that is a thin
    slate, not a mirror, and failing on it would make every out-of-season sport
    unaddable.

    *mirrors* is the already-measured list when the caller has one — collect
    measures once for the gate and the filing check.
    """
    mirrors = mirrors if mirrors is not None else find_mirrors(quotes)
    for pair in mirrors:
        # Intentional Action Network failover pairs are supposed to agree.
        # :func:`check_redundancy` flags drift and outages for those; treating
        # them as a registration error would delete the durability path.
        if is_redundant_pair(pair.source_a, pair.source_b):
            continue
        whole_book = pair.rate >= MIRROR_AGREEMENT_RATE
        if whole_book:
            report.add(
                Severity.ERROR,
                "sources_are_one_counterparty",
                f"{pair.summary()}. Two licences of one book, not two books: a position "
                "across them cannot be held, and nothing else in the pipeline can see "
                "the difference. Remove one from src.sources.registry and record it in "
                "docs/SOURCE_FEASIBILITY.md with the source it mirrors",
                source=pair.source_b,
            )
            continue
        # A partial mirror is a different problem with a different remedy.  These
        # two disagree about most sports and are one feed in some, so removing
        # either would throw away real coverage that the other does not have.
        # What has to stop is comparing them *where* they are one feed, which is
        # done rather than asked for: the leagues are handed to the detector as
        # one counterparty and no position can span them there.
        report.add(
            Severity.WARNING,
            "sources_are_one_counterparty_in_some_leagues",
            f"{pair.summary()}. They are not one book everywhere — outside those "
            "competitions they disagree often enough to be two — so both are kept "
            "and they are treated as one counterparty in the leagues named, where "
            "no position between them is reported",
            source=pair.source_b,
        )
    check_redundancy(quotes, report, state=state, configured=configured)
    return mirrors


def _order_book_sources(sources: Sequence[OddsSource]) -> set[str]:
    """Sources whose rows exist only where somebody offered liquidity.

    Exchanges and prediction markets.  Two validation checks mean something
    different there — a missing leg is an empty book rather than a parser fault —
    and the registry is where that fact lives, so it is read off the registry
    rather than guessed from the data.  A source the registry does not know (a
    test's fake, say) is treated as a sportsbook, which is the stricter reading.
    """
    order_driven: set[str] = set()
    for source in sources:
        entry = registry.BY_KEY.get(source.source_key)
        if entry is not None and entry.kind.has_stated_liquidity:
            order_driven.add(source.source_key)
    return order_driven


def _declared_markets(
    source: OddsSource, tier: Tier
) -> dict[tuple[str, str], frozenset[Market]]:
    """What one source says it prices, keyed ``(source, league)``.

    An adapter that publishes nothing contributes nothing, and validation then
    falls back to expecting the sport's full core market set of it — which is the
    old behaviour and the safe direction to fail in.
    """
    declare = getattr(source, "capabilities", None)
    if declare is None:
        return {}
    # Whether the tier is accepted is decided by *inspecting* the signature, not
    # by calling and catching TypeError.  Catching it swallowed any TypeError
    # raised inside the method's own body, called it a second time, and let the
    # identical error escape on the retry — turning one adapter's bug into the
    # end of the run rather than into that adapter's problem.
    try:
        takes_tier = "tier" in signature(declare).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        takes_tier = False
    claims = declare(tier=tier) if takes_tier else declare()
    return {
        (source.source_key, league): frozenset(markets)
        for league, markets in dict(claims).items()
    }


def _run_note(
    coverage: Sequence[SportCoverage],
    sports: Sequence[str] | None,
    leagues: Sequence[str] | None,
    excluded: int,
    tier: Tier = Tier.FULL,
) -> str:
    """One line stored with the run, so its scope is recoverable later."""
    parts = [f"tier={tier.value}"]
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
    tier: Tier = Tier.FULL,
    capture_id: str = "",
) -> tuple[SourceHealth, ParseOutcome]:
    checked_at = datetime.now(UTC)
    started = time.perf_counter()
    raws: list[RawResponse] = []

    try:
        raws = _fetch(source, tier)
        latency_ms = (time.perf_counter() - started) * 1000
    except SourceError as exc:
        # Keep the bytes that explain the failure.  A block page whose markers
        # changed, or a JSON error envelope nobody has seen before, is
        # unreadable from a log line and obvious from the payload — and it is
        # the one response most worth having when the same failure recurs.
        refusal = getattr(exc, "raw", None)
        if refusal is not None:
            # Isolated like every other raw write.  This call sat outside the
            # per-source isolation, and the two failures it joins are
            # *correlated*: a venue blocking you is exactly when a refusal
            # payload exists, and an unwritable raw directory makes every
            # source take some write path — so a blocked source plus a bad
            # directory aborted the whole pass with no health rows, an
            # unfinished run wearing the Ctrl-C signature, and the healthy
            # sources' fetches discarded.  Losing the *capture* of a refusal
            # must never outrank recording the refusal itself.
            try:
                refusal, _ = _persist_raw(
                    refusal,
                    raw_store=raw_store,
                    store=store,
                    run_id=run_id,
                    capture_id=capture_id,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to persist %s's refusal payload; the refusal itself "
                    "is still recorded",
                    source.source_key,
                )
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                request_count=1 if refusal is not None else 0,
                raw_bytes=refusal.byte_size if refusal is not None else 0,
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

    # Scopes this source asked for and was refused, on a pass it survived.  The
    # adapters log these and the tally counts them; nothing read them, so a book
    # that lost a league to a 429 still summarised as OK.
    tally = getattr(source, "last_fetch", None)
    failed_scopes = tuple(getattr(tally, "failed_scopes", ()))
    truncated_scopes = tuple(getattr(tally, "truncated_scopes", ()))
    scopes_requested = int(getattr(tally, "scopes_requested", 0))
    # Distinct scopes refused, not messages logged — see ``SourceHealth``.
    scopes_failed = int(getattr(tally, "scopes_failed", len(failed_scopes)))

    # Raw bytes land on disk before anything interprets them.
    #
    # Wrapped per source like the fetch and the parse.  This was the one stage
    # per-source isolation did not cover: an unwritable raw directory on the
    # *first* source aborted the whole pass, discarding three healthy sources'
    # fetches and leaving an unfinished run that ``runs`` labels "interrupted
    # mid-pass" — the mark of an operator's Ctrl-C, not a crash.  A failed
    # write is that source's failure, recorded as one; when the cause is the
    # directory itself, every source fails the same way and the run finishes
    # with ten explicit failures instead of vanishing half-done.
    unchanged_payloads = 0
    try:
        for index, raw in enumerate(raws):
            # The stamped row replaces the unstamped one *in the list the parser
            # is about to be handed*, so collection and replay read the same
            # bytes with the same provenance.
            raws[index], unchanged = _persist_raw(
                raw,
                raw_store=raw_store,
                store=store,
                run_id=run_id,
                capture_id=capture_id,
            )
            unchanged_payloads += int(unchanged)
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to persist %s's raw bytes", source.source_key)
        return (
            SourceHealth(
                source_key=source.source_key,
                ok=False,
                checked_at=checked_at,
                request_count=len(raws),
                raw_bytes=sum(raw.byte_size for raw in raws),
                latency_ms=latency_ms,
                # The adapter already knew which scopes it lost; dropping that
                # here left a parse/write failure looking like a source that had
                # never been asked for anything.
                scopes_requested=scopes_requested,
                scopes_failed=scopes_failed,
                truncated_scopes=truncated_scopes,
                failed_scopes=failed_scopes,
                error_kind="raw_write_failed",
                error_message=f"{type(exc).__name__}: {exc}",
            ),
            ParseOutcome(),
        )

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
                scopes_requested=scopes_requested,
                scopes_failed=scopes_failed,
                truncated_scopes=truncated_scopes,
                failed_scopes=failed_scopes,
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
            repaired_count=sum(outcome.repaired.values()),
            unchanged_payloads=unchanged_payloads,
            error_kind=error_kind,
            error_message=error_message,
            scopes_requested=scopes_requested,
            scopes_failed=scopes_failed,
            truncated_scopes=truncated_scopes,
            failed_scopes=failed_scopes,
        ),
        outcome,
    )


def _persist_raw(
    raw: RawResponse,
    *,
    raw_store: RawStore,
    store: Store | None,
    run_id: int | None,
    capture_id: str = "",
) -> tuple[RawResponse, bool]:
    """Write one captured response to disk.  Returns the stamped row and "unchanged".

    Stamping :attr:`~src.raw_store.RawResponse.capture_id` happens here because
    this is the only layer that knows where one collection pass ends: adapters
    are built once and reused across every pass of a watch loop, so an id held on
    the adapter or its HTTP client would be the same for all of them.

    The stamped response is *returned* rather than only written, because
    ``RawResponse`` is frozen and rebinding it here would leave the caller's list
    — the one handed to ``parse`` — unstamped.  That split matters:
    :func:`~src.sources._common.latest_capture` reads the stamp when there is one
    and falls back to a timestamp window when there is not, so collection and
    replay would take different branches over the same bytes.  For a pass longer
    than that window they disagree, and for Smarkets they disagree by dropping
    the ``events`` response, which makes the parser raise at collection and
    succeed on replay.
    """
    if capture_id and not raw.capture_id:
        raw = replace(raw, capture_id=capture_id)
    path = raw_store.write(raw)
    if store is None or run_id is None:
        return raw, False
    previous = store.previous_sha(raw.source, raw.endpoint, before_run_id=run_id)
    unchanged = previous is not None and previous == raw.sha256
    store.record_raw(run_id, raw, path, unchanged=unchanged)
    return raw, unchanged


def _fetch(source: OddsSource, tier: Tier) -> list[RawResponse]:
    """Ask a source for its responses, tolerating one that predates tiering.

    A source injected by a test — or written before the tier argument existed —
    takes no keyword, and refusing to call it would turn a compatible adapter
    into a failed one.  Decided by *inspecting* the signature rather than by
    calling and catching ``TypeError``: catching it would swallow a real
    ``TypeError`` from inside the adapter's own fetch and silently retry the
    whole network pass.
    """
    try:
        takes_tier = "tier" in signature(source.fetch_raw).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        takes_tier = False
    # Copied into a list this function owns: the caller stamps each response in
    # place, and an adapter that handed back a tuple — or its own cached list —
    # must not have that done to it.
    return list(source.fetch_raw(tier=tier) if takes_tier else source.fetch_raw())


def _sources_covering(
    coverage: Sequence[LeagueCoverage],
    sports: Sequence[str] | None,
    leagues: Sequence[str] | None,
) -> set[str]:
    """Sources that *declared* they cover the requested scope.

    The denominator for "how much of the pipeline answered".  Without it a
    narrowed run had no collapse check at all: the exemption below was blanket,
    on the reasoning that several adapters accept a league they have no path
    for — true, and it also meant ``collect --sport baseball`` could lose eight
    of its ten books and exit 0, on a slate where all ten do serve baseball.

    Declared, not produced, so a source going silent still counts against it.
    """
    if not sports and not leagues:
        return {entry.source_key for entry in coverage}
    return {
        entry.source_key
        for entry in coverage
        if (not sports or entry.sport in sports) and (not leagues or entry.league in leagues)
    }


def _check_source_health(
    health: Sequence[SourceHealth],
    report: ValidationReport,
    *,
    configured: Sequence[str],
    expected: set[str] | None = None,
    store: Store | None = None,
    run_id: int | None = None,
    narrowed: bool = False,
    view_only: frozenset[str] | None = None,
) -> None:
    """Judge the run against what was asked for, not against a fixed floor.

    ``MIN_HEALTHY_SOURCES`` is a *comparability* bar: below two counterparties
    nothing can be compared at all, so that stays an error.  But it is a floor,
    not a verdict.  "Two of thirty sources answered" clears it while being a
    catastrophic run, and the shape of that failure — most books blocked, two
    still working — is exactly what a growing source list makes likely.

    So two further things are checked, each answering a question the floor
    cannot:

    * **Against the configuration.**  Sources that were asked and produced
      nothing are named, because a silent shortfall is indistinguishable from a
      thin slate.
    * **Against this source's own past.**  A book that produced rows in a recent
      run and produces none now has *regressed*, which is a different fact from
      one that has never worked, and it is the one an unattended watch loop has
      to shout about.  Graded an error for that reason: the row count alone would
      stay plausible while a feed quietly died.
    """
    # View-only feeds do not make a slate comparable — AN Open alone beside one
    # real book must not clear the two-counterparty floor.  The *run's* set,
    # like every other consumer of the distinction.  The pre-fix ambient read
    # was reachable, not theoretical: ``hardrock`` is built into **IL** passes
    # (route VALIDATED, real runs carry its rows), so a batch box with
    # ODDS_STATE=PA collecting the IL pass graded IL's slate under PA's set —
    # a false ``insufficient_sources`` over a healthy two-book slate.
    if view_only is None:
        from src.sources.registry import VIEW_ONLY_SOURCES

        view_only = VIEW_ONLY_SOURCES
    producing = [
        h.source_key
        for h in health
        if h.quote_count > 0 and h.source_key not in view_only
    ]
    silent = [
        h.source_key
        for h in health
        if h.quote_count == 0 and h.source_key not in view_only
    ]

    if len(producing) < MIN_HEALTHY_SOURCES:
        report.add(
            Severity.ERROR,
            "insufficient_sources",
            f"only {len(producing)} source(s) produced data ({producing or 'none'}); "
            f"at least {MIN_HEALTHY_SOURCES} are required for anything to be compared",
        )

    # A slate with no sportsbook in it is a different kind of slate, and it was
    # indistinguishable from a healthy one.  The floor above counts *venues*, so
    # two exchanges clear it: every sportsbook could be geo-blocked or have
    # changed format and an unattended watch loop still reported ``ok`` and
    # exited 0, with exchanges rendered as "book(s)" in the coverage summary.
    #
    # A warning, not an error: two exchanges genuinely can be compared with each
    # other, and refusing that would throw away a usable slate.  What was wrong
    # was the silence — and the claim, in this module's own header and in the
    # README, that ``ok`` means "at least two *sportsbooks* produced data".  It
    # never meant that; now the run says which it is.
    books = [
        h.source_key
        for h in health
        if h.quote_count > 0
        and (entry := registry.BY_KEY.get(h.source_key)) is not None
        and not entry.kind.has_stated_liquidity
    ]
    if producing and not books:
        report.add(
            Severity.WARNING,
            "no_sportsbook_produced",
            f"every venue that produced data is an exchange or prediction market "
            f"({', '.join(sorted(producing))}) — no sportsbook did. Prices can "
            "still be compared, but a book's posted price and an order book's "
            "resting offer are different things: the sizes, the commission and "
            "what happens to a cancelled game all differ",
        )

    if silent and len(configured) > len(producing):
        # Graded on the *share* that answered, not on the absolute count.  Two of
        # thirty sources answering clears ``MIN_HEALTHY_SOURCES`` and used to
        # pass: on a fresh database with eight of ten geo-blocked — every
        # sportsbook among them — the run reported zero errors and exited 0,
        # because ``source_stopped_producing`` needs a history it does not have
        # and two is still "enough to compare".  This function's own docstring
        # calls that "a catastrophic run"; it is now graded as one.
        # Graded against the sources that *declared* they cover this scope, not
        # against everything configured.  A run restricted by --sport/--league
        # leaves sources silent for a good reason — several adapters accept a
        # league they have no path for — but exempting such a run wholesale left
        # it with no collapse check at all, and ``collect --sport baseball`` can
        # lose eight of the ten books that do serve baseball.
        # The sources that declared they cover this scope, and nothing else.
        #
        # An earlier version added back every source missing from ``coverage``,
        # to stop one that declares no leagues vanishing from the denominator.
        # That was a no-op where it was meant to help — an unnarrowed run already
        # counts everything configured, and ``league_coverage`` emits an entry
        # per configured league even at zero quotes — and actively wrong where it
        # fired: ``coverage`` is scope-filtered, so on ``--league TENNIS_OTHER``
        # it re-added the seven sources that never claimed tennis and failed a
        # run whose three relevant sources had all answered.
        relevant = set(expected) & set(configured) if expected else set(configured)
        answered = [key for key in producing if key in relevant] if relevant else producing
        denominator = len(relevant) or len(configured)
        share = len(answered) / denominator
        collapsed = share < MIN_PRODUCING_SHARE
        report.add(
            Severity.ERROR if collapsed else Severity.WARNING,
            "configured_sources_produced_nothing",
            f"{len(producing)} of {len(configured)} configured source(s) produced rows; "
            f"silent: {', '.join(sorted(silent))} — a shortfall against what was asked "
            "for, which a row count alone cannot show"
            + (
                f"; that is {share * 100:.0f}% of the venues answering, which is a "
                "broken pipeline rather than a thin slate, whether or not any two of "
                "them can still be compared"
                if collapsed
                else ""
            ),
        )

    # A scope this source asked for and was refused, on a run it survived.
    #
    # ``ScopeTally`` counted these and only ``require_something`` ever read the
    # count — and only when *every* scope came back empty, so one surviving
    # league discarded every refusal.  A book answering 429 on one league lost
    # 14% of its rows and summarised as ``OK (701 quotes, 12 requests)``, with no
    # finding, no error, and exit 0.  Graded a warning rather than an error: the
    # book is still usable and the rest of its slate is real, but the shortfall
    # has to be visible to somebody deciding whether to act on the run.
    for entry in health:
        if not entry.failed_scopes:
            continue
        # Graded on the share lost, as ``configured_sources_produced_nothing``
        # is: one league of nine is a bad afternoon and eight of nine is a
        # broken feed, and a flat warning called them the same thing — a book
        # that lost 89% of its scopes read as PASS, exit 0, one line among a
        # hundred warnings.
        # Graded on the share of scopes lost *and* on the source going quiet.
        #
        # Scope count alone is the wrong weight: Pinnacle's soccer is 91% of its
        # rows and one of its six scopes, so losing it entirely graded a warning
        # while losing four idle ones graded an error.  There is no honest way to
        # weight a scope that returned nothing — its size is exactly what was not
        # collected — so the second clause asks the question that can be
        # answered: did what survived amount to a slate?
        # Counted in **distinct scopes**, both sides.  ``failed_scopes`` is a
        # list of messages and one scope can raise twice — SX Bet's metadata
        # half and its order-book half are one scope and two requests — which
        # put the numerator above the denominator and printed "was refused 2 of
        # the 1 scope(s) it asked for".
        lost = entry.scopes_failed or len(entry.failed_scopes)
        asked = max(entry.scopes_requested, lost)
        share_lost = lost / max(asked, 1)
        collapsed = share_lost > (1 - MIN_PRODUCING_SHARE) or not entry.event_count
        report.add(
            Severity.ERROR if collapsed else Severity.WARNING,
            "scopes_refused",
            f"{entry.source_key} was refused {lost} of the {asked} "
            "scope(s) it asked for"
            # "and returned the rest" was said unconditionally, including on the
            # runs where there was no rest: a source whose every scope was
            # refused read as one that had mostly worked.  The denominator is
            # named for the same reason — "refused 2 scopes" and "refused 2 of
            # 2" are different runs.
            + (
                " and returned none of the rest"
                if lost >= asked
                else " and returned the rest"
            )
            + f": {'; '.join(entry.failed_scopes[:3])}"
            + (" …" if len(entry.failed_scopes) > 3 else "")
            + (
                " — most of what this source was asked for, which is a broken feed "
                "rather than a quiet league"
                if collapsed
                else ""
            ),
            source=entry.source_key,
        )

    # A scope that answered and then stopped short of its whole slate.
    #
    # Reported separately from a refusal, and never folded into the share above.
    # Filing a cap as a refusal said something false in the direction that
    # matters: Polymarket handing over 41 MLB events and stopping at its own
    # two-page cap was reported as "refused 1 of the 1 scope(s) it asked for and
    # returned none of the rest", graded ERROR, and failed a run that had
    # collected 2,393 rows from ten venues.  A cap leaves behind a fraction of
    # one scope, of unknown size; "one scope of one was lost" is not a reading
    # the evidence supports.
    #
    # A warning, not an error, and not gradable on any share: the count of
    # scopes cannot express how much of one scope is missing.  What it can do is
    # name it, so that an operator deciding whether to act on the run knows the
    # slate is short and where.
    for entry in health:
        if not entry.truncated_scopes:
            continue
        report.add(
            Severity.WARNING,
            "scopes_truncated",
            f"{entry.source_key} stopped short on {len(entry.truncated_scopes)} "
            f"scope(s) it did collect, so their slates are incomplete: "
            + "; ".join(entry.truncated_scopes[:3])
            + (" …" if len(entry.truncated_scopes) > 3 else ""),
            source=entry.source_key,
        )

    # A source that rejected rows is unhealthy, and only ``SourceHealth`` knew.
    # ``RunResult.ok`` reads the validation report alone, so a book that refused
    # 5,000 in-scope markets produced a report byte-identical to a clean run.
    for entry in health:
        if entry.ok or not entry.error_kind:
            continue
        report.add(
            Severity.WARNING if entry.quote_count else Severity.ERROR,
            f"source_unhealthy:{entry.error_kind}",
            f"{entry.source_key} reported {entry.error_kind}: {entry.error_message}"
            + (
                f" — it still stored {entry.quote_count:,} rows, so the run is usable "
                "without it being right"
                if entry.quote_count
                else " and stored nothing"
            ),
            source=entry.source_key,
        )

    if store is None or run_id is None or not silent:
        return
    if narrowed:
        # A run restricted to one sport or league is *expected* to produce
        # nothing from the sources that cannot serve it, and several adapters
        # accept a league they have no path for.  Comparing that against a run
        # that asked for everything manufactures the alarm rather than raising
        # it, so the check simply does not apply here — and says so, rather than
        # firing and being learned to ignore.
        report.add(
            Severity.WARNING,
            "regression_check_skipped_for_a_narrowed_run",
            "this run was restricted by --sport/--league, so a source producing "
            "nothing is not compared against its history: the comparison would be "
            "against runs that asked for more",
        )
        return
    try:
        previously = store.sources_that_have_produced(run_id)
    except Exception:  # noqa: BLE001 - history is a nicety; the run is not
        log.exception("could not read source history")
        return
    regressed = sorted(set(silent) & previously)
    for source_key in regressed:
        report.add(
            Severity.ERROR,
            "source_stopped_producing",
            f"{source_key} has produced rows before and produced none now — a feed "
            "that worked and has stopped, which is a defect rather than a thin slate",
            source=source_key,
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
            view_only_count = len(entry.sources) - len(entry.counterparty_sources)
            also = (
                f"; {view_only_count} view-only feed(s) priced it too, but a "
                "mirror is not a counterparty"
                if view_only_count
                else ""
            )
            report.add(
                Severity.WARNING,
                "sport_below_two_books",
                f"{entry.sport}: only {len(entry.counterparty_sources)} counterparty "
                f"book(s) ({', '.join(entry.counterparty_sources) or 'none'}) priced "
                f"it{also}, so none of its {entry.quote_count} rows can be compared "
                "across books",
            )
        elif not entry.cross_book_events:
            report.add(
                Severity.WARNING,
                "sport_without_cross_book_fixtures",
                f"{entry.sport}: {len(entry.counterparty_sources)} counterparty books "
                f"priced it but they have no fixture in common across "
                f"{entry.event_count} fixtures, so none of its "
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
    # The run's **own recorded scope** is applied to the replayed side whatever
    # the command asks for.  A scope-collected run stores fewer rows than its
    # raws parse *by design* — the filter's drops are counted, not lost — and
    # replaying it unscoped reported those drops as corruption: ``row count
    # differs: stored 2476, replayed 5429 / replay invented row`` on a run whose
    # own note says "2953 rows excluded by filter", with the dashboard masthead
    # reading "replay FAIL".  The README's quick start (``collect --sport
    # hockey`` then ``replay``) walked straight into it.
    run_sports, run_leagues, _ = store.run_scope(run_id)
    stored = store.load_quotes(run_id, sports=sports, leagues=leagues)
    # A run stamped by ``migrate`` was collected under an older schema, and the
    # parser has legitimately changed since — a difference on such a run can be
    # evolution rather than corruption, and the verdict has to say which kind of
    # claim it is making.  The stamp is the only thing that can tell them apart:
    # the sha check still catches changed bytes either way.  Applied to **every**
    # failing return, including "no stored raw responses" — v3 predates raw
    # capture for some runs, which is itself evolution rather than loss.
    run_row = store.run_row(run_id)
    migrated_from = run_row["migrated_from"] if run_row is not None else None

    def _judged(problems: list[str]) -> tuple[bool, list[str]]:
        if problems and migrated_from is not None:
            problems.insert(
                0,
                f"run {run_id} was collected under schema v{migrated_from} and "
                "migrated: the parser has changed since, so the differences below "
                "can be parser evolution rather than corruption — the stored rows "
                "are still what was collected, and a sha mismatch (none unless "
                "named below) is the only sign of changed bytes",
            )
        elif (
            problems
            and not any("sha256" in problem for problem in problems)
            and any(
                marker in problem
                for problem in problems
                for marker in (
                    "row count differs",
                    "replay lost row",
                    "replay invented row",
                    "changed on replay",
                )
            )
        ):
            # The migration stamp was the only channel that admitted the parser
            # legitimately changes, so a deliberate change convicted every
            # v4-native run it touched with corruption-shaped output — the
            # 2026-08-09 mid-move guard re-parses older line-tracker captures
            # to fewer rows *by design*, and an adversarial round found runs
            # whose replay read "row count differs / replay lost row" with
            # nothing saying why.  Every stored byte carrying a recorded
            # sha256 verified (a mismatch raises out of RawStore.read and is
            # named above), so the differences below are the current parser
            # disagreeing with the one that stored the run.  The verdict stays
            # FAIL because replay cannot tell evolution from regression — but
            # it now states which two things the reader must tell apart.
            problems.insert(
                0,
                f"run {run_id}'s stored bytes verified against their recorded "
                "sha256s, so the differences below are the current parser "
                "disagreeing with the parser that stored this run. A deliberate "
                "parser change looks exactly like this (deliberate ones are "
                "recorded in docs/SOURCE_FEASIBILITY.md); corrupted bytes would "
                "be named as a sha256 mismatch instead",
            )
        return not problems, problems
    by_source_paths: dict[str, list[Path]] = {}
    for source_key, path in store.raw_paths(run_id):
        by_source_paths.setdefault(source_key, []).append(path)

    if not by_source_paths:
        return _judged([f"run {run_id} has no stored raw responses"])

    # Which sources actually contributed rows.  A source whose *fetch* failed
    # still has bytes on disk — the refusal that explains the failure is
    # captured deliberately — and those bytes are not a slate: handing Pinnacle's
    # 503 maintenance page to its parser raises, correctly, because half a
    # matchups/markets pair cannot be joined.  Replaying it as though it were
    # data failed the whole replay every time a venue had a bad hour.
    #
    # So a parse that raises is a *problem* for a source that stored rows, and a
    # *note* for one that stored none: there, the raise is the parser saying the
    # bytes were never a slate, which is exactly what the health record already
    # says.  The invented-row case is still caught — a source that stored nothing
    # and now parses to something is a divergence, and it reaches the comparison
    # below rather than being skipped.
    #
    # "Stored rows" is the *stored* fact, so it is read off the quote table and
    # not off ``source_health.quote_count`` — the two diverge exactly when a
    # source's insert failed or a scope filter dropped everything it produced
    # (see ``Store.stored_quote_counts``), and in both divergent cases the
    # health number files the judgement on the wrong side: a problem for a
    # source with nothing stored, or a note for one whose stored rows are now
    # genuinely irreproducible.
    produced = {
        source for source, count in store.stored_quote_counts(run_id).items() if count
    }

    replayed: list[Quote] = []
    for source_key, paths in by_source_paths.items():
        try:
            # See ``replay_factory``: resolved from the run's jurisdiction, never
            # from ``settings.STATE``.
            stored_state = str(run_row["jurisdiction"] or "") if run_row else ""
            factory = replay_factory(stored_state, source_key)
        except (KeyError, RuntimeError):
            # Same judgement as a parse that raises, below: losing the ability to
            # re-parse bytes that produced no rows loses nothing, and stored runs
            # legitimately outlive registrations — ``an_circa``/``an_fliff``/
            # ``an_superbook`` left 180 raw responses and zero quotes behind when
            # they were deregistered on 2026-08-09.  A deregistered source that
            # *did* store rows still fails the replay, because those rows are now
            # genuinely irreproducible.
            message = f"{source_key}: no adapter available to replay this source"
            if source_key in produced:
                problems.append(message)
            else:
                log.info("%s (it stored no rows on this run, so nothing is lost)", message)
            continue
        source = factory()
        try:
            raws = [raw_store.read(path) for path in paths]
            # Unscoped on purpose — see the note above.  Scoping happens after
            # reconciliation, because clustering needs every source's view of a
            # fixture to land on the same key the collector stored.
            replayed.extend(source.parse(raws).quotes)
        except Exception as exc:  # noqa: BLE001
            message = f"{source_key}: replay raised {type(exc).__name__}: {exc}"
            if source_key in produced:
                problems.append(message)
            else:
                log.info("%s (it stored no rows on this run, so nothing is lost)", message)
        finally:
            source.close()

    replayed, _ = reconcile_event_keys(replayed)
    # Both scopes apply, as successive filters: what the run kept, then what the
    # command asked about.  A union would let a baseball row through when the
    # command asked for hockey on a baseball-scoped run — an invented row again,
    # from the fix for the last one.
    replayed = [
        quote
        for quote in replayed
        if in_scope(quote, run_sports or None, run_leagues or None)
        and in_scope(quote, sports, leagues)
    ]

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
        for field, was, now in _row_differences(before, after):
            problems.append(f"{field} changed on replay for {key}: {was!r} -> {now!r}")
        if len(problems) > 20:
            break

    return _judged(problems)


#: Prices are floats and have been through SQLite, so they are compared to a
#: tolerance rather than for identity.  Everything else must come back exactly.
REPLAY_FLOAT_TOLERANCE = 1e-9


def _row_differences(before: Quote, after: Quote) -> Iterator[tuple[str, Any, Any]]:
    """Every field on which a stored row and its re-parse disagree.

    **Every** field, not the two that used to be checked.  ``dedup_key`` covers
    identity — source, fixture, market, period, side, line, selection — so a
    comparison keyed on it and then checking only ``decimal_odds`` and
    ``observed_at`` was blind to everything else the parser produces.  Measured
    against the committed fixtures, a parser that rewrote one field per run
    still reported **PASS** while flipping ``status`` from ACTIVE to SUSPENDED
    on 3,293 rows — which is exactly what :func:`src.arb.find_opportunities`
    reads to decide whether a price is takeable — and while rewriting
    ``limit_amount`` on 2,346, which sets the bankroll a position claims it can
    be placed at.  Also silently reproducible: ``american_odds``,
    ``implied_probability``, ``home_team`` and ``raw_ref``, the last of which is
    the pointer back to the bytes a row came from.

    This is the pipeline's one guarantee that parsing is a pure function of the
    captured bytes.  It has to compare the whole row or it does not say that.
    """
    for name in type(before).model_fields:
        was, now = getattr(before, name), getattr(after, name)
        if isinstance(was, float) and isinstance(now, float):
            if abs(was - now) > REPLAY_FLOAT_TOLERANCE:
                yield name, was, now
        elif was != now:
            yield name, was, now


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


def collect_batch_once(
    states: Sequence[str],
    *,
    detected_state: str,
    raw_store: RawStore,
    store: Store | None,
    source_keys: Sequence[str] | None = None,
    sports: Sequence[str] | None = None,
    leagues: Sequence[str] | None = None,
    tier: Tier = Tier.FULL,
    on_progress: Callable[[Mapping[str, Any]], None] | None = None,
    alert: bool = True,
) -> BatchResult:
    """Fetch globals once, then combine them with each exact-state retail slate."""
    batch_id = uuid4().hex
    resolved_leagues = resolve_leagues(sports, leagues)
    # Each scope may legitimately contribute nothing — ``--source an_caesars``
    # names only a state-licensed republisher, which is no longer a global
    # source — so emptiness is judged on the batch below, not per scope.  Before
    # that, asking for one republisher died with "none of [] can collect
    # league(s) ['MLB']", blaming the league for a scope mismatch.
    globals_built = build_sources(
        source_keys,
        leagues=resolved_leagues,
        route_scope="global",
        allow_empty=True,
    )
    cached_globals = [
        CachedOddsSource(source, capture_id=batch_id) for source in globals_built
    ]
    runs: list[tuple[str, RunResult]] = []
    try:
        if cached_globals:
            global_result = collect_once(
                cached_globals,
                raw_store=raw_store,
                store=store,
                sports=sports,
                leagues=leagues,
                tier=tier,
                on_progress=on_progress,
                # The state passes are the alerting surfaces: their marking is
                # governed, so their texts carry the locality disclaimer.  The
                # GLOBAL pass sees the same cross-global positions minutes
                # earlier with an ungoverned marking — letting it text first
                # sends the bare body, and the dedupe key holds no
                # jurisdiction, so the labelled state-pass text is then
                # swallowed as a duplicate.  Only a batch with no state pass
                # at all keeps its alert here.
                alert=alert and not states,
                jurisdiction="GLOBAL",
                batch_id=batch_id,
                route_scope="global",
            )
            runs.append(("GLOBAL", global_result))

        for state in states:
            retail = build_sources(
                source_keys,
                leagues=resolved_leagues,
                state=state,
                route_scope="state",
                allow_empty=True,
            )
            state_keys = tuple(source.source_key for source in retail)
            # Built per state and *not* cached with the globals: a republisher's
            # book id is a state licence, so one shared instance can only have
            # asked for one state's book.  Sharing them stored Caesars NJ
            # (``bookIds=123``) under every Pennsylvania key.
            #
            # Inside its own guard because ``retail`` is already built and holds
            # open transports: a failure here used to leak all of them, and the
            # ``finally`` below only covers what the ``try`` after it can reach.
            try:
                republished = build_sources(
                    source_keys,
                    leagues=resolved_leagues,
                    state=state,
                    route_scope="state_republished",
                    allow_empty=True,
                )
            except BaseException:
                for source in retail:
                    source.close()
                raise
            # Deliberately excluded from ``state_keys``: that set feeds the
            # "how many exact-state first-party sources produced" gate, and a
            # republished mirror must not be able to satisfy it.
            combined: list[OddsSource] = [*retail, *republished, *cached_globals]
            if not combined:
                raise SystemExit(
                    f"none of {list(source_keys or ())} can collect league(s) "
                    f"{list(resolved_leagues or ())} in {state}"
                )
            try:
                result = collect_once(
                    combined,
                    raw_store=raw_store,
                    store=store,
                    sports=sports,
                    leagues=leagues,
                    tier=tier,
                    on_progress=on_progress,
                    alert=alert,
                    jurisdiction=state,
                    batch_id=batch_id,
                    route_scope="state",
                    state_source_keys=state_keys,
                )
                runs.append((state, result))
            finally:
                for source in (*retail, *republished):
                    source.close()
    finally:
        for source in cached_globals:
            source.close()
    return BatchResult(
        batch_id=batch_id,
        detected_state=detected_state,
        runs=tuple(runs),
    )


def _cmd_collect(args: argparse.Namespace) -> int:
    sports, leagues = _filter_args(args)
    raw_store = RawStore(settings.RAW_DIR)
    store = None if args.no_store else _open_store()
    tier = Tier(args.tier)
    from src.state_selection import StateSelectionError, detect_and_select

    try:
        selection = detect_and_select(args.state)
    except StateSelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if store is not None:
            store.close()
        return 2
    print(
        f"auto-state: detected {selection.detected.state} via {selection.provider}; "
        f"batch states: {', '.join(selection.states)}",
        flush=True,
    )
    exit_code = 0
    try:
        iteration = 0
        while True:
            iteration += 1
            try:
                batch = collect_batch_once(
                    selection.states,
                    detected_state=selection.detected.state,
                    raw_store=raw_store,
                    store=store,
                    source_keys=args.source,
                    sports=sports,
                    leagues=leagues,
                    tier=tier,
                    alert=not args.no_alert,
                )
            except Exception:  # noqa: BLE001
                # Unattended operation is a requirement, and only the quote
                # insert used to be protected: a locked database, a disk error
                # writing a raw capture, or a bug in one adapter's own
                # bookkeeping ended the loop entirely.  One failed pass is a
                # failed pass; the next one is five minutes away.
                log.exception("collection pass %d failed", iteration)
                exit_code = 1
                if not args.watch:
                    raise
                if args.max_runs and iteration >= args.max_runs:
                    break
                log.info("sleeping %ss before the next run", args.interval)
                time.sleep(args.interval)
                continue
            print(f"\n=== batch {batch.batch_id} ===")
            for state, result in batch.runs:
                print(f"\n--- {state} ---")
                result.print_summary()
            # **Sticky.**  ``exit_code = 0 if result.ok else 1`` let one good
            # pass overwrite an earlier failure, so a bounded batch — ``--watch
            # --max-runs 3`` in cron — exited 0 when pass 2 of 3 died with a
            # traceback and only the *final* pass's verdict survived.  An
            # endless watch never reaches the exit, so the only reader of this
            # value is exactly the bounded batch that was being lied to.
            if not batch.ok:
                exit_code = 1
            if not args.watch:
                break
            if args.max_runs and iteration >= args.max_runs:
                break
            log.info("sleeping %ss before next run", args.interval)
            time.sleep(args.interval)
    finally:
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


def _non_negative(*, whole: bool = True):
    """An argparse type for a value where zero is meaningful but below zero is not.

    ``--max-runs 0`` means unlimited, so it cannot use :func:`_positive` — and
    as a bare ``int`` a *negative* also read as unlimited and then delivered one
    pass in silence.

    ``--min-margin 0`` means "report every edge", so it needs the same treatment
    with ``whole=False``.  As a bare ``float`` a negative bar admitted markets
    with no edge at all and then filed them under ``rounding_destroys_edge`` —
    "margin -3.54% is too thin for a 1-unit stake increment", of 110 markets on
    the captured slate, when rounding had destroyed nothing and there was
    nothing there to round.  No position is reported either way, so this is a
    diagnostic that accuses the wrong thing rather than money at risk.
    """

    def parse(text: str):
        try:
            value = int(text) if whole else float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{text!r} is not a {'whole number' if whole else 'number'}"
            ) from None
        # Finite first: every comparison below is **False** for NaN, and
        # ``1e400`` parses to ``inf`` without raising.
        if not whole and not math.isfinite(value):
            raise argparse.ArgumentTypeError(f"must be a finite number, got {text}")
        if value < 0:
            raise argparse.ArgumentTypeError(f"cannot be negative, got {text}")
        return value

    return parse


def _a_rate(text: str) -> float:
    """An argparse type for a proportion, which is a number between 0 and 1.

    ``--min-rate`` was a bare ``float``.  ``nan`` made the threshold unreachable,
    so the command exited **0** on the database it exits 1 on by default — a
    silent all-clear from the check an unattended watch reads.  ``1e400`` printed
    "below the inf% success threshold", and ``80`` — which the output invites,
    since every rate on it is a percentage — printed "below the 8000% success
    threshold" and failed every source.

    argparse already prefixes the failing flag's own name onto the message, so
    this takes the value and nothing else.
    """
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be a finite number, got {text}")
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(
            f"is a proportion between 0 and 1, not a percentage, got {text}"
        )
    return value


def _positive(name: str):
    """An argparse type that refuses zero and negatives up front.

    ``--stake 0`` used to reach :func:`src.arb.find_opportunities` and raise
    ``ValueError`` from inside the first position it tried to build — so the same
    invalid argument aborted with a traceback on a slate that had an opportunity
    and was accepted in silence on one that did not.  ``--limit 0`` was worse: it
    printed exactly one row, because the counter was incremented before the
    limit was tested.
    """

    def parse(text: str):
        try:
            value = float(text) if name != "count" else int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
        # Finite first, because every comparison below is **False** for NaN.
        #
        # ``--stake nan`` and ``--stake inf`` walked past ``value <= 0`` and
        # past the one-unit floor, reached ``stake_candidates``' ``int()`` and
        # aborted with a raw ValueError/OverflowError on a slate that had a
        # position — and were accepted in silence on one that did not.  That is
        # verbatim the failure this type exists to remove.  ``1e400`` parses to
        # ``inf`` without raising, so the text is not a sufficient check either.
        if not math.isfinite(value):
            raise argparse.ArgumentTypeError(f"must be a finite number, got {text}")
        if value <= 0:
            raise argparse.ArgumentTypeError(f"must be greater than zero, got {text}")
        if name == "stake" and value < STAKE_INCREMENT:
            # A bankroll under one betting unit rounds every leg to zero.
            # ``find_opportunities`` refuses it, and reaching that refusal from
            # the command line means a traceback — the exact failure this type
            # was written to remove, reintroduced one argument over.
            raise argparse.ArgumentTypeError(
                f"must be at least one {STAKE_INCREMENT:g}-unit stake, got {text}"
            )
        return value

    return parse


def _resolve_run(
    store: Store,
    requested: int | None,
    *,
    what: str,
    needs_fresh_prices: bool = True,
    sports: Sequence[str] | None = None,
    leagues: Sequence[str] | None = None,
) -> tuple[int | None, str]:
    """The run a read command should work on, or ``None`` with the reason why.

    Three failures used to be indistinguishable from a quiet slate, all of them
    printing nothing unusual and exiting 0:

    * ``--run`` naming a run that does not exist — the command reported "0
      opportunities from 0 cross-book markets", which is what a real run with no
      edge says.
    * ``--run`` naming a run that stored no rows.
    * No ``--run`` at all, against a database whose newest finished run is
      **weeks old**.  This is the one that matters: :meth:`Store.latest_run_id`
      returns the newest finished run however old it is, and every freshness
      check in the detector is *relative* — two legs captured six weeks ago are
      seconds apart and clear it. The started-game gate does not help either,
      since a fixture weeks out is still in the future.  So the morning after
      collection silently stops, ``arb`` prints a full page of "guaranteed"
      positions priced before the outage, with nothing anywhere naming their age.

    The age is stated on every run, not only a stale one, so the number is
    normally visible rather than appearing only in the failure.

    *needs_fresh_prices* is false for the commands that do not read a price for
    its takeability.  ``replay`` re-parses stored bytes and ``mirrors`` measures a
    structural property of two feeds; refusing them because the prices are old
    answers a question neither asked, and ``src.report`` calls ``replay_run``
    directly, so the same operation would have succeeded there and failed here.
    """
    # Resolution is scope-aware: "the newest run" for ``arb --sport baseball``
    # is the newest run *holding a baseball row*, not the newest run outright.
    # Scope-blind resolution picked a ``--sport tennis`` run — which kept no
    # baseball by the operator's own flag — printed the quiet-slate sentence
    # this docstring exists to prevent, and exited 0, while the previous run's
    # real baseball edge sat one ``--run`` away and ``health`` on the same
    # database described that earlier run.
    run_id = (
        requested
        if requested is not None
        else store.latest_run_id(sports=sports, leagues=leagues)
    )
    if run_id is None:
        newest = store.latest_run_id()
        if newest is not None:
            run_sports, run_leagues, _ = store.run_scope(newest)
            described = " ".join(
                part
                for part in (
                    f"--sport {','.join(run_sports)}" if run_sports else "",
                    f"--league {','.join(run_leagues)}" if run_leagues else "",
                )
                if part
            )
            return None, (
                f"no stored run holds prices{_scope_label(sports, leagues)} — the "
                f"newest run {newest} "
                + (
                    f"was collected under {described}, which excludes what was asked "
                    "for; collect again without that scope, or pass --run to read an "
                    "earlier run"
                    if described
                    else "holds none; collect again"
                )
            )
        return None, f"no stored runs {what}"
    row = store.run_row(run_id)
    if row is None:
        return None, (
            f"run {run_id} does not exist — the stored runs are "
            f"{_known_runs(store)}"
        )
    if row["finished_at"] is None:
        return None, (
            f"run {run_id} never finished, so what it holds is a fraction of a "
            "collection rather than a slate"
        )
    if not row["quote_count"]:
        # The case this function's own docstring lists and never checked.
        #
        # ``run_row`` selects ``quote_count`` for exactly this, and nothing read
        # it.  ``collect_once`` calls ``finish_run`` unconditionally, so a pass
        # in which every source failed leaves a *finished* run holding nothing —
        # and ``show``/``arb``/``lines`` then answered "0 rows", "0
        # opportunities from 0 cross-book markets" and "0 market(s) shown", each
        # exiting 0.  Those are the sentences a quiet slate produces, which is
        # the confusion the docstring says this exists to prevent.
        return None, (
            f"run {run_id} finished but stored no prices, so there is nothing {what} "
            "— every source failed on that pass. Its health rows say which; collect "
            "again, or pass --run to read an earlier one"
        )
    started = datetime.fromisoformat(row["started_at"])
    if started.tzinfo is None:
        # Everything this build writes is timezone-aware, but an older or
        # hand-edited database need not be, and a freshness check is the last
        # place that should abort a command with a TypeError.
        started = started.replace(tzinfo=UTC)
    age = datetime.now(UTC) - started
    if needs_fresh_prices and requested is None and age < -_CLOCK_SKEW:
        # A run from the future is a clock problem, not a fresh run, and letting
        # it through is the failing-open direction: a naive stamp written east of
        # UTC and read as UTC looks *newer* than it is, so a stale run would sail
        # past the age gate on a negative number.
        return None, (
            f"run {run_id} is stamped {describe_age(-age)} in the future — its clock "
            "and this machine's disagree, so its age cannot be judged. Pass --run to "
            "read it anyway, or fix the clock"
        )
    if needs_fresh_prices and age > MAX_PRICE_AGE and requested is None:
        return None, (
            f"the newest finished run is {run_id}, collected {describe_age(age)} ago "
            f"— older than the {describe_age(MAX_PRICE_AGE)} a price stays takeable. "
            "Collect again, or pass --run to read it anyway as history"
        )
    note = f"run {run_id}, collected {describe_age(age)} ago"
    if row["finished_at"] is not None and not row["ok"]:
        # Resolution never read the verdict, so ``arb`` printed "guaranteed"
        # positions from a run validation had recorded FAILED — whose stored
        # findings can include "every price from this source is suspect" — with
        # nothing on the surface and exit 0.  The failed verdict cost nothing.
        # Not a refusal (history is the operator's to read); a caveat the
        # sentence carries everywhere the run's number goes.
        errors = int(row["error_count"] or 0)
        note += (
            f" — recorded FAILED with {errors} validation error"
            f"{'' if errors == 1 else 's'}; its prices may be mis-parsed, see "
            f"`runs` or the dashboard's Checks"
        )
    if requested is None and (sports or leagues):
        newest = store.latest_run_id()
        if newest is not None and newest != run_id:
            # The scope is *not* named here: every caller of this function
            # appends ``_scope_label`` to the note it prints, so naming it twice
            # produced "holds no prices [sport=tennis] and was skipped
            # [sport=tennis]".
            note += f" — the newer run {newest} holds none of it and was skipped"
    return run_id, note


#: How far ahead of this machine a run's clock may be before its age is unusable.
_CLOCK_SKEW = timedelta(minutes=5)


def _known_runs(store: Store) -> str:
    rows = store.query("SELECT id FROM collection_run ORDER BY id DESC LIMIT 6")
    if not rows:
        return "none"
    listed = ", ".join(str(row["id"]) for row in rows)
    return listed


def _cmd_replay(args: argparse.Namespace) -> int:
    sports, leagues = _filter_args(args)
    raw_store = RawStore(settings.RAW_DIR)
    with _open_store() as store:
        run_id, note = _resolve_run(store, args.run, what="to replay", needs_fresh_prices=False)
        if run_id is None:
            print(note)
            return 1
        ok, problems = replay_run(
            run_id, store=store, raw_store=raw_store, sports=sports, leagues=leagues
        )
        replay_row = store.run_row(run_id)
        replay_state = (replay_row["jurisdiction"] if replay_row else "") or "UNKNOWN"
        print(
            f"replay of run {run_id} [{replay_state}]{_scope_label(sports, leagues)}: "
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
        # ``state`` is a column, not a footnote: a batch writes GLOBAL plus one run
        # per jurisdiction, and without it three rows describing three different
        # slates read as three passes over the same one.
        print(
            f"{'run':>4}  {'started':<26} {'state':<7} {'ok':<3} {'quotes':>7} "
            f"{'events':>7} {'err':>4} {'warn':>5}"
        )
        for row in rows:
            # An unfinished run is not a failed one, and printing ``ok=False``
            # for it says the collection ran and the checks found problems —
            # when what happened is that it never got to the checks.  That is
            # what an interrupted pass leaves behind, and it is also the run the
            # read commands deliberately skip, so the two views contradicted
            # each other on the same database seconds apart.
            state = "?" if row["finished_at"] is None else str(bool(row["ok"]))
            print(
                f"{row['id']:>4}  {row['started_at']:<26} "
                f"{(row['jurisdiction'] or '?'):<7} {state:<5}"
                f"{row['quote_count']:>7} {row['event_count']:>7}"
                f"{row['error_count']:>4} {row['warning_count']:>5}"
            )
            if row["finished_at"] is None:
                print("        never finished — interrupted mid-pass; "
                      "arb, lines, report and mirrors skip it")
            stored_by_source = store.stored_quote_counts(row["id"])
            lost = store.sources_that_failed_to_persist(row["id"])
            for health in store.health_for_run(row["id"]):
                status = "ok" if health["ok"] else f"FAILED[{health['error_kind']}]"
                print(
                    f"        {health['source_key']:<16} {status:<24}"
                    f"{health['quote_count']:>6} quotes {health['event_count']:>4} events"
                )
                # The fetch can succeed and the *insert* still fail, which is the
                # case ``save_quotes_by_source`` isolates per source.  Printing
                # only what the adapter produced read as ``ok  2346 quotes`` for a
                # source with nothing in the table.
                #
                # Gated on the run's own ``quotes_not_persisted`` finding, not on
                # the two counts differing.  ``source_health`` counts what the
                # adapter produced, *before* the ``--sport``/``--league`` filter
                # drops what was out of scope — so ``collect --league EPL`` had
                # this accusing the database of losing 351 FanDuel rows, naming an
                # error the run does not contain, two lines above the run's own
                # note saying those rows were excluded by filter.
                produced = health["quote_count"]
                stored = stored_by_source.get(health["source_key"], 0)
                if lost is None or health["source_key"] in lost:
                    print(
                        f"        {'':<16} {stored} of those {produced} rows are in the "
                        "database — the rest were not stored; see the "
                        "quotes_not_persisted error"
                    )
            # Per-sport, because "the run stored 12,000 rows" says nothing about
            # whether any one sport is comparable across books.
            cross = store.cross_book_event_counts(
                row["id"],
                min_books=MIN_HEALTHY_SOURCES,
                # The run's own jurisdiction, not this process's.
                view_only=registry.view_only_for_run(row["jurisdiction"]),
            )
            # The run's own partition, so an all-mirror sport is named as such
            # rather than printed as "NO OVERLAP … 3 book(s)" — which asserts
            # three counterparties had no fixture in common when the truth is
            # zero counterparties priced it at all.  Same two-verdicts family
            # as the collect-time note, surviving on the replay surface.
            per_sport_sources = store.sport_sources(row["id"])
            runs_view_only = registry.view_only_for_run(row["jurisdiction"])
            for entry in store.sport_coverage(row["id"]):
                if sports and entry["sport"] not in sports:
                    continue
                shared = cross.get(entry["sport"], 0)
                sport_sources = per_sport_sources.get(entry["sport"], frozenset())
                counterparty_count = len(sport_sources - runs_view_only)
                view_only_count = entry["source_count"] - counterparty_count
                if entry["source_count"] and not counterparty_count:
                    bar = "VIEW-ONLY"
                elif counterparty_count < MIN_HEALTHY_SOURCES:
                    bar = "1 BOOK ONLY"
                elif not shared:
                    bar = "NO OVERLAP"
                else:
                    bar = "usable"
                books = f"{entry['source_count']} book(s)"
                if view_only_count:
                    books += f" ({view_only_count} view-only)"
                print(
                    f"          {entry['sport']:<12} {bar:<12}"
                    f"{entry['quote_count']:>7} quotes {entry['event_count']:>4} fixtures "
                    f"({shared} cross-book) {books}, "
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
        run_id, note = _resolve_run(
            store, args.run, what="to show", sports=sports, leagues=leagues
        )
        if run_id is None:
            print(note)
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
        # ``note`` carries the run's age and any caveat on it — a FAILED
        # verdict, a newer out-of-scope run skipped.  ``show`` was building its
        # own header and throwing all of that away, so it alone printed
        # thirty-six-hour-old prices with no age on them while ``arb`` and
        # ``lines`` stated it, against this module's own promise that the age is
        # stated on every run and not only a stale one.
        # And the jurisdiction, for the same reason the age is stated: these are one
        # state's prices, and a reader who did not choose the state was being shown
        # them with nothing on the page naming it.  ``--run`` defaults to the latest
        # run, which in a batch is the last state collected.
        show_row = store.run_row(run_id)
        show_state = (show_row["jurisdiction"] if show_row else "") or "UNKNOWN"
        print(
            f"{note}{_scope_label(sports, leagues)} [{show_state}]: "
            f"showing {len(rows)} rows"
        )
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
        run_id, note = _resolve_run(
            store, args.run, what="to analyse", sports=sports, leagues=leagues
        )
        if run_id is None:
            print(note)
            return 1
        # The gate is measured on the **whole run** and the report is narrowed,
        # not the other way round: the evidence that two venues are one
        # counterparty does not stop existing because this command was asked
        # about one league.  See ``collect_once``.
        everything, _ = reconcile_event_keys(store.load_quotes(run_id))
        # Unioned with what the collector recorded at collection time.  A scoped
        # run stores only the kept rows, so the store alone is narrowed evidence
        # — the mirror established on 22 shared MLB selections vanished from a
        # ``--sport tennis`` run's rows, the gate reopened, and this command
        # published a "guaranteed" +3.00 with both legs at one operator seconds
        # after the live pass refused it.  When the run already recorded the
        # full-slate measurement, re-scanning the (possibly narrower) rows
        # cannot strengthen the gate and is skipped.
        # Which jurisdiction's rules apply is a property of the **run**, not of
        # this process.  Omitting ``view_only_sources`` fell back to the
        # module-level ``VIEW_ONLY_SOURCES``, which :mod:`src.sources.registry`
        # freezes at import from ``settings.STATE`` — so analysing a stored PA
        # run applied *Illinois's* view-only set, and any book Pennsylvania
        # declares view-only but Illinois does not was eligible to be a leg.
        # ``collect_once`` and ``src.report`` both resolve this per run; this
        # command was the one that did not, and it is the one that sends a text.
        # Resolved *before* the gate below, because the gate re-measures with
        # the same set: measuring pairs with the reader's set while forming
        # legs with the run's turned a stored IL run's measured hardrock mirror
        # into "guaranteed +15.00" on any PA-configured box.
        run_row = store.run_row(run_id)
        run_state = (run_row["jurisdiction"] if run_row is not None else "") or ""
        run_state = run_state.strip().upper()
        recorded = store.recorded_counterparty_groups(run_id)
        measured_counterparties = merge_counterparty_groups(
            {}
            if recorded
            else counterparty_groups(
                everything, view_only=registry.view_only_for_run(run_state)
            ),
            recorded,
        )
        quotes = [q for q in everything if in_scope(q, sports, leagues)]
        report = find_opportunities(
            quotes,
            total_stake=args.stake,
            min_margin=args.min_margin / 100.0,
            # Gated on the clock by default, exactly as the live path is.
            # Without it, re-analysing yesterday's run prints positions on games
            # that have already been played, indistinguishable from takeable ones
            # — and the default ``--run`` is the latest run, so the same command
            # was live on one invocation and historical on the next.
            as_of=None if args.include_started else datetime.now(UTC),
            one_counterparty=measured_counterparties,
            view_only_sources=registry.view_only_for_run(run_state),
        )
        # And the exact-state leg marking, for the same reason.  Without it
        # this command printed — and texted — a "PA" arbitrage whose every leg
        # was a global or offshore venue holding no Pennsylvania licence, with
        # nothing saying so.  The default ``--run`` is the latest run, which in a
        # batch is the last state collected, so the operator neither chose that
        # jurisdiction nor was told which one they got.
        arb_marking = locality_marking(
            run_state,
            # The scope the run was *collected* under, not this process's.
            route_scope=(run_row["route_scope"] if run_row is not None else "") or "",
        )
        non_local_flagged = arb_marking.count_without_local_leg(report.opportunities)
        print(f"jurisdiction: {run_state or 'UNKNOWN'}")
        if args.include_started:
            print(
                "including fixtures that have already started — these are a "
                "historical study, not positions anyone can take"
            )
        print(f"{note}{_scope_label(sports, leagues)}: {report.summary()}")
        # Printed after the summary, because it explains the labels below rather
        # than standing on its own.  Said out loud rather than left to per-leg
        # tags alone: a reader skimming for a count should learn how many
        # positions are wholly foreign without reading every leg.
        if non_local_flagged:
            print(
                f"{non_local_flagged} position(s) have no leg you can reach from "
                f"{run_state} — shown and labelled, not takeable from there"
            )
        for opportunity in report.opportunities:
            print(
                opportunity.describe(
                    leg_note=lambda s: (
                        ""
                        if arb_marking.leg_is_local(s)
                        else f"  [{arb_marking.label()}]"
                    )
                )
            )
            if not arb_marking.has_local_leg(opportunity):
                print(
                    f"    no leg reachable from {run_state} — informational, "
                    f"not {run_state} prices"
                )
        rejected = Counter(d.code for d in report.diagnostics)
        if rejected:
            print(f"\nrejected: {dict(rejected)}")
            if args.verbose:
                for diagnostic in report.diagnostics:
                    print(f"  [{diagnostic.code}] {diagnostic.event_key} "
                          f"{diagnostic.market.value}/{diagnostic.period.value}: {diagnostic.detail}")
        if report.opportunities and not args.no_alert:
            notified = notify_opportunities(report.opportunities, marking=arb_marking)
            if notified:
                print(
                    f"\ntexted {len(notified)} arb(s) "
                    f"(>= {settings.ALERT_MIN_ROI * 100:.1f}% ROI) to {settings.ALERT_TO}"
                )
        return 0


def _net_or_quoted(quote: Quote) -> float:
    """The price after commission, falling back to the quoted one.

    The commission models refuse odds at or below 1.0, which the schema's floor
    already makes unreachable from a live row — but this is a display path, and
    ``src.report`` guards the identical call for the identical reason.  One
    surface aborting a whole command on a row the other renders as "unknown"
    would be a difference nobody could explain.
    """
    try:
        return net_decimal_odds(quote.source, quote.decimal_odds)
    except ValueError:
        return quote.decimal_odds


def _cmd_lines(args: argparse.Namespace) -> int:
    """Best available price per selection, across books — the line-shopping view.

    This is the surface arbitrage is drawn from, so printing it is how you tell a
    genuine "no edge today" apart from a market nobody is actually comparing.

    Printed **net of commission**, because that is what :func:`best_prices`
    ranks by and what :func:`find_opportunities` acts on.  Printing the quoted
    number beside a sum computed from it put this command into direct
    contradiction with the detector on exactly the legs the commission model
    exists for: four markets on the committed slate printed a sum below 1.0 —
    ``MLB-TEX@MLB-TB total/full_game @8.5`` showed 0.9976 — whose net sum is
    1.0200 and which ``arb`` therefore, correctly, does not report.  An operator
    comparing the two surfaces would have concluded the detector was broken.

    Where the quoted and net prices differ the quoted one is shown too, since
    the net price is not what you will see on the venue's own screen.
    """
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        run_id, note = _resolve_run(
            store, args.run, what="to read", sports=sports, leagues=leagues
        )
        if run_id is None:
            print(note)
            return 1
        # Reconciled over the **whole run**, then narrowed.  Reconciliation
        # clusters start times globally and numbers repeat fixtures over the
        # slate, so filtering first can merge two clusters — by removing the
        # member whose listing made a span exceed the tolerance — and can
        # renumber a doubleheader ordinal.  Either makes this command key a
        # fixture differently from ``arb`` on the same stored run, which is the
        # one thing this command's own contract says must not happen.
        everything, _ = reconcile_event_keys(store.load_quotes(run_id))
        quotes = [quote for quote in everything if in_scope(quote, sports, leagues)]
        run_row = store.run_row(run_id)
        lines_state = (run_row["jurisdiction"] if run_row is not None else "") or ""
        lines_state = lines_state.strip().upper()
        # Resolved from the **run's** jurisdiction, exactly as ``arb`` does it four
        # hundred lines up.  ``best_prices`` defaults to the ambient
        # ``VIEW_ONLY_SOURCES``, frozen at import from ``settings.STATE``, and
        # ``hardrock`` is the key that differs: a Pennsylvania run read from an
        # Illinois box put a Hard Rock price on the PA board as a selection's *best*
        # price, and the same run read from a PA box did not.
        surface = best_prices(quotes, view_only=registry.view_only_for_run(lines_state))
        sport_of = {quote.event_key: (quote.sport.value, quote.league) for quote in quotes}
        # Measured on the whole run and unioned with the collection-time
        # record, for the same reason ``arb`` does it — and under the run's
        # view-only set, for the same reason again.  Reuse the already-
        # reconciled rows — a second load+reconcile was pure duplicate work.
        recorded = store.recorded_counterparty_groups(run_id)
        one_counterparty = merge_counterparty_groups(
            {}
            if recorded
            else counterparty_groups(
                everything, view_only=registry.view_only_for_run(lines_state)
            ),
            recorded,
        )

        # Which jurisdiction, and which of these books it does not license.
        #
        # Deliberately marked rather than filtered: this command's whole purpose is
        # telling a genuine "no edge today" apart from a market nobody compared, and
        # dropping the out-of-state books would hide the comparison it exists to
        # show.  But printing "home 1.423 onexbet" unlabelled on a Pennsylvania run
        # presents a price the reader cannot take as the state's best — the same
        # substitution ``src.coverage`` refuses.  So: recorded, named, never silent.
        lines_scope = (run_row["route_scope"] if run_row is not None else "") or ""
        # The same condition ``arb`` and the dashboard use, asked of the same
        # function.  Spelling it here as "is the jurisdiction known" was a fourth
        # answer: on a run stored with a widened scope, ``arb`` offered a position
        # while ``lines``, one command later, marked every leg of it unreachable.
        lines_marking = locality_marking(lines_state, route_scope=lines_scope)
        if lines_state:
            print(f"jurisdiction: {lines_state}")

        def _elsewhere(source_key: str) -> str:
            """Mark a book the operator cannot reach from this jurisdiction.

            Reachability only, and it needs no republisher case: ``best_prices``
            above is given this run's view-only set, so every mirror has already been
            excluded from the surface and nothing here can be one.  A branch marking
            them would be unreachable, and a mark reading "not reachable from PA"
            beside ``an_fanduel`` — book id 255, *FanDuel Pennsylvania* — would be
            false as well, which is why the two facts are kept apart rather than
            spelled by one string.
            """
            if lines_marking.leg_is_local(source_key):
                return ""
            return f"  [{lines_marking.label()}]"

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
            net = {
                selection: _net_or_quoted(quote)
                for selection, quote in selections.items()
            }
            overround = sum(1.0 / price for price in net.values())
            print(
                f"[{sport}/{league}] {event_key} {label}{line_label}  (sum {overround:.4f})"
            )
            if overround < 1.0:
                refusal = _why_the_detector_would_refuse(
                    list(selections.values()), one_counterparty
                )
                if refusal is not None:
                    print(f"    note: {refusal}")
            for selection, quote in sorted(selections.items(), key=lambda i: i[0].value):
                charged = commission_for(quote.source)
                gross = (
                    ""
                    if charged.is_free
                    else f"  (quoted {quote.decimal_odds:.3f}, {charged.describe()})"
                )
                american = (
                    quote.american_odds
                    if charged.is_free
                    else decimal_to_american(net[selection])
                )
                print(
                    f"    {selection.value:<6} {net[selection]:>7.3f} "
                    f"{american:>+6d}  {quote.source}{gross}"
                    f"{_elsewhere(quote.source)}"
                )
            shown += 1
            if shown >= args.limit:
                break  # tested after the increment, so --limit N shows N
        closing = (
            f"\n{shown} market(s) shown of {len(surface)} in {note}"
            f"{_scope_label(sports, leagues)}"
        )
        if not surface and quotes:
            # "0 of 0" beside a run holding thousands of rows reads as an empty
            # collection.  Account for every stored row **by the reason the
            # surface excluded it**, in ``best_prices``' own evaluation order
            # (status first, then view-only).  The first cut of this message
            # said "all from N view-only feed(s)" unconditionally, which was
            # false the moment a run's rows were all *suspended* instead —
            # "all from 0 view-only feeds" over two suspended counterparty
            # rows, a wrong explanation in the exact spot wrong explanations
            # were being removed.
            run_view_only = registry.view_only_for_run(lines_state)
            inactive = sum(
                1 for quote in quotes if quote.status is not QuoteStatus.ACTIVE
            )
            view_only_active = sum(
                1
                for quote in quotes
                if quote.status is QuoteStatus.ACTIVE
                and quote.source in run_view_only
            )
            remainder = len(quotes) - inactive - view_only_active
            reasons = []
            if inactive:
                reasons.append(f"{inactive} not ACTIVE")
            if view_only_active:
                reasons.append(
                    f"{view_only_active} from view-only feeds, which never "
                    "surface as a best price"
                )
            if remainder:
                reasons.append(f"{remainder} excluded by other surface rules")
            closing += (
                f" — none of the {len(quotes)} stored row(s) can surface: "
                + "; ".join(reasons)
            )
        print(closing)
        return 0


def _why_the_detector_would_refuse(
    legs: Sequence[Quote], one_counterparty: Mapping[str, Sequence[frozenset[str]]]
) -> str | None:
    """Why ``arb`` will not report a sub-1.0 sum this surface is showing.

    Commission was the first way these two surfaces contradicted each other, and
    fixing it left the other two gates open: a sum below 1.0 here is printed the
    same whether the detector would act on it or refuse it outright.  Verified
    against the committed slate — a fanduel/kalshi pair prints ``sum 0.9282``, a
    7.2% apparent edge, and is rejected as ``legs_do_not_void_together``; two
    mirrored Kambi tenants print ``sum 0.9302`` while the run reports ``0
    opportunities from 0 cross-book markets`` and emits no diagnostic at all, so
    nothing anywhere explains the contradiction.

    This says so on the line itself.  Reporting is not the gate — the detector
    stays the authority on what is takeable — so this deliberately explains
    rather than filters.
    """
    # Freshness first, because it is the refusal that actually fires.  On the
    # committed slate **all three** sub-1.0 cross-book groups are refused as
    # ``stale_leg`` and this returned ``None`` for every one of them — so the
    # annotation explained the two gates that had not fired and stayed silent on
    # the one that had, which is the contradiction it was written to close.
    spread = max(q.observed_at for q in legs) - min(q.observed_at for q in legs)
    if spread > MAX_OBSERVATION_SPREAD:
        return (
            f"these prices were observed {describe_age(spread)} apart, further than "
            "two legs may be to have been available at the same moment"
        )
    sources = [quote.source for quote in legs]
    mapping = _counterparties(list(legs), one_counterparty)
    identities = {mapping.get(source, source) for source in sources}
    if len(identities) < len(set(sources)):
        return (
            "two of these venues are measurably one counterparty, so this is one "
            "book against itself and not a position anybody can hold"
        )
    clash = settlement_mismatch(sources)
    if clash is not None:
        return f"these venues do not void together — {clash.note}"
    # Deliberately not exhaustive, and it says so rather than implying it is.
    # The detector has a dozen further refusals — an implausible margin, an
    # ambiguous tie, a duplicate selection, a line granularity it cannot
    # represent — and they are properties of a *position* it has assembled, not
    # of the best-price surface printed here.  What this covers is the set that
    # can be decided from two prices alone.
    return None


def _cmd_mirrors(args: argparse.Namespace) -> int:
    """Print how closely every pair of sources agrees on price.

    The number behind the distinctness gate, so it can be read before it fails a
    run rather than only afterwards — and so a candidate source can be screened
    against the registry before anybody writes it in.
    """
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        run_id, note = _resolve_run(store, args.run, what="to compare", needs_fresh_prices=False)
        if run_id is None:
            print(note)
            return 1
        # Measured on the **whole run**; the scope narrows only what is printed.
        #
        # This is the third place the same defect has had to be closed — the
        # other two are ``_check_distinctness(unfiltered_quotes, ...)`` in
        # ``collect_once`` and ``_cmd_arb``'s "measured on the whole run and the
        # report is narrowed, not the other way round" — and
        # ``counterparty_groups``' docstring calls it the most expensive defect
        # this gate has had.  Measured narrowed, this command answered its own
        # question two opposite ways on one run: unscoped, "rsiusil vs rsiusnj:
        # 24/28 identical (85.7%) — MIRROR", exit 1; under ``--sport tennis``,
        # "only 4 shared selection(s), fewer than the 20 needed to judge — no
        # verdict", exit 0.  The exit code is a gate a person screens a candidate
        # source with, so the narrowed answer is the dangerous one.
        everything, _ = reconcile_event_keys(store.load_quotes(run_id))
        # The **run's** view-only set, resolved once and used for both the pair
        # formation and the explanation of an empty result.  ``compare_all``'s
        # ambient default is frozen from this process's ODDS_STATE, and
        # ``hardrock`` differs between IL and PA/DC — so a stored IL run read
        # from a PA box formed no hardrock pair while the message layer,
        # resolving from the run, denied that anything was missing.  Same
        # bytes, two verdicts, decided by the reader's environment: the exact
        # defect class ``view_only_for_run`` exists to kill.
        mirror_run_row = store.run_row(run_id)
        mirror_run_state = (
            (mirror_run_row["jurisdiction"] if mirror_run_row else "") or ""
        )
        run_view_only = registry.view_only_for_run(mirror_run_state)
        pairs = compare_all(everything, view_only=run_view_only)
        if not pairs:
            # Say which of two very different situations this is.  On the first
            # all-republisher run this line claimed "fewer than two sources
            # stored" about a run holding ten sources and 2,871 rows — every
            # one view-only, which is why ``compare_all`` (rightly) had no
            # counterparty pair to form.  A false statement about the store
            # points the operator at the collector when the real answer is
            # "these feeds are mirrors by design".
            stored = {quote.source for quote in everything}
            counterparty = sorted(stored - run_view_only)
            if len(stored) >= 2 and len(counterparty) < 2:
                print(
                    f"run {run_id}: {len(stored)} source(s) stored, but "
                    f"{len(stored) - len(counterparty)} are view-only feeds and a "
                    "mirror is not a counterparty — nothing to compare"
                )
            else:
                print(
                    f"run {run_id}: fewer than two sources stored, so nothing to compare"
                )
            return 1
        scope_note = (
            " — measured on the whole run, because narrowing the evidence can "
            "only weaken it" if sports or leagues else ""
        )
        mirror_state = mirror_run_state or "UNKNOWN"
        print(
            f"run {run_id} [{mirror_state}]{_scope_label(sports, leagues)}: "
            f"{len(pairs)} source pair(s){scope_note}"
        )
        for pair in pairs:
            print(f"  {pair.summary()}")
        mirrors = [pair for pair in pairs if pair.verdict.blocks_registration]
        if mirrors:
            print(
                f"\n{len(mirrors)} pair(s) are one counterparty in {mirror_state}. "
                "Remove one of each from "
                "src.sources.registry: an arbitrage reported between them is a position "
                "nobody can hold."
            )
        return 1 if mirrors else 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Per-source health across recent runs, so a slow decay is visible."""
    sports, leagues = _filter_args(args)
    with _open_store() as store:
        rows = store.run_summaries(limit=args.limit, sports=sports, leagues=leagues)
        if not rows:
            print(f"no runs recorded{_scope_label(sports, leagues)}")
            return 1
        # The success rates are computed over **every** run in the window, not
        # the scoped ones.  Health is a property of a source, not of a sport:
        # ``run_summaries`` keeps only runs holding a row in scope, which deleted
        # from this command exactly the runs it exists to surface — a total
        # outage stores nothing, so ``health`` read 60% and exited 1 while
        # ``health --sport baseball`` read 100% and exited 0 on the same
        # database.  A first repair kept the scoped rates and flagged blank runs
        # beside them, which fixed that case and left two others: the flag fired
        # "a source is below the threshold" over a table showing every rate at
        # 100%, and a source that failed on a run holding *another* sport's rows
        # still vanished from the scoped rate.  Scope narrows what is *shown*
        # about coverage; it must not narrow what the verdict is computed from.
        every_run = store.run_summaries(limit=args.limit)
        per_source: dict[str, list[bool]] = {}
        per_state: dict[tuple[str, str], list[bool]] = {}
        for row in every_run:
            state = (row["jurisdiction"] or "?").strip().upper() or "?"
            for health in store.health_for_run(row["id"]):
                key = health["source_key"]
                per_source.setdefault(key, []).append(bool(health["ok"]))
                per_state.setdefault((key, state), []).append(bool(health["ok"]))

        print(f"{'source':<18} {'runs':>5} {'ok':>5} {'rate':>6}  last (newest first)")
        below: list[str] = []
        for source_key, outcomes in sorted(per_source.items()):
            ok_count = sum(outcomes)
            rate = ok_count / len(outcomes)
            recent = "".join("." if ok else "X" for ok in outcomes)
            print(f"{source_key:<18} {len(outcomes):>5} {ok_count:>5} {rate * 100:>5.0f}%  {recent}")
            # A republisher's route is a *state licence*, so one key can be healthy
            # in Illinois and dead in Pennsylvania — and the blended rate is what
            # ``--min-rate`` fires on. Printed as a split rather than replacing the
            # blend: the blend is still the answer to "is this source worth having",
            # and the split is the answer to "where is it broken".
            states = {
                state: rates
                for (key, state), rates in per_state.items()
                if key == source_key
            }
            if len(states) > 1 and len(set(
                sum(r) / len(r) for r in states.values()
            )) > 1:
                split = "  ".join(
                    f"{state} {sum(rates) / len(rates) * 100:.0f}% ({len(rates)})"
                    for state, rates in sorted(states.items())
                )
                print(f"{'':<18} {'':>5} {'':>5} {'':>6}  by state: {split}")
            if rate < args.min_rate:
                below.append(source_key)

        if sports or leagues:
            scoped_ids = {row["id"] for row in rows}
            uncovered = [row for row in every_run if row["id"] not in scoped_ids]
            if uncovered:
                print(
                    f"\nrates above cover all {len(every_run)} run(s); "
                    f"run(s) {', '.join(str(row['id']) for row in uncovered)} hold no "
                    f"prices{_scope_label(sports, leagues)} and appear only in the rates"
                )

        # The same run the read commands work on, so ``health`` and ``arb`` cannot
        # describe different collections seconds apart.  An interrupted pass is
        # listed above with its own marker and named here, rather than silently
        # becoming "the latest run" for one command and not for another.
        finished = [row for row in rows if row["finished_at"] is not None]
        if len(finished) < len(rows):
            unfinished = [str(row["id"]) for row in rows if row["finished_at"] is None]
            print(
                f"\nrun(s) {', '.join(unfinished)} never finished and are not counted "
                "below; every other command skips them too"
            )
        if not finished:
            print("no finished runs to summarise")
            return 1
        latest = finished[0]
        # Health per source is not health per sport: a book can be perfectly
        # healthy and still be the only book pricing a sport.
        print(f"\nlatest run {latest['id']} — which sports are comparable across books:")
        coverage = [
            entry
            for entry in store.sport_coverage(latest["id"])
            if not sports or entry["sport"] in sports
        ]
        cross = store.cross_book_event_counts(
            latest["id"],
            min_books=MIN_HEALTHY_SOURCES,
            view_only=registry.view_only_for_run(latest["jurisdiction"]),
        )
        if not coverage:
            print("  no rows stored for this scope")
        # Counterparty partition, same as ``runs``: an all-mirror sport must
        # not be graded as counterparties without a shared fixture.
        health_sport_sources = store.sport_sources(latest["id"])
        health_view_only = registry.view_only_for_run(latest["jurisdiction"])
        counterparty_counts = {
            entry["sport"]: len(
                health_sport_sources.get(entry["sport"], frozenset())
                - health_view_only
            )
            for entry in coverage
        }
        for entry in coverage:
            shared = cross.get(entry["sport"], 0)
            counterparty_count = counterparty_counts[entry["sport"]]
            view_only_count = entry["source_count"] - counterparty_count
            if entry["source_count"] and not counterparty_count:
                bar = "VIEW-ONLY"
            elif counterparty_count < MIN_HEALTHY_SOURCES:
                bar = "1 BOOK ONLY"
            elif not shared:
                bar = "NO OVERLAP"
            else:
                bar = "usable"
            books = f"{entry['source_count']} book(s)"
            if view_only_count:
                books += f" ({view_only_count} view-only)"
            print(
                f"  {entry['sport']:<12} {bar:<12} {books}, "
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
            if counterparty_counts[entry["sport"]] < MIN_HEALTHY_SOURCES
            or not cross.get(entry["sport"], 0)
        ]
        if unusable:
            print(f"not comparable across books: {', '.join(unusable)}")
        if below:
            # Named, so the sentence cannot contradict the table it follows —
            # the flag it replaced also fired for blank scoped-out runs, printing
            # "a source is below the threshold" over rates that all read 100%.
            print(
                f"{', '.join(below)} below the "
                f"{args.min_rate * 100:.0f}% success threshold"
            )
        # The verdict reads the newest finished run **overall**: under a scope,
        # ``latest`` is the newest run with rows in scope, and if the run after
        # it failed outright the scoped view was exiting 0 on a database whose
        # unscoped view exits 1.
        overall = next(
            (row for row in every_run if row["finished_at"] is not None), None
        )
        if overall is not None and overall["id"] != latest["id"] and not overall["ok"]:
            print(
                f"newest run {overall['id']} was recorded FAILED — it holds no "
                f"prices{_scope_label(sports, leagues)}, so it is absent from the "
                "coverage above"
            )
        newest_ok = bool(overall["ok"]) if overall is not None else bool(latest["ok"])
        return 0 if not below and newest_ok else 1


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
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    refusal = settings.refuse_bad_settings()
    if refusal is not None:
        return refusal
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
    collect.add_argument(
        "--auto-state",
        action="store_true",
        help="deprecated compatibility flag; live collection now always detects state",
    )
    collect.add_argument(
        "--state",
        action="append",
        type=lambda value: value.strip().upper(),
        choices=["IL", "PA", "NJ", "DC"],
        help="additional state to scrape; repeatable (detected state is always included)",
    )
    collect.add_argument(
        "--tier",
        choices=[tier.value for tier in Tier],
        default=Tier.FULL.value,
        help=(
            "request budget. 'core' asks each source only for the endpoints that "
            "return a whole league at once (a few requests per source, safe on a "
            "short interval); 'full' adds the per-event follow-ups — FanDuel's "
            "soccer detail pages are 126 of its 133 requests"
        ),
    )
    collect.add_argument("--watch", action="store_true", help="collect repeatedly")
    # Both guarded, because ``--watch`` is the unattended mode.
    #
    # As bare ``int`` these took values they cannot honour: ``--interval -5``
    # reached ``time.sleep`` and killed the loop with a traceback after the
    # first successful pass; ``--interval 0`` polled ten public endpoints
    # continuously, defeating the politeness this pipeline calls "a design
    # property here, not a courtesy"; and ``--max-runs -1`` read as unlimited
    # (0 means unlimited) and delivered exactly one pass with no message.
    collect.add_argument(
        "--interval",
        type=_positive("interval"),
        default=settings.DEFAULT_INTERVAL_SECONDS,
        help="seconds between passes in --watch; must be positive",
    )
    collect.add_argument(
        "--max-runs",
        type=_non_negative(),
        default=0,
        help="0 means unlimited",
    )
    collect.add_argument("--no-store", action="store_true", help="skip the database, still store raw")
    collect.add_argument(
        "--no-alert",
        action="store_true",
        help="skip SMS even when Twilio is configured",
    )
    _add_scope_arguments(collect)
    collect.set_defaults(func=_cmd_collect)

    replay = subparsers.add_parser("replay", help="re-parse a stored run's raw responses")
    replay.add_argument("--run", type=int, help="run id; defaults to the most recent")
    _add_scope_arguments(replay)
    replay.set_defaults(func=_cmd_replay)

    runs = subparsers.add_parser("runs", help="list recent runs and per-source health")
    runs.add_argument("--limit", type=_positive("count"), default=10)
    _add_scope_arguments(runs)
    runs.set_defaults(func=_cmd_runs)

    show = subparsers.add_parser("show", help="print normalized rows from a run")
    show.add_argument("--run", type=int)
    show.add_argument("--limit", type=_positive("count"), default=40)
    _add_scope_arguments(show)
    show.set_defaults(func=_cmd_show)

    arb = subparsers.add_parser("arb", help="find arbitrage in a stored run")
    arb.add_argument("--run", type=int, help="run id; defaults to the most recent")
    arb.add_argument(
        "--stake", type=_positive("stake"), default=100.0, help="bankroll per position"
    )
    arb.add_argument(
        "--min-margin",
        type=_non_negative(whole=False),
        default=0.0,
        help="minimum edge to report, in percent",
    )
    arb.add_argument("--verbose", action="store_true", help="explain every rejected market")
    arb.add_argument(
        "--include-started",
        action="store_true",
        help=(
            "also report fixtures that have already begun. A historical study of "
            "what the stored prices implied — never a position that can be taken"
        ),
    )
    arb.add_argument(
        "--no-alert",
        action="store_true",
        help="skip SMS even when Twilio is configured",
    )
    _add_scope_arguments(arb)
    arb.set_defaults(func=_cmd_arb)

    lines = subparsers.add_parser("lines", help="best price per market across books")
    lines.add_argument("--run", type=int, help="run id; defaults to the most recent")
    lines.add_argument("--limit", type=_positive("count"), default=20)
    lines.add_argument(
        "--cross-book-only",
        action="store_true",
        help="only markets priced by more than one book",
    )
    _add_scope_arguments(lines)
    lines.set_defaults(func=_cmd_lines)

    mirrors = subparsers.add_parser(
        "mirrors",
        help="how closely each pair of sources agrees on price — the distinctness gate",
    )
    mirrors.add_argument("--run", type=int, help="run id; defaults to the most recent")
    _add_scope_arguments(mirrors)
    mirrors.set_defaults(func=_cmd_mirrors)

    health = subparsers.add_parser("health", help="per-source success rate across recent runs")
    health.add_argument(
        "--limit", type=_positive("count"), default=20,
        help="how many runs to look back over",
    )
    health.add_argument(
        "--min-rate", type=_a_rate, default=0.8,
        help="failure threshold, 0-1",
    )
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

    args = parser.parse_args(effective_argv)
    if args.command is None:
        args = parser.parse_args(["collect", *effective_argv])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
