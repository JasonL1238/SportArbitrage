"""Polymarket US: the CFTC-designated venue, which is not the offshore one.

``gateway.polymarket.us`` is open and unauthenticated.  It is the **public**
host; ``api.polymarket.us`` is the authenticated one and answers ``401 Missing
required API key headers`` to everything.  That distinction cost three days —
the venue was recorded as KYC-gated on the strength of a probe against the wrong
hostname — so it is stated here as well as in
``docs/evidence/exchanges-and-mirrors.md``.

This is a **separate venue** from :mod:`src.sources.polymarket`, not a config
variant of it.  QCX LLC is a CFTC-designated contract market with its own order
book, its own fee schedule (``0.06 × p × (1−p)``, not the offshore
``0.05 × min(p, 1−p)``) and its own settlement rule (last fair market price, not
resolve-at-0.50).  :data:`src.commission.COMMISSIONS` and
:data:`src.settlement.SETTLEMENT` are keyed by source key, so sharing one would
read a fee schedule and a rain-out rule off the wrong venue's bytes.

Four things about this payload are traps, in the precise sense that each one
produces a *plausible wrong number* rather than an error.  All four are measured,
not inferred, and each has a guard below.

**1. ``outcomes`` is not aligned with ``outcomePrices`` or ``marketSides``.**
Measured over 544 NFL markets on 2026-08-13: ``outcomes[0]`` disagreed with
``marketSides[0].description`` on 198 of them, 36%.  On the KC @ LAD moneyline,
``outcomes`` reads ``["Los Angeles Dodgers","Kansas City Royals"]`` while
``marketSides[0]`` is Kansas City — so indexing prices by ``outcomes`` puts the
Dodgers at 0.05, a 20:1 line on a heavy favourite.  **Nothing here reads
``outcomes`` or ``outcomePrices``.**  ``marketSides`` is the only price source,
and it is self-describing: each side carries its own ``teamId``, its own label
and its own quote.

**2. The takeable price is ``marketSides[i].quote.value``.**  Verified 544/544:
side 0's quote equals ``bestAskQuote`` and side 1's equals ``1 − bestBidQuote``.
The venue performs the transform the offshore adapter has to do by hand — buying
the second token is selling the first, so its cost is ``1 − bid`` — and publishes
the result per side.  ``outcomePrices`` is ``[bestBid, bestAsk]`` of *one* token,
which is why it does not sum to 1.00 and must never be read as two complementary
probabilities.

**3. ``team_`` versus ``game_`` in ``sportsMarketType`` does not mean what it
says.**  ``football_team_full_game_total`` is a **game** total ("Will the total
in GB vs PIT be more than 17.5") while ``football_team_first_half_total`` is a
**team** total ("Will GB score more than 9.5 in the first half").  They differ by
one token and are opposite scopes.  What actually decides it is ``metadata``:
present, with a ``teamId`` both sides carry, exactly on the team-scoped markets.
So :data:`MARKET_TYPES` declares a :class:`Scope` per type and
:func:`_scope_of` **asserts** it against the payload.  A disagreement is a
rejection, never a reinterpretation — if the venue renames one of these, this
refuses loudly instead of publishing a 17.5-point game line as one team's total.

**4. Home and away come from the league, never from the team blob.**  On market
``390522`` both sides carry ``teamId: 74`` and the embedded team objects say
``ordering: "away"`` on side 0 and ``"home"`` on side 1 — the same team, two
answers, because that field is stamped by side index.  The real statement is the
response's own ``league.ordering``: ``"away"`` means ``teams[0]`` is the away
side (mlb, nfl, nba, nhl, wnba) and ``"home"`` means it is the home side (epl,
mls, ucl).  Each route declares what it expects and the payload is checked
against it, so a silent flip is a rejection rather than a mirrored slate.

Two smaller shape facts, both load-bearing:

*A spread's line lives on its own side.*  ``marketSides[i].description`` is
``"+20.50"`` / ``"-20.50"``, already signed for that side's own team.  The
market-level ``line`` is not per-side and its sign is inconsistent across
families (``2.5`` on one full-game spread, ``-1.5`` on a first-five), so it is
read only for totals, where both sides share it and the sign question does not
arise.  There is no index-based sign flip here, unlike the offshore adapter.

*Soccer is the mirror image of offshore.*  There the three-way is split into
``["Yes","No"]`` contracts naming no competitor, so no soccer moneyline is
collected.  Here each contract carries a ``teamId`` — or ``None``, which is the
draw — so the three-way **is** attributable and is collected.  What this venue
does not publish is any soccer spread or total.  The three contracts share one
synthesized ``source_market_id`` so the completeness and overround checks see one
three-way market rather than three markets of one; the reasoning is
:mod:`src.sources.kalshi`'s.

Deferred deliberately, so their absence is a stated choice rather than a gap:
**UFC** (``src.vocab`` has no MMA sport), **NCAA basketball** (no cbb/wcbb
league), **quarter markets** and **second-half markets** (no such
:class:`~src.vocab.Period`).  Their types are simply absent from
:data:`MARKET_TYPES` and are counted by name when they arrive.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.normalize import decimal_to_american, implied_probability, is_plausible_decimal_odds
from src.participants import Participant, canonical_participant, is_pairing
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
    Market,
    Period,
    QuoteStatus,
    Selection,
    Side,
    Sport,
    draw_is_priced,
)
from src.sources._common import (
    Fixture,
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_capture,
    latest_per_endpoint,
    parse_iso_time,
    priced_quote,
    within_schedule_horizon,
)
from src.sources.base import ParseOutcome
from src.sources.guards import (
    CoverageCappedError,
    FormatChangeError,
    SourceError,
    require_list,
    require_mapping,
)

log = logging.getLogger(__name__)

SOURCE_KEY = "polymarket_us"
DEFAULT_BASE_URL = "https://gateway.polymarket.us"

#: Events per request, and how many pages.  Much smaller than the offshore
#: adapter's 40: a ``/v2`` event carries 136-170 markets inline, each with a full
#: team object on *each* side, so three events is already 162 KB and ``limit=200``
#: was met with a TCP reset.  Two pages of 25 is roughly 2.7 MB per league.
DEFAULT_PAGE_SIZE = 25
MAX_PAGES_PER_LEAGUE = 2

_HOME = "home"
_AWAY = "away"


@dataclass(frozen=True)
class LeagueRoute:
    """One gateway league slug, the competition it is, and its team ordering.

    ``ordering`` is which side ``event.teams[0]`` is, and it is declared here
    rather than only read from the payload so that a change upstream is a
    rejection instead of a silently mirrored slate.  See the module docstring,
    trap 4.
    """

    slug: str
    sport: Sport
    league: str
    ordering: str


#: Leagues fetched.  ``ucl`` files under ``SOCCER_OTHER`` because this repository
#: has no Champions League key and inventing one to hold eight fixtures would be
#: a vocabulary change dressed as an adapter.
LEAGUE_ROUTES: tuple[LeagueRoute, ...] = (
    LeagueRoute("mlb", Sport.BASEBALL, "MLB", _AWAY),
    LeagueRoute("wnba", Sport.BASKETBALL, "WNBA", _AWAY),
    LeagueRoute("nba", Sport.BASKETBALL, "NBA", _AWAY),
    LeagueRoute("nhl", Sport.HOCKEY, "NHL", _AWAY),
    LeagueRoute("nfl", Sport.FOOTBALL, "NFL", _AWAY),
    LeagueRoute("epl", Sport.SOCCER, "EPL", _HOME),
    LeagueRoute("mls", Sport.SOCCER, "MLS", _HOME),
    LeagueRoute("ucl", Sport.SOCCER, "SOCCER_OTHER", _HOME),
)

ROUTE_BY_LEAGUE: dict[str, LeagueRoute] = {route.league: route for route in LEAGUE_ROUTES}
ROUTE_BY_SLUG: dict[str, LeagueRoute] = {route.slug: route for route in LEAGUE_ROUTES}

DEFAULT_LEAGUES: tuple[str, ...] = tuple(route.league for route in LEAGUE_ROUTES)


class Scope(StrEnum):
    """Who a market is about — asserted against the payload, never inferred.

    The whole defence against trap 3.  ``sportsMarketType`` names this
    inconsistently, so the name supplies the *claim* and ``metadata`` supplies
    the *evidence*, and they have to agree.
    """

    #: Two competitors, one price each: a moneyline or a spread.
    PAIR = "pair"
    #: Both teams combined, no competitor named: a game total.
    GAME = "game"
    #: One named team, carried by ``metadata.teamId`` and by both sides.
    TEAM = "team"
    #: A three-way result split into one Yes/No contract per outcome.
    DRAWABLE = "drawable"


@dataclass(frozen=True)
class MarketRule:
    market: Market
    period: Period
    scope: Scope


#: ``sportsMarketType`` -> what it settles as.  Every entry below was observed on
#: a live board except the hockey family, which is marked where it appears.
MARKET_TYPES: dict[str, MarketRule] = {
    # ── baseball ─────────────────────────────────────────────────────────────
    "baseball_team_full_game_winner": MarketRule(Market.MONEYLINE, Period.FULL_GAME, Scope.PAIR),
    "baseball_team_full_game_spread": MarketRule(Market.SPREAD, Period.FULL_GAME, Scope.PAIR),
    "baseball_team_full_game_total": MarketRule(Market.TOTAL, Period.FULL_GAME, Scope.GAME),
    "baseball_team_first_five_spread": MarketRule(
        Market.SPREAD, Period.FIRST_5_INNINGS, Scope.PAIR
    ),
    "baseball_team_first_five_total": MarketRule(
        Market.TOTAL, Period.FIRST_5_INNINGS, Scope.GAME
    ),
    # ── basketball ───────────────────────────────────────────────────────────
    "basketball_team_full_game_winner": MarketRule(Market.MONEYLINE, Period.FULL_GAME, Scope.PAIR),
    "basketball_team_full_game_spread": MarketRule(Market.SPREAD, Period.FULL_GAME, Scope.PAIR),
    "basketball_team_full_game_total": MarketRule(Market.TOTAL, Period.FULL_GAME, Scope.GAME),
    # ── football ─────────────────────────────────────────────────────────────
    "football_team_full_game_winner": MarketRule(Market.MONEYLINE, Period.FULL_GAME, Scope.PAIR),
    "football_team_full_game_spread": MarketRule(Market.SPREAD, Period.FULL_GAME, Scope.PAIR),
    # Trap 3 lives in the next two lines.  The first says ``team`` and is the
    # *game* total; the second is the team total.  Do not reorder them without
    # re-reading their ``question`` text on a live board.
    "football_team_full_game_total": MarketRule(Market.TOTAL, Period.FULL_GAME, Scope.GAME),
    "football_team_points_full_game_total": MarketRule(
        Market.TEAM_TOTAL, Period.FULL_GAME, Scope.TEAM
    ),
    "football_team_first_half_spread": MarketRule(Market.SPREAD, Period.FIRST_HALF, Scope.PAIR),
    "football_team_first_half_total": MarketRule(
        Market.TEAM_TOTAL, Period.FIRST_HALF, Scope.TEAM
    ),
    "football_game_first_half_total": MarketRule(Market.TOTAL, Period.FIRST_HALF, Scope.GAME),
    # ── soccer ───────────────────────────────────────────────────────────────
    "soccer_team_full_time_winner": MarketRule(
        Market.MONEYLINE, Period.FULL_GAME, Scope.DRAWABLE
    ),
    # ── hockey: INFERRED, never observed ─────────────────────────────────────
    # The NHL board was empty at every capture (offseason), so these three are
    # named from the cross-sport pattern rather than from bytes.  They are safe
    # to declare because both guards still stand behind them: a type that does
    # not exist simply never matches, and a type whose scope differs is rejected
    # by :func:`_scope_of`.  A 60-minute three-way — a *different contract* from
    # ``FULL_GAME`` per :class:`~src.vocab.Period` — would arrive with three
    # sides and be skipped as ``not_a_two_outcome_market`` rather than published
    # as a two-way.  Re-probe before the season and replace this comment with a
    # measurement.
    "hockey_team_full_game_winner": MarketRule(Market.MONEYLINE, Period.FULL_GAME, Scope.PAIR),
    "hockey_team_full_game_spread": MarketRule(Market.SPREAD, Period.FULL_GAME, Scope.PAIR),
    "hockey_team_full_game_total": MarketRule(Market.TOTAL, Period.FULL_GAME, Scope.GAME),
}

#: What each sport may emit, published by :meth:`capabilities` and re-checked per
#: market so a claim and an emission cannot drift.  Soccer is the live exception
#: in the opposite direction from the offshore adapter: this venue prices the
#: three-way result and prices no soccer spread or total at all.
MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    sport: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}) for sport in Sport
}
MARKETS_BY_SPORT[Sport.FOOTBALL] = frozenset(
    {Market.MONEYLINE, Market.SPREAD, Market.TOTAL, Market.TEAM_TOTAL}
)
MARKETS_BY_SPORT[Sport.SOCCER] = frozenset({Market.MONEYLINE})

#: The market status the venue uses for a market taking orders.
_OPEN_STATUS = "MARKET_STATUS_OPEN"

#: ``sportsMarketTypeV2`` -> the :class:`~src.vocab.Market` it must agree with.
#: A cheap second witness on the mapping above.  It cannot discriminate team from
#: game (both totals are ``SPORTS_MARKET_TYPE_TOTAL``) or one period from
#: another, so it is a check and never the map.
_V2_MARKETS: dict[str, frozenset[Market]] = {
    "SPORTS_MARKET_TYPE_MONEYLINE": frozenset({Market.MONEYLINE}),
    "SPORTS_MARKET_TYPE_SPREAD": frozenset({Market.SPREAD}),
    "SPORTS_MARKET_TYPE_TOTAL": frozenset({Market.TOTAL, Market.TEAM_TOTAL}),
    "SPORTS_MARKET_TYPE_DRAWABLE_OUTCOME": frozenset({Market.MONEYLINE}),
}


def events_endpoint(slug: str, page: int) -> str:
    return f"events:{slug}:{page:02d}"


class PolymarketUSAdapter:
    """Collects Polymarket US's open sports game markets for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        routes: list[LeagueRoute] = []
        for key in leagues:
            route = ROUTE_BY_LEAGUE.get(key)
            if route is None:
                raise KeyError(
                    f"Polymarket US has no league slug for {key!r}; known: "
                    f"{sorted(ROUTE_BY_LEAGUE)}"
                )
            if route not in routes:
                routes.append(route)
        if not routes:
            raise ValueError("PolymarketUSAdapter needs at least one league to collect")
        self._routes: tuple[LeagueRoute, ...] = tuple(routes)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.last_fetch: ScopeTally | None = None
        self._http = SourceClient(source_key, timeout=timeout, client=client)

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return tuple(route.league for route in self._routes)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {route.league: MARKETS_BY_SPORT[route.sport] for route in self._routes},
            self.leagues,
        )

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Open events per league.  *tier* changes nothing: markets arrive inline."""
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for route in self._routes:
            tally.requested(route.slug)
            before = len(raws)
            try:
                self._fetch_league(route, into=raws)
            except SourceError as exc:
                log.warning("%s: league %s failed: %s", self._source_key, route.slug, exc)
                tally.failed(route.slug, exc)
                continue
            pages = raws[before:]
            # An out-of-season league answers 200 with an empty list, which is a
            # produced scope of zero rather than a failure — NHL, NBA and the
            # European competitions are all empty for months at a time, and
            # ``require_something`` still passes because the in-season leagues
            # produced.
            tally.produced(route.slug, sum(_event_count(page) for page in pages))
        tally.require_something(what="open event")
        return raws

    def _fetch_league(
        self, route: LeagueRoute, *, into: list[RawResponse] | None = None
    ) -> list[RawResponse]:
        """Every page of this scope, appended to *into* as they arrive.

        ``into=`` rather than a returned list so a scope that fails part-way
        keeps the pages it already paid for; see :meth:`fetch_raw`.
        """
        pages: list[RawResponse] = [] if into is None else into
        offset = 0
        for page in range(1, MAX_PAGES_PER_LEAGUE + 1):
            raw = self._http.get(
                f"{self.base_url}/v2/leagues/{route.slug}/events",
                endpoint=events_endpoint(route.slug, page),
                params=self._params(offset, self.page_size),
            )
            count = _event_count(raw)
            pages.append(raw)
            if not count:
                break
            offset += count
            if count < self.page_size:
                break
        else:
            self._probe_past_cap(route, offset, into=pages)
        return pages

    def _params(self, offset: int, limit: int) -> dict[str, str]:
        """Query for one page.

        ``type`` and ``section`` are sent explicitly even though they are the
        documented defaults.  ``/v1/markets`` on this host is a futures shelf, so
        a default that changed under us would swap the board for one with no
        game lines in it and nothing in the response would say so.
        """
        return {
            "limit": str(limit),
            "offset": str(offset),
            "type": "sport",
            "section": "general",
        }

    def _probe_past_cap(self, route: LeagueRoute, offset: int, *, into: list[RawResponse]) -> None:
        """Ask for one event past the cap, and report truncation only if it is there.

        The gateway pages by offset and returns no cursor, no total and no
        more-pages flag, so a full last page is the same bytes whether more
        events follow it or none.  Asserting truncation from the cap alone would
        make the claim on nearly every run and so buy nothing for the runs that
        really were short.  One extra request settles it.
        """
        if self.last_fetch is None:
            return
        try:
            probe = self._http.get(
                f"{self.base_url}/v2/leagues/{route.slug}/events",
                endpoint=events_endpoint(route.slug, MAX_PAGES_PER_LEAGUE + 1),
                params=self._params(offset, 1),
            )
        except SourceError as exc:
            self.last_fetch.truncated(
                route.slug,
                CoverageCappedError(
                    f"{self._source_key}: {route.slug} filled all "
                    f"{MAX_PAGES_PER_LEAGUE} permitted pages and the request that "
                    f"would have shown whether more events follow was refused "
                    f"({exc}), so the slate cannot be called complete"
                ),
            )
            return
        events = _events_of(probe)
        if events is None:
            # A body this adapter's own parser would refuse answers nothing, and
            # keeping it would turn a usable partial slate into a source-wide
            # ``FormatChangeError``.  The pages already collected stay; only the
            # unreadable probe is dropped.
            self.last_fetch.truncated(
                route.slug,
                CoverageCappedError(
                    f"{self._source_key}: {route.slug} filled all "
                    f"{MAX_PAGES_PER_LEAGUE} permitted pages and the request that "
                    f"would have shown whether more events follow came back without "
                    f"a readable 'events' list, so the slate cannot be called complete"
                ),
            )
            return
        into.append(probe)
        if not events:
            return
        self.last_fetch.truncated(
            route.slug,
            CoverageCappedError(
                f"{self._source_key}: {route.slug} still had events past offset "
                f"{offset} when the {MAX_PAGES_PER_LEAGUE}-page cap was reached; "
                f"the rest were not collected"
            ),
        )

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_polymarket_us(raws)

    def close(self) -> None:
        self._http.close()


def _events_of(raw: RawResponse) -> list[Any] | None:
    """The ``events`` array, or ``None`` when the body is not the expected shape."""
    try:
        payload = raw.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    events = payload.get("events")
    return events if isinstance(events, list) else None


def _event_count(raw: RawResponse) -> int:
    events = _events_of(raw)
    return 0 if events is None else len(events)


# ── parsing (pure) ───────────────────────────────────────────────────────────


def _slug_of(endpoint: str) -> str:
    parts = endpoint.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "events" else ""


def _stated_route(event: Mapping[str, Any]) -> LeagueRoute | None:
    """The competition the payload itself names, as a route, or ``None``.

    ``teams[].league`` first and only when every team agrees, then ``seriesSlug``
    with its season stripped — the live board states ``nfl-2025`` for a 2026
    fixture, and comparing that verbatim is what rejected 124 real events the
    first time the offshore adapter tried it.

    Abstains rather than guessing, because a competition this adapter does not
    route is one it cannot judge.  The guard exists to catch an event tagged into
    the *wrong* league, not to make the venue spell its leagues our way — and it
    earns its keep because participant resolution is not injective across
    leagues: ``canonical_participant("New York Giants", MLB)`` resolves, to San
    Francisco.
    """
    stated = {
        str(team.get("league")).lower()
        for team in (event.get("teams") or [])
        if isinstance(team, dict) and team.get("league")
    }
    if len(stated) == 1:
        return ROUTE_BY_SLUG.get(stated.pop())
    series = event.get("seriesSlug")
    if not isinstance(series, str) or not series:
        return None
    slug = series.lower()
    route = ROUTE_BY_SLUG.get(slug)
    if route is not None:
        return route
    head, _, tail = slug.rpartition("-")
    return ROUTE_BY_SLUG.get(head) if tail.isdigit() else None


def _resolve_team(team: Mapping[str, Any], competition: Any) -> Participant | None:
    """Resolve one team object, climbing a ladder of the names it states.

    This venue spells a WNBA team's ``name`` as a bare city — ``"Atlanta"``,
    ``"Connecticut"`` — and keeps the nickname in ``alias``.  Measured:
    ``canonical_participant("Atlanta", WNBA)`` is ``None``, so without the second
    rung *every* WNBA event rejects as ``unresolved_participants``.  Every other
    league states a full name and resolves on the first rung, so nothing else
    changes shape.

    ``abbreviation`` is the last rung and is there for one measured case: the
    2026 expansion side Portland Fire arrives with ``alias: ""``, so the two
    middle rungs both collapse back to the bare city and the whole team is
    unresolvable without it.  ``"por"`` resolves.

    Everything below the first rung is safe only because the league is already
    fixed by the route.  ``canonical_participant("Giants", MLB)`` is San
    Francisco and ``("Giants", NFL)`` is New York, so none of these may ever be
    tried against a competition chosen by anything other than the request.
    """
    name = str(team.get("name") or "").strip()
    alias = str(team.get("alias") or "").strip()
    abbreviation = str(team.get("abbreviation") or "").strip()
    candidates = [name]
    if alias and alias.lower() not in name.lower():
        candidates.append(f"{name} {alias}")
    if alias:
        candidates.append(alias)
    if abbreviation:
        candidates.append(abbreviation)
    for candidate in candidates:
        if not candidate:
            continue
        resolved = canonical_participant(candidate, competition)
        if resolved is not None:
            return resolved
    return None


def parse_polymarket_us(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Polymarket US responses into normalized rows."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

    fixtures: dict[str, Fixture] = {}
    teams_by_event: dict[str, dict[int, Participant]] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], Fixture]] = []

    for raw in latest_per_endpoint(latest_capture(raws)):
        route = ROUTE_BY_SLUG.get(_slug_of(raw.endpoint))
        if route is None:
            raise FormatChangeError(
                f"{source}: stored response for unknown league {raw.endpoint!r}"
            )
        payload = require_mapping(raw.json(), source=source, endpoint=raw.endpoint)
        events = require_list(payload.get("events"), source=source, endpoint=raw.endpoint)

        # The response states its own ordering, and it decides which competitor
        # every price on the page attaches to.  Checked against what the route
        # declares rather than simply trusted, because it has no second witness
        # in the payload and a flip would mirror the whole league silently.
        ordering = str((payload.get("league") or {}).get("ordering") or "").strip().lower()
        if ordering and ordering != route.ordering:
            outcome.reject(
                source,
                "league_ordering_disagrees_with_its_route",
                f"{route.slug} states ordering {ordering!r} but this adapter routes it "
                f"as {route.ordering!r}; which side is home cannot be read safely",
            )
            continue

        for event in events:
            if not isinstance(event, dict):
                outcome.skipped["event_not_an_object"] += 1
                continue
            event_id = str(event.get("id") or event.get("slug") or "")
            if not event_id:
                outcome.reject(
                    source, "missing_event_id", f"event without an id in {raw.endpoint}"
                )
                continue
            if event_id in fixtures:
                outcome.skipped["duplicate_event"] += 1
                continue
            stated = _stated_route(event)
            if stated is not None and stated.league != route.league:
                outcome.reject(
                    source,
                    "event_league_disagrees_with_its_route",
                    f"event {event_id} was fetched from the {route.slug!r} league but "
                    f"states {stated.slug!r}",
                    event_id=event_id,
                )
                continue
            accepted = _accept_event(event, route, source, raw.fetched_at, outcome)
            if accepted is None:
                continue
            fixture, by_team_id = accepted
            fixtures[event_id] = fixture
            teams_by_event[event_id] = by_team_id
            work.append((raw, event, fixture))

    if not fixtures:
        return outcome

    event_keys = resolve_doubleheaders(
        {event_id: (f.base_key, f.commence_time) for event_id, f in fixtures.items()}
    )

    for raw, event, fixture in work:
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                outcome.skipped["game_market_not_an_object"] += 1
                continue
            _parse_market(
                market=market,
                raw=raw,
                source=source,
                fixture=fixture,
                by_team_id=teams_by_event[fixture.event_id],
                event_key=event_keys[fixture.event_id],
                outcome=outcome,
            )

    drop_duplicate_selections(source, outcome)
    return outcome


def _accept_event(
    event: Mapping[str, Any],
    route: LeagueRoute,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> tuple[Fixture, dict[int, Participant]] | None:
    """One pregame fixture plus its ``teamId`` -> participant map, or ``None``."""
    event_id = str(event.get("id") or event.get("slug"))
    competition = league_registry.league(route.league)

    if (
        event.get("closed")
        or event.get("archived")
        or event.get("hidden")
        or event.get("active") is False
    ):
        outcome.skipped["event_not_open_pregame"] += 1
        return None
    # ``live`` is ``None`` on a pregame event rather than ``False`` — 23 of 25
    # MLB events on the measured board — so this tests truthiness and must not
    # be written as ``is False``.  ``period`` is *not* a pregame flag: three
    # WNBA events carried ``period: ""`` while pregame.
    if event.get("live") or event.get("ended"):
        outcome.skipped["event_live"] += 1
        return None

    entries = [team for team in (event.get("teams") or []) if isinstance(team, dict)]
    if len(entries) != 2:
        outcome.skipped["missing_home_away"] += 1
        return None
    names = [str(team.get("name") or "").strip() for team in entries]
    if any(is_pairing(name, competition.sport) for name in names):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    resolved = [_resolve_team(team, competition) for team in entries]
    if any(who is None for who in resolved) or resolved[0].key == resolved[1].key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} ({event.get('slug')!r}) in {competition.key}: "
            + ", ".join(
                f"{name!r} -> {who.key if who else None}"
                for name, who in zip(names, resolved)
            ),
            event_id=event_id,
        )
        return None

    # Trap 4: the league states which side ``teams[0]`` is; the team blob's own
    # ``ordering`` is stamped by side index and lies.
    if route.ordering == _AWAY:
        away, home = resolved[0], resolved[1]
    else:
        home, away = resolved[0], resolved[1]

    commence_time = parse_iso_time(event.get("startTime"))
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} has unparseable startTime {event.get('startTime')!r}",
            event_id=event_id,
        )
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None
    if not within_schedule_horizon(commence_time, captured_at, competition):
        outcome.skipped["event_beyond_the_leagues_schedule_horizon"] += 1
        return None

    away_side, home_side = orient(
        away, home, competition, home=home if competition.has_home_away else None
    )
    fixture = Fixture(
        event_id=event_id,
        sport=route.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )

    by_team_id: dict[int, Participant] = {}
    for team, who in zip(entries, resolved):
        try:
            by_team_id[int(team["id"])] = who
        except (KeyError, TypeError, ValueError):
            continue
    return fixture, by_team_id


def _sides(market: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [side for side in (market.get("marketSides") or []) if isinstance(side, dict)]


def _team_id(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _scope_of(market: Mapping[str, Any], sides: Sequence[Mapping[str, Any]]) -> Scope | None:
    """What the *payload* says this market's scope is, or ``None`` if incoherent.

    Trap 3's guard.  ``metadata.teamId`` is present exactly on the team-scoped
    markets and both of their sides repeat it; a game total carries no team
    anywhere; a moneyline or spread carries two different ones.  Compared against
    the scope :data:`MARKET_TYPES` claims, so a renamed type is a rejection.
    """
    metadata = market.get("metadata")
    stated = _team_id(metadata.get("teamId")) if isinstance(metadata, dict) else None
    ids = [_team_id(side.get("teamId")) for side in sides]

    if str(market.get("sportsMarketTypeV2") or "") == "SPORTS_MARKET_TYPE_DRAWABLE_OUTCOME":
        return Scope.DRAWABLE
    if stated is not None:
        return Scope.TEAM if all(team_id == stated for team_id in ids) else None
    if all(team_id is None for team_id in ids):
        return Scope.GAME
    known = [team_id for team_id in ids if team_id is not None]
    if len(known) == len(ids) and len(set(known)) == len(known):
        return Scope.PAIR
    return None


def _quote_price(side: Mapping[str, Any]) -> float | None:
    """The takeable buy price for this side, or ``None``.

    Trap 2: ``quote.value`` is what it costs to buy this side right now — side 0
    at the ask, side 1 at ``1 − bid``, computed by the venue.  A side with no
    quote has no offer resting against it, which is a real and common state.
    """
    quote = side.get("quote")
    if not isinstance(quote, Mapping):
        return None
    try:
        price = float(quote.get("value"))
    except (TypeError, ValueError):
        return None
    return price if 0.0 < price < 1.0 else None


def _selection_for(
    side: Mapping[str, Any], rule: MarketRule, fixture: Fixture, by_team_id: Mapping[int, Participant]
) -> Selection | None:
    """Which canonical selection this side is, read from fields rather than prose."""
    if rule.market in (Market.TOTAL, Market.TEAM_TOTAL):
        label = str(side.get("description") or "").strip().lower()
        if label == "over":
            return Selection.OVER
        return Selection.UNDER if label == "under" else None

    team_id = _team_id(side.get("teamId"))
    if team_id is None:
        # On a three-way soccer contract the missing team *is* the draw.  On
        # anything else it is a side nobody can attribute.
        return Selection.DRAW if rule.scope is Scope.DRAWABLE else None
    who = by_team_id.get(team_id)
    if who is None:
        return None
    if who.key == fixture.home.key:
        return Selection.HOME
    return Selection.AWAY if who.key == fixture.away.key else None


def _parse_market(
    *,
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    by_team_id: Mapping[int, Participant],
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    market_type = str(market.get("sportsMarketType") or "")
    rule = MARKET_TYPES.get(market_type)
    if rule is None:
        # A half or a quarter is one of the kinds collected here, on a scoring
        # window this repository has no vocabulary for.  Filed as a period
        # decision rather than as "this market is out of scope", because the two
        # read very differently to whoever asks why the shelf is short.
        reason = (
            "period_out_of_scope"
            if any(token in market_type for token in ("_quarter_", "_second_half_"))
            else "market_type_out_of_scope"
        )
        outcome.skipped[f"{reason}:{market_type or 'missing'}"] += 1
        return
    if rule.market not in MARKETS_BY_SPORT.get(fixture.sport, frozenset()):
        outcome.skipped[
            f"market_out_of_scope:{fixture.sport.value}:{rule.market.value}"
        ] += 1
        return
    if (
        market.get("closed")
        or market.get("archived")
        or market.get("hidden")
        or not market.get("active", True)
        or str(market.get("status") or _OPEN_STATUS) != _OPEN_STATUS
    ):
        outcome.skipped["market_not_visible"] += 1
        return

    sides = _sides(market)
    if len(sides) != 2:
        outcome.skipped["not_a_two_outcome_market"] += 1
        return

    market_id = str(market.get("id") or "")
    if not market_id:
        outcome.reject(
            source,
            "missing_market_id",
            f"{market_type} market on event {fixture.event_id} has no id",
            event_id=fixture.event_id,
        )
        return

    stated_scope = _scope_of(market, sides)
    if stated_scope is not rule.scope:
        outcome.reject(
            source,
            "market_scope_disagrees_with_its_type",
            f"{market_type} market {market_id} is mapped as {rule.scope.value} but the "
            f"payload reads as {stated_scope.value if stated_scope else 'incoherent'}; "
            f"the type name does not decide this and the mapping must be re-measured",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return
    allowed = _V2_MARKETS.get(str(market.get("sportsMarketTypeV2") or ""))
    if allowed is not None and rule.market not in allowed:
        outcome.reject(
            source,
            "market_scope_disagrees_with_its_type",
            f"{market_type} market {market_id} is mapped as {rule.market.value} but "
            f"states {market.get('sportsMarketTypeV2')!r}",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return

    side_of_total: Side | None = None
    if rule.market is Market.TEAM_TOTAL:
        metadata = market.get("metadata")
        whose = by_team_id.get(_team_id(metadata.get("teamId")) if isinstance(metadata, dict) else None)
        if whose is None:
            outcome.reject(
                source,
                "unknown_team_on_market_side",
                f"team total {market_id} names a team this fixture does not carry",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            return
        side_of_total = Side.HOME if whose.key == fixture.home.key else Side.AWAY

    # Totals share one line stated at market level; a spread's line is per-side
    # and already signed, so it is read inside the loop.  See the module
    # docstring: the market-level ``line`` on a spread is not a per-side quantity
    # and its sign is inconsistent across families.
    shared_line: float | None = None
    if rule.market in MARKETS_REQUIRING_LINE and rule.market is not Market.SPREAD:
        try:
            shared_line = float(market["line"])
        except (KeyError, TypeError, ValueError):
            outcome.reject(
                source,
                "market_without_line",
                f"{market_type} market {market_id} carries no readable line",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            return

    # The three soccer contracts are one market and must group as one, or
    # ``_check_moneyline_completeness`` reports a missing draw leg on each of
    # them.  Same reasoning as kalshi's shared moneyline id.
    group_id = f"moneyline:{fixture.event_id}" if rule.scope is Scope.DRAWABLE else market_id

    for side in sides:
        if rule.scope is Scope.DRAWABLE and not side.get("long"):
            # "No" on *Montreal wins* is draw-or-away — a double chance, not a
            # canonical selection.
            outcome.skipped["combined_outcome_market"] += 1
            continue
        if not side.get("tradable", True):
            outcome.skipped["selection_disabled"] += 1
            continue
        price = _quote_price(side)
        if price is None:
            outcome.skipped["contract_without_a_takeable_offer"] += 1
            continue

        selection = _selection_for(side, rule, fixture, by_team_id)
        if selection is None:
            outcome.reject(
                source,
                "unresolved_outcome",
                f"side {side.get('description')!r} on {market_type} market {market_id} "
                f"names neither a competitor of this fixture nor an over/under",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            continue
        if selection is Selection.DRAW and not draw_is_priced(fixture.sport, rule.period):
            outcome.reject(
                source,
                "draw_not_priced",
                f"a draw outcome on {fixture.sport.value}/{rule.period.value} for event "
                f"{fixture.event_id}",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            continue

        line = shared_line
        if rule.market is Market.SPREAD:
            try:
                line = float(str(side.get("description") or "").strip())
            except (TypeError, ValueError):
                outcome.reject(
                    source,
                    "unreadable_spread_line",
                    f"spread side {side.get('description')!r} on market {market_id} does "
                    f"not read as a signed number",
                    event_id=fixture.event_id,
                    market_id=market_id,
                )
                continue

        decimal_odds = 1.0 / price
        # An extreme is a real resting order on an order-driven venue, not a
        # units error as it would be on a posted sportsbook price.  Counted out
        # of scope rather than rejected, so one lopsided market cannot mark the
        # whole source unhealthy.
        if not is_plausible_decimal_odds(decimal_odds):
            outcome.skipped["price_outside_the_plausible_band"] += 1
            continue
        try:
            outcome.quotes.append(
                priced_quote(
                    fixture,
                    source=source,
                    raw=raw,
                    event_key=event_key,
                    market=rule.market,
                    period=rule.period,
                    selection=selection,
                    side=side_of_total,
                    line=None if line is None else line + 0.0,
                    is_alternate=False,
                    decimal_odds=decimal_odds,
                    american_odds=decimal_to_american(decimal_odds),
                    implied_probability=implied_probability(decimal_odds),
                    source_market_id=group_id,
                    source_selection_id=str(side.get("id") or ""),
                    # The payload states a minimum trade size, not the size
                    # resting at the top of book.  ``None`` is the schema's word
                    # for "unknown", which is the truth here.
                    limit_amount=None,
                    status=QuoteStatus.ACTIVE,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(
                source,
                "invalid_quote",
                f"{rule.market.value}/{selection.value} on {fixture.event_id}: {exc}",
                event_id=fixture.event_id,
                market_id=market_id,
            )
