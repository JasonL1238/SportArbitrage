"""Pinnacle pregame game markets, across all six collected sports.

``guest.api.arcadia.pinnacle.com`` is the unauthenticated endpoint the public
odds page reads from.  Every scope needs **two** calls: ``matchups`` supplies
event identity and ``markets/straight`` supplies prices, joined locally on
``matchupId``.  One adapter owns the book's session, its raw-capture labels and
its parsing rules for all sports, and is parameterized by which leagues to
collect (see :class:`src.sources.base.OddsSource`).

Three properties of this feed drive the whole design, and each one has already
produced a concrete bug:

**Matchups are a tree, not a list.**  ``type == "special"`` entries are props
(player total bases, exact score) — 517 of 528 on one MLB slate.  Worse,
``type == "matchup"`` entries can be *children* of a real game that re-express it
in a different scoring unit: tennis ``units:"Games"`` (297 of 592 tennis matchups
on 2026-07-28), soccer ``units:"Corners"``.  A child carries the same two
participants at the same start time, so accepting it fabricates a doubleheader
*and* files a games handicap as a match moneyline.  Hence
:func:`_accept_matchup` requires ``parentId is None`` **and**
``type == "matchup"``; anything else is a counted skip.

**Periods are numbers whose meaning depends on the sport.**  ``0`` is the full
contest everywhere, but ``1`` is the first five innings in baseball, the first
half in soccer and the first period in hockey, and ``6`` is *regulation* in
hockey (a 3-way market with a real draw) while in basketball the same number
would be the fourth quarter.  A single global table would therefore file a
hockey 60-minute price as a full-game price including the shootout, which is a
different contract and looks like free money when paired.  The table is keyed by
sport: :data:`PERIODS_BY_SPORT`.

**Futures leak in as ordinary-looking markets.**  A market whose ``prices``
carry ``participantId`` and no ``designation`` is an outright — NHL league 1456
publishes three such ``"moneyline"`` markets while having no real games at all.
:func:`_is_outright_priced` catches them before anything else looks at the row.

Prices are **American integers** and ``points`` are plain numbers; unlike Kambi
there is no thousandths scaling here, and applying one would turn a -110 into a
nonsense price.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
from src.normalize import american_to_decimal, implied_probability
from src.participants import Participant, canonical_participant, is_pairing
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
    MARKETS_REQUIRING_SIDE,
    Market,
    Period,
    Quote,
    QuoteStatus,
    Selection,
    Side,
    Sport,
    draw_is_priced,
)
from src.sources.base import ParseOutcome
from src.sources.guards import (
    EmptyResponseError,
    FormatChangeError,
    SourceError,
    check_http_response,
    require_list,
)

log = logging.getLogger(__name__)

SOURCE_KEY = "pinnacle"
DEFAULT_BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"


# ── declarative tables ───────────────────────────────────────────────────────

#: Pinnacle ``sport.id`` -> canonical sport.  From ``GET /sports``.
SPORT_BY_PINNACLE_ID: dict[int, Sport] = {
    3: Sport.BASEBALL,
    4: Sport.BASKETBALL,
    15: Sport.FOOTBALL,
    19: Sport.HOCKEY,
    29: Sport.SOCCER,
    33: Sport.TENNIS,
}

PINNACLE_ID_BY_SPORT: dict[Sport, int] = {
    sport: sport_id for sport_id, sport in SPORT_BY_PINNACLE_ID.items()
}


@dataclass(frozen=True)
class PinnacleLeague:
    """One Pinnacle league whose classification is known up front.

    ``league_key`` is a key in :mod:`src.leagues`.  It may name a league the
    registry does not (yet) carry — see
    :data:`LEAGUE_ROUTES` — in which case the payload is still fetched and
    captured, and its rows are counted as an explicit skip rather than being
    filed under a competition they do not belong to.
    """

    pinnacle_id: int
    sport: Sport
    league_key: str
    pinnacle_name: str


#: The Pinnacle leagues this adapter can classify by id.  Ids are stable for
#: season-long competitions (unlike tennis, where a "league" is one
#: tournament-round and the ids churn weekly — those are classified by name).
#:
#: Hockey league 1602 is the entry worth explaining: on 2026-07-28 Pinnacle's
#: NHL markets are outrights only, and its *only* real hockey games are club
#: friendlies in 1602 — which is also the only place either of hockey's two
#: scoring windows (period 0 including the shootout, period 6 regulation) can be
#: observed.  It maps onto the open-roster ``HOCKEY_OTHER`` competition rather
#: than onto ``NHL``, whose closed roster these clubs are correctly not in.
LEAGUE_ROUTES: tuple[PinnacleLeague, ...] = (
    # ── baseball ─────────────────────────────────────────────────────────────
    PinnacleLeague(246, Sport.BASEBALL, "MLB", "MLB"),
    # ── basketball ───────────────────────────────────────────────────────────
    PinnacleLeague(578, Sport.BASKETBALL, "WNBA", "WNBA"),
    PinnacleLeague(487, Sport.BASKETBALL, "NBA", "NBA"),
    # ── hockey ───────────────────────────────────────────────────────────────
    PinnacleLeague(1456, Sport.HOCKEY, "NHL", "NHL"),
    PinnacleLeague(1602, Sport.HOCKEY, "HOCKEY_OTHER", "World - Club Friendlies"),
    # ── football ─────────────────────────────────────────────────────────────
    # Preseason settles the same way a regular-season game does and the books
    # list it on the same page, so it maps onto the same canonical league.
    PinnacleLeague(889, Sport.FOOTBALL, "NFL", "NFL"),
    PinnacleLeague(4347, Sport.FOOTBALL, "NFL", "NFL Pre Season"),
    # ── soccer ───────────────────────────────────────────────────────────────
    # Matched on id, never on a name substring: Pinnacle also lists
    # "Iceland - Premier League", "Brazil - Serie A" and "Austria - Bundesliga",
    # all of which a substring rule would file as EPL / SERIE_A / BUNDESLIGA.
    PinnacleLeague(1980, Sport.SOCCER, "EPL", "England - Premier League"),
    PinnacleLeague(2663, Sport.SOCCER, "MLS", "USA - Major League Soccer"),
    PinnacleLeague(2196, Sport.SOCCER, "LA_LIGA", "Spain - La Liga"),
    PinnacleLeague(2436, Sport.SOCCER, "SERIE_A", "Italy - Serie A"),
    PinnacleLeague(1842, Sport.SOCCER, "BUNDESLIGA", "Germany - Bundesliga"),
    PinnacleLeague(2036, Sport.SOCCER, "LIGUE_1", "France - Ligue 1"),
)

ROUTE_BY_PINNACLE_ID: dict[int, PinnacleLeague] = {
    route.pinnacle_id: route for route in LEAGUE_ROUTES
}

#: Every soccer competition Pinnacle carries that is not routed above lands
#: here, so a fixture is never dropped merely because its competition is
#: unrecognised.  Identity does not depend on the league, so the catch-all costs
#: nothing but coverage legibility.
SOCCER_CATCH_ALL = "SOCCER_OTHER"

#: Tennis tour classification, by league-name prefix, longest marker first.
#: Pinnacle names a tennis league after the tournament *and round* ("ATP
#: Challenger Bonn - R1", "ITF Women Aldershot - R1", "WTA 125K Vancouver - R1"),
#: and mints new ids per tournament, so the name is the only stable signal.
#: ``"ATP Challenger"`` must be tested before ``"ATP"``.
TENNIS_TOUR_PREFIXES: tuple[tuple[str, str], ...] = (
    ("atp challenger", "ATP_CHALLENGER"),
    ("challenger", "ATP_CHALLENGER"),
    ("itf", "ITF"),
    ("wta", "WTA"),
    ("atp", "ATP"),
)

#: Sports whose leagues are fetched with one ``/sports/{id}/...`` pair rather
#: than one pair per league.  Tennis has 37 leagues with matchups on a single
#: day and soccer 98; per-league iteration would be 270 requests for data the
#: per-sport endpoint returns in two.
SPORT_ENDPOINT_SPORTS: frozenset[Sport] = frozenset({Sport.TENNIS, Sport.SOCCER})

#: Pinnacle ``type`` -> canonical market.
MARKET_TYPES: dict[str, Market] = {
    "moneyline": Market.MONEYLINE,
    "spread": Market.SPREAD,
    "total": Market.TOTAL,
    "team_total": Market.TEAM_TOTAL,
}

#: Which markets are in scope per sport.
#:
#: Tennis is the entry that matters.  A tennis root matchup has
#: ``units: "Sets"``, so its ``spread`` is a **set** handicap (±1.5) and its
#: ``total`` is **total sets** (2.5) — neither is the game-count market the other
#: books quote, and both would be nonsense as a canonical SPREAD/TOTAL beside a
#: soccer or baseball line.  Only the match winner is collected, which is also
#: what :data:`src.vocab.CORE_MARKETS_BY_SPORT` expects of tennis.
MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset(MARKET_TYPES.values()),
    Sport.BASKETBALL: frozenset(MARKET_TYPES.values()),
    Sport.HOCKEY: frozenset(MARKET_TYPES.values()),
    Sport.FOOTBALL: frozenset(MARKET_TYPES.values()),
    Sport.TENNIS: frozenset({Market.MONEYLINE}),
    Sport.SOCCER: frozenset(MARKET_TYPES.values()),
}

#: Pinnacle numeric ``period`` -> canonical period, **per sport**.  A period
#: absent from a sport's map is a narrower or different scoring window and is
#: counted out of scope; it is never coerced onto ``FULL_GAME``.
#:
#: Verified against live payloads on 2026-07-28:
#:
#: * ``0`` is the full contest in every sport (baseball main total median 8.5,
#:   soccer 2.75, hockey moneyline 2-way, tennis match winner).
#: * Baseball ``1`` matched Kambi's labelled "First 5 Innings" markets (total
#:   median 4.5) and ``3`` priced a 0.5 total at -109 plus a three-way
#:   moneyline, which is one inning and not three.
#: * Hockey ``6`` returned ``moneyline`` with home/away/**draw** and totals
#:   around 5.5-6.5 — 60 minutes, three-way.  Hockey ``1`` totals sit at 1.5-2.0,
#:   i.e. one 20-minute period, and are out of scope.
#: * Soccer ``1`` totals have median 1.25 (first half).  Soccer ``8`` is 2-way
#:   with no draw and appears only on cup ties and UEFA qualifiers — an
#:   extra-time / "to qualify" contract, explicitly out of scope, and the single
#:   most dangerous number in this feed to mistake for ``FULL_GAME``.
#: * Tennis ``1`` is the first *set*, not a half.
PERIODS_BY_SPORT: dict[Sport, dict[int, Period]] = {
    Sport.BASEBALL: {0: Period.FULL_GAME, 1: Period.FIRST_5_INNINGS, 3: Period.FIRST_1_INNING},
    Sport.BASKETBALL: {0: Period.FULL_GAME},
    Sport.HOCKEY: {0: Period.FULL_GAME, 6: Period.REGULATION},
    Sport.FOOTBALL: {0: Period.FULL_GAME},
    Sport.TENNIS: {0: Period.FULL_GAME},
    Sport.SOCCER: {0: Period.FULL_GAME},
}

#: Pinnacle ``designation`` -> canonical selection.
DESIGNATIONS: dict[str, Selection] = {
    "home": Selection.HOME,
    "away": Selection.AWAY,
    "draw": Selection.DRAW,
    "over": Selection.OVER,
    "under": Selection.UNDER,
}

#: Every canonical league this adapter knows how to collect from Pinnacle.
#: Tennis tours are included wholesale because Pinnacle's tennis leagues are
#: per-tournament-round; soccer's catch-all is included so unrecognised
#: competitions are collected rather than dropped.
DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB",
    "WNBA",
    "NBA",
    "NHL",
    "HOCKEY_OTHER",
    "NFL",
    "ATP",
    "WTA",
    "ATP_CHALLENGER",
    "ITF",
    "EPL",
    "MLS",
    "LA_LIGA",
    "SERIE_A",
    "BUNDESLIGA",
    "LIGUE_1",
    SOCCER_CATCH_ALL,
)

_CLOSED_STATUSES = frozenset({"closed", "suspended", "cancelled", "canceled", "settled"})

#: Endpoint-label kinds.  The label is part of the stored filename and of every
#: row's ``raw_ref``, so it must be stable across runs and must say which league
#: it came from — otherwise two leagues' captures are indistinguishable on disk
#: and a row cannot be traced back to the request that produced it.
_KIND_MATCHUPS = "matchups"
_KIND_MARKETS = "markets"
_KIND_LEAGUE_INDEX = "leagues"


def league_scope(league_key: str, pinnacle_id: int) -> str:
    """Stable scope token for a one-league request pair."""
    return f"league:{league_key}:{pinnacle_id}"


def sport_scope(sport: Sport, pinnacle_id: int) -> str:
    """Stable scope token for a whole-sport request pair."""
    return f"sport:{sport.value}:{pinnacle_id}"


def canonical_league_key(
    sport: Sport, pinnacle_league_id: int | None, pinnacle_league_name: str | None
) -> str | None:
    """Map one Pinnacle league onto a canonical league key, or ``None``.

    ``None`` means "this competition is not one the pipeline models", which the
    caller turns into a counted skip.  It is never a licence to invent a league:
    a wrong league key would misreport coverage and hand validation the wrong
    schedule horizon and total range.
    """
    if pinnacle_league_id is not None:
        route = ROUTE_BY_PINNACLE_ID.get(pinnacle_league_id)
        if route is not None and route.sport is sport:
            return route.league_key
    if sport is Sport.TENNIS:
        name = (pinnacle_league_name or "").strip().lower()
        for prefix, key in TENNIS_TOUR_PREFIXES:
            if name.startswith(prefix):
                return key
        return None
    if sport is Sport.SOCCER:
        return SOCCER_CATCH_ALL
    return None


# ── the adapter ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Scope:
    """One ``(matchups, markets)`` request pair and its stable label."""

    token: str
    matchups_path: str
    markets_path: str


class PinnacleAdapter:
    """Collects Pinnacle pregame game markets for the configured leagues.

    ``parse`` is deliberately **not** filtered by :attr:`leagues`: it normalizes
    everything the bytes it is handed contain, so replaying a stored capture
    always reproduces the rows that capture produced, whatever the replaying
    instance was constructed with.  Narrowing happens in :meth:`fetch_raw`,
    which only requests the scopes the configuration asks for.
    """

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        max_fallback_leagues: int = 20,
        request_pause: float = 0.1,
    ) -> None:
        keys: list[str] = []
        for key in leagues:
            # Fail at construction rather than mid-run: an unknown key here
            # would otherwise surface as a league that silently collects nothing.
            league_registry.league(key)
            if key not in keys:
                keys.append(key)
        if not keys:
            raise ValueError("PinnacleAdapter needs at least one league to collect")
        self._leagues: tuple[str, ...] = tuple(keys)
        self.base_url = base_url.rstrip("/")
        self.max_fallback_leagues = max_fallback_leagues
        self.request_pause = request_pause
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    @property
    def source_key(self) -> str:
        return SOURCE_KEY

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    @property
    def sports(self) -> tuple[Sport, ...]:
        """The sports the configured leagues span, in enum order."""
        wanted = {league_registry.league(key).sport for key in self._leagues}
        return tuple(sport for sport in Sport if sport in wanted)

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self) -> list[RawResponse]:
        raws: list[RawResponse] = []
        failures: list[SourceError] = []
        for sport in self.sports:
            try:
                raws.extend(self._fetch_sport(sport))
            except SourceError as exc:
                # One sport's endpoint being blocked must not silently truncate
                # the other five, but it must not vanish either.
                log.warning("%s: %s fetch failed: %s", SOURCE_KEY, sport.value, exc)
                failures.append(exc)
        if not raws:
            if failures:
                raise failures[0]
            raise EmptyResponseError(
                f"{SOURCE_KEY}: no matchups returned for any of {list(self.sports)}"
            )
        return raws

    def _fetch_sport(self, sport: Sport) -> list[RawResponse]:
        if sport in SPORT_ENDPOINT_SPORTS and self._prefers_sport_endpoint(sport):
            sport_id = PINNACLE_ID_BY_SPORT[sport]
            scope = _Scope(
                token=sport_scope(sport, sport_id),
                matchups_path=f"/sports/{sport_id}/matchups",
                markets_path=f"/sports/{sport_id}/markets/straight",
            )
            try:
                return self._fetch_scope(scope)
            except SourceError as exc:
                log.warning(
                    "%s: per-sport endpoint for %s failed (%s); falling back to leagues",
                    SOURCE_KEY,
                    sport.value,
                    exc,
                )
                return self._fetch_sport_by_league_index(sport)
        return self._fetch_routed_leagues(sport)

    def _prefers_sport_endpoint(self, sport: Sport) -> bool:
        """Is the whole-sport endpoint the right call for this sport?

        Tennis: always, because its leagues are tournament-rounds with churning
        ids that cannot be enumerated ahead of time.  Soccer: only when the
        catch-all is configured, since that means "every competition"; asking for
        just EPL and MLS is two cheap league pairs instead of a 19 MB download.
        """
        if sport is Sport.TENNIS:
            return True
        return SOCCER_CATCH_ALL in self._leagues

    def _routes_for(self, sport: Sport) -> tuple[PinnacleLeague, ...]:
        """Routed leagues to fetch for *sport*.

        A route whose canonical key is not in the registry is still fetched: its
        payload is real data the registry cannot yet file, and capturing it is
        what makes the gap visible (as a ``league_not_registered`` skip) instead
        of imaginary.  Every route currently maps onto a registered league, so
        this clause is a forward guard rather than a live code path.
        """
        return tuple(
            route
            for route in LEAGUE_ROUTES
            if route.sport is sport
            and (
                route.league_key in self._leagues
                or not league_registry.is_known(route.league_key)
            )
        )

    def _fetch_routed_leagues(self, sport: Sport) -> list[RawResponse]:
        raws: list[RawResponse] = []
        failures: list[SourceError] = []
        for route in self._routes_for(sport):
            scope = _Scope(
                token=league_scope(route.league_key, route.pinnacle_id),
                matchups_path=f"/leagues/{route.pinnacle_id}/matchups",
                markets_path=f"/leagues/{route.pinnacle_id}/markets/straight",
            )
            try:
                raws.extend(self._fetch_scope(scope))
            except SourceError as exc:
                log.warning("%s: league %s failed: %s", SOURCE_KEY, route.pinnacle_id, exc)
                failures.append(exc)
        if not raws and failures:
            raise failures[0]
        return raws

    def _fetch_sport_by_league_index(self, sport: Sport) -> list[RawResponse]:
        """Degraded path: enumerate the sport's leagues and fetch the busiest.

        Bounded on purpose.  Soccer has 98 leagues with fixtures on one day, and
        196 requests to work around one failed call is not politeness, it is a
        small scrape.  What is left out is logged rather than implied.
        """
        sport_id = PINNACLE_ID_BY_SPORT[sport]
        index = self._get(
            f"/sports/{sport_id}/leagues",
            f"{_KIND_LEAGUE_INDEX}:{sport_scope(sport, sport_id)}",
        )
        listed = require_list(index.json(), source=SOURCE_KEY, endpoint=index.endpoint)
        entries = [
            entry
            for entry in listed
            if isinstance(entry, dict)
            and isinstance(entry.get("id"), int)
            and entry.get("matchupCount")
        ]
        ordered = sorted(entries, key=lambda entry: (-int(entry["matchupCount"]), int(entry["id"])))
        chosen = ordered[: self.max_fallback_leagues]
        if len(ordered) > len(chosen):
            log.warning(
                "%s: %s fallback covers %d of %d leagues with fixtures; %d omitted",
                SOURCE_KEY,
                sport.value,
                len(chosen),
                len(ordered),
                len(ordered) - len(chosen),
            )
        raws: list[RawResponse] = [index]
        for entry in chosen:
            pinnacle_id = int(entry["id"])
            key = canonical_league_key(sport, pinnacle_id, entry.get("name")) or "UNMAPPED"
            scope = _Scope(
                token=league_scope(key, pinnacle_id),
                matchups_path=f"/leagues/{pinnacle_id}/matchups",
                markets_path=f"/leagues/{pinnacle_id}/markets/straight",
            )
            try:
                raws.extend(self._fetch_scope(scope))
            except SourceError as exc:
                log.warning("%s: fallback league %s failed: %s", SOURCE_KEY, pinnacle_id, exc)
        return raws

    def _fetch_scope(self, scope: _Scope) -> list[RawResponse]:
        """Fetch one matchups/markets pair, or nothing if the scope is idle.

        An empty ``matchups`` array is a real off day for that league, not a
        failure, so it returns no responses instead of raising — otherwise one
        out-of-season league would abort the whole book.  A blocked, non-JSON or
        structurally changed response still raises, because that *is* a failure
        and must not look like an off day.  :meth:`fetch_raw` raises if every
        scope came back idle.
        """
        matchups = self._get(scope.matchups_path, f"{_KIND_MATCHUPS}:{scope.token}")
        listed = require_list(matchups.json(), source=SOURCE_KEY, endpoint=matchups.endpoint)
        if not listed:
            log.info("%s: %s returned no matchups", SOURCE_KEY, scope.token)
            return []
        markets = self._get(scope.markets_path, f"{_KIND_MARKETS}:{scope.token}")
        require_list(markets.json(), source=SOURCE_KEY, endpoint=markets.endpoint)
        return [matchups, markets]

    def _get(self, path: str, endpoint: str) -> RawResponse:
        if self.request_pause:
            time.sleep(self.request_pause)
        response = self._client.get(f"{self.base_url}{path}")
        raw = RawResponse(
            source=SOURCE_KEY,
            endpoint=endpoint,
            url=str(response.request.url),
            status_code=response.status_code,
            body=response.text,
            fetched_at=datetime.now(UTC),
            content_type=response.headers.get("content-type"),
            headers=RawResponse.clean_headers(response.headers),
        )
        check_http_response(
            source=SOURCE_KEY,
            endpoint=endpoint,
            status_code=raw.status_code,
            body=raw.body,
            content_type=raw.content_type,
            url=raw.url,
        )
        return raw

    # ── parse ────────────────────────────────────────────────────────────────

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_pinnacle(raws)

    def close(self) -> None:
        self._client.close()


# ── parsing (pure) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Game:
    """One accepted matchup: an event identity ready to hang prices on."""

    matchup_id: str
    sport: Sport
    competition: League
    home: Participant
    away: Participant
    book_home_key: str
    """Key of the participant **Pinnacle** labelled ``alignment: "home"``.

    Not always :attr:`home`.  Where a league has no real home side — tennis —
    :func:`src.events.orient` imposes its own ordering by participant key, and it
    disagrees with Pinnacle's arbitrary ordering about half the time.  A price
    arrives tagged with Pinnacle's *own* designation, so translating that
    designation needs this, not :attr:`home`."""
    commence_time: datetime
    base_key: str
    status: Any


def parse_pinnacle(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Join every captured matchups/markets pair into normalized quotes.

    Pure: a function of the bytes handed in.  No network, no clock, no
    filesystem, no configuration — parsing the same responses twice yields
    identical rows.
    """
    outcome = ParseOutcome()
    pairs = _pair_responses(raws, outcome)

    # Pass 1: event identity, across every pair at once, so a matchup that two
    # scopes both returned is accepted once and doubleheader ordinals are
    # numbered over the whole slate rather than per request.
    games: dict[str, _Game] = {}
    listed_ids: set[str] = set()
    for _, matchups_raw, _ in pairs:
        for matchup in _iter_records(matchups_raw, outcome):
            matchup_id = str(matchup.get("id") or "")
            listed_ids.add(matchup_id)
            if matchup_id in games:
                outcome.skipped["duplicate_matchup"] += 1
                continue
            game = _accept_matchup(matchup, outcome)
            if game is None:
                continue  # already counted as a skip or a rejection
            games[game.matchup_id] = game

    event_keys = resolve_doubleheaders(
        {mid: (game.base_key, game.commence_time) for mid, game in games.items()}
    )

    # Pass 2: prices.
    priced: set[tuple[str, str]] = set()
    for _, matchups_raw, markets_raw in pairs:
        price_ref, identity_ref = provenance(markets_raw, matchups_raw)
        for market in _iter_records(markets_raw, outcome):
            _parse_market(
                market=market,
                games=games,
                listed_ids=listed_ids,
                event_keys=event_keys,
                markets_raw=markets_raw,
                raw_ref=price_ref,
                identity_raw_ref=identity_ref,
                priced=priced,
                outcome=outcome,
            )
    return outcome


def provenance(markets_raw: RawResponse, matchups_raw: RawResponse) -> tuple[str, str]:
    """Provenance for one row: ``(price_ref, identity_ref)``.

    A Pinnacle row is built from two captures, and pointing only at
    ``markets/straight`` — which is what this adapter used to do — made the
    response that supplied the teams, the start time and therefore the whole
    ``event_key`` unreferenced and unauditable.  If the matchups payload mislabels
    a home side, nothing on the row said which bytes to go and look at.

    The two refs go into two *separate* schema fields, ``raw_ref`` and
    ``identity_raw_ref``.  They were briefly packed into one ``"price+identity"``
    string, which is worse than it looks: it gives ``raw_ref`` an undeclared
    grammar, so any check that compares it against the set of stored refs — as the
    source contract's does — sees a concatenation matching nothing and reports the
    row as orphaned.
    """
    return markets_raw.ref, matchups_raw.ref


def _pair_responses(
    raws: Sequence[RawResponse], outcome: ParseOutcome
) -> list[tuple[str, RawResponse, RawResponse]]:
    """Group captured responses into ``(scope, matchups, markets)`` triples."""
    grouped: dict[str, dict[str, RawResponse]] = {}
    for raw in raws:
        kind, _, token = raw.endpoint.partition(":")
        if kind == _KIND_LEAGUE_INDEX:
            # The league listing is captured for provenance but carries no odds.
            outcome.skipped["league_index_response"] += 1
            continue
        if kind not in (_KIND_MATCHUPS, _KIND_MARKETS) or not token:
            outcome.skipped[f"unrecognised_response:{raw.endpoint}"] += 1
            continue
        slot = grouped.setdefault(token, {})
        previous = slot.get(kind)
        if previous is None or raw.fetched_at > previous.fetched_at:
            if previous is not None:
                outcome.skipped[f"superseded_response:{kind}"] += 1
            slot[kind] = raw
        else:
            outcome.skipped[f"superseded_response:{kind}"] += 1

    pairs: list[tuple[str, RawResponse, RawResponse]] = []
    for token in sorted(grouped):
        slot = grouped[token]
        missing = [kind for kind in (_KIND_MATCHUPS, _KIND_MARKETS) if kind not in slot]
        if missing:
            # In scope but unusable: half a pair cannot be joined, and silence
            # here would look exactly like a league with no fixtures.
            outcome.reject(
                SOURCE_KEY,
                "unpaired_response",
                f"scope {token!r} has no {missing[0]} response to join against",
                scope=token,
            )
            continue
        pairs.append((token, slot[_KIND_MATCHUPS], slot[_KIND_MARKETS]))

    if not pairs:
        raise FormatChangeError(
            f"{SOURCE_KEY}: no matchups/markets pair among "
            f"{[raw.endpoint for raw in raws]}"
        )
    return pairs


def _iter_records(raw: RawResponse, outcome: ParseOutcome) -> Iterable[dict[str, Any]]:
    payload = require_list(raw.json(), source=SOURCE_KEY, endpoint=raw.endpoint)
    for entry in payload:
        if isinstance(entry, dict):
            yield entry
        else:
            outcome.skipped[f"non_object_record:{raw.endpoint.partition(':')[0]}"] += 1


# ── event identity ───────────────────────────────────────────────────────────


def _accept_matchup(matchup: dict[str, Any], outcome: ParseOutcome) -> _Game | None:
    """Turn one matchup into a :class:`_Game`, or count why it was dropped."""
    matchup_id = str(matchup.get("id") or "")
    if not matchup_id:
        outcome.reject(
            SOURCE_KEY, "missing_matchup_id", f"matchup without an id: {sorted(matchup)[:10]}"
        )
        return None

    kind = str(matchup.get("type") or "")
    if kind != "matchup":
        # Props and outrights: "special" entries such as player total bases or
        # "Place 1st".  517 of 528 on one MLB slate.
        outcome.skipped[f"matchup_type:{kind or 'missing'}"] += 1
        return None

    if matchup.get("parentId") is not None:
        # THE guard.  A child re-expresses the same fixture in another scoring
        # unit — tennis "Games", soccer "Corners" — with the same two names at
        # the same start time.  Accepting one invents a second game between the
        # same players and files its handicap as a match moneyline.
        outcome.skipped[f"child_matchup:units:{matchup.get('units') or 'unknown'}"] += 1
        return None

    if matchup.get("isLive"):
        outcome.skipped["live_matchup"] += 1
        return None

    league_info = matchup.get("league") or {}
    sport_id = (league_info.get("sport") or {}).get("id")
    sport = SPORT_BY_PINNACLE_ID.get(sport_id) if isinstance(sport_id, int) else None
    if sport is None:
        outcome.skipped[f"sport_unmapped:{sport_id}"] += 1
        return None

    pinnacle_league_id = league_info.get("id")
    league_key = canonical_league_key(
        sport,
        pinnacle_league_id if isinstance(pinnacle_league_id, int) else None,
        league_info.get("name"),
    )
    if league_key is None:
        outcome.skipped[f"league_unmapped:{sport.value}:{pinnacle_league_id}"] += 1
        return None
    if not league_registry.is_known(league_key):
        # Pinnacle carries a competition src.leagues does not model.  Counted,
        # not guessed at: filing it under a neighbouring league would hand
        # validation the wrong horizon and the wrong plausible total range.
        outcome.skipped[f"league_not_registered:{league_key}"] += 1
        return None
    competition = league_registry.league(league_key)

    sides = _aligned_participants(matchup)
    if sides is None:
        outcome.reject(
            SOURCE_KEY,
            "unaligned_participants",
            f"matchup {matchup_id} does not have exactly one home and one away participant: "
            f"{[(p.get('alignment'), p.get('name')) for p in matchup.get('participants') or []]}",
            matchup_id=matchup_id,
        )
        return None
    home_name, away_name = sides

    # A doubles entry names two players a side ("Alvarez / Magadan"), so it is
    # deliberately unresolvable — but it is a market this collector does not cover,
    # not a participant it failed to recognise.  Grading it a rejection failed the
    # entire Pinnacle run over one doubles match in the tennis slate, discarding
    # 2300 good rows across five other sports.
    if any(is_pairing(name) for name in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    resolved = [canonical_participant(name, competition) for name in (home_name, away_name)]
    if any(participant is None for participant in resolved):
        outcome.reject(
            SOURCE_KEY,
            "unknown_participant",
            f"matchup {matchup_id} in {competition.key} has a participant that did not "
            f"resolve: {[home_name, away_name]}",
            matchup_id=matchup_id,
            league=competition.key,
        )
        return None
    first, second = resolved[0], resolved[1]
    assert first is not None and second is not None  # narrowed above
    if first.key == second.key:
        outcome.reject(
            SOURCE_KEY,
            "duplicate_participant",
            f"matchup {matchup_id} resolved both sides to {first.key}: {[home_name, away_name]}",
            matchup_id=matchup_id,
        )
        return None

    # Tennis has no home player: each book orders the two names as it likes, so
    # the ordering is imposed rather than trusted.  Team sports pass the book's
    # own home side through.
    if competition.has_home_away:
        away, home = orient(first, second, competition, home=first)
    else:
        away, home = orient(first, second, competition)

    commence_time = _parse_time(matchup.get("startTime"))
    if commence_time is None:
        outcome.reject(
            SOURCE_KEY,
            "missing_commence_time",
            f"matchup {matchup_id} has unparseable startTime {matchup.get('startTime')!r}",
            matchup_id=matchup_id,
        )
        return None

    return _Game(
        matchup_id=matchup_id,
        sport=sport,
        competition=competition,
        home=home,
        away=away,
        book_home_key=first.key,
        commence_time=commence_time,
        base_key=build_event_key(away.key, home.key, commence_time, competition),
        status=matchup.get("status"),
    )


def _aligned_participants(matchup: dict[str, Any]) -> tuple[str, str] | None:
    """``(home_name, away_name)`` when exactly one of each is present."""
    entries = [p for p in matchup.get("participants") or [] if isinstance(p, dict)]
    found: dict[str, list[str]] = {"home": [], "away": []}
    for participant in entries:
        alignment = str(participant.get("alignment") or "")
        name = participant.get("name")
        if alignment in found and isinstance(name, str) and name.strip():
            found[alignment].append(name)
    if len(found["home"]) != 1 or len(found["away"]) != 1:
        return None
    return found["home"][0], found["away"][0]


# ── prices ───────────────────────────────────────────────────────────────────


def _is_participant_priced(market: dict[str, Any]) -> bool:
    """Is this market priced per competitor rather than per matchup side?

    Its ``prices`` name a ``participantId`` and carry no ``designation``, which
    is the shape of both an **outright** and a **player prop**.  NHL league 1456
    publishes three outrights as ``type: "moneyline"``, period 0 — while having
    no real games at all — and NBA league 487 five more; a full MLB slate carries
    several hundred prop markets in the same shape.  Nothing downstream could
    tell such a row from a genuine two-way moneyline (there is no designation to
    map, so every price would land on the same selection), so it is stopped here
    and counted, whether or not its matchup was accepted.
    """
    prices = market.get("prices") or []
    if not prices:
        return False
    return all(
        isinstance(price, dict) and "participantId" in price and "designation" not in price
        for price in prices
    )


def _parse_market(
    *,
    market: dict[str, Any],
    games: dict[str, _Game],
    listed_ids: set[str],
    event_keys: dict[str, str],
    markets_raw: RawResponse,
    raw_ref: str,
    identity_raw_ref: str | None,
    priced: set[tuple[str, str]],
    outcome: ParseOutcome,
) -> None:
    matchup_id = str(market.get("matchupId") or "")
    participant_priced = _is_participant_priced(market)

    game = games.get(matchup_id)
    if game is None:
        if participant_priced:
            # Outrights and player props.  Separated from the game-market count
            # so the NHL's futures-only "moneylines" are countable on their own.
            outcome.skipped["participant_priced_market:on_out_of_scope_matchup"] += 1
        elif matchup_id in listed_ids:
            outcome.skipped["market_on_out_of_scope_matchup"] += 1
        else:
            # The two calls are seconds apart, so a matchup can appear in one and
            # not the other.  Counted rather than rejected: with no identity
            # payload there is no evidence the market was ever in scope.
            outcome.skipped["market_without_listed_matchup"] += 1
        return

    if participant_priced:
        # A participant-priced market hanging off a *real game* is the dangerous
        # case: the matchup guard cannot catch it, and this counter is expected
        # to stay at zero.
        outcome.skipped["participant_priced_market:on_game_matchup"] += 1
        return

    market_type = MARKET_TYPES.get(str(market.get("type") or ""))
    if market_type is None:
        outcome.skipped[f"market_type:{market.get('type')}"] += 1
        return
    if market_type not in MARKETS_BY_SPORT[game.sport]:
        outcome.skipped[
            f"market_out_of_scope:{game.sport.value}:{market_type.value}"
        ] += 1
        return

    period_raw = market.get("period")
    if not isinstance(period_raw, int) or isinstance(period_raw, bool):
        outcome.reject(
            SOURCE_KEY,
            "unparseable_period",
            f"period {period_raw!r} on {market_type.value} for matchup {matchup_id}",
            matchup_id=matchup_id,
            market_key=market.get("key"),
        )
        return
    period = PERIODS_BY_SPORT[game.sport].get(period_raw)
    if period is None:
        outcome.skipped[f"period_out_of_scope:{game.sport.value}:{period_raw}"] += 1
        return

    side: Side | None = None
    if market_type in MARKETS_REQUIRING_SIDE:
        side_raw = str(market.get("side") or "")
        if side_raw not in ("home", "away"):
            outcome.reject(
                SOURCE_KEY,
                "missing_side",
                f"{market_type.value} without a home/away side on matchup {matchup_id}",
                matchup_id=matchup_id,
                side=market.get("side"),
            )
            return
        side = Side(side_raw)

    market_id = str(market.get("key") or "").strip() or _fallback_market_id(matchup_id, market)
    if (matchup_id, market_id) in priced:
        # The same market captured twice in one parse — a league fetched both on
        # its own and inside its sport's payload.  Pricing it again would collide
        # on dedup_key and abort the whole run's insert.
        outcome.skipped["duplicate_market"] += 1
        return
    priced.add((matchup_id, market_id))

    is_alternate = bool(market.get("isAlternate"))
    status = _status(game.status, market.get("status"))
    limit_amount = _max_risk(market.get("limits") or [])

    for price in market.get("prices") or []:
        if not isinstance(price, dict):
            outcome.skipped["non_object_price"] += 1
            continue
        quote = _build_quote(
            price=price,
            game=game,
            event_key=event_keys[matchup_id],
            market_type=market_type,
            period=period,
            side=side,
            is_alternate=is_alternate,
            status=status,
            limit_amount=limit_amount,
            market_id=market_id,
            observed_at=markets_raw.fetched_at,
            raw_ref=raw_ref,
            identity_raw_ref=identity_raw_ref,
            outcome=outcome,
        )
        if quote is not None:
            outcome.quotes.append(quote)


def _other_key(game: _Game) -> str:
    """The participant Pinnacle labelled ``away`` — whichever of ours that is."""
    return game.away.key if game.book_home_key == game.home.key else game.home.key


def _fallback_market_id(matchup_id: str, market: dict[str, Any]) -> str:
    """Market identity when Pinnacle omits its own ``key``.

    ``market_key`` is ``(source, source_event_id, source_market_id)`` and must
    name **exactly one** market.  The previous fallback was
    ``matchup:type:period``, which fused the home and away team totals of a game
    into one market and every alternate line of a spread into one more — so a
    "complete market" was seven lines deep and its implied probabilities summed
    to three.  Side, line and the alternate flag are what separate them, so all
    three are in here, along with the period they settle on.
    """
    points = sorted(
        float(price["points"])
        for price in market.get("prices") or []
        if isinstance(price, dict) and isinstance(price.get("points"), (int, float))
    )
    return "|".join(
        (
            "fallback",
            matchup_id,
            str(market.get("type") or ""),
            str(market.get("period")),
            str(market.get("side") or ""),
            ",".join(f"{value + 0.0:g}" for value in points),
            "alt" if market.get("isAlternate") else "main",
        )
    )


def _build_quote(
    *,
    price: dict[str, Any],
    game: _Game,
    event_key: str,
    market_type: Market,
    period: Period,
    side: Side | None,
    is_alternate: bool,
    status: QuoteStatus,
    limit_amount: float | None,
    market_id: str,
    observed_at: datetime,
    raw_ref: str,
    identity_raw_ref: str | None,
    outcome: ParseOutcome,
) -> Quote | None:
    designation = str(price.get("designation") or "")
    selection = DESIGNATIONS.get(designation)

    # Translate the book's own side label through the orientation this row
    # actually uses.  `DESIGNATIONS` maps Pinnacle's "home" to `Selection.HOME`,
    # which is only right when Pinnacle's home side *is* our home side.
    #
    # For tennis it is right about half the time.  There is no home player, so
    # `src.events.orient` imposes an ordering by participant key while Pinnacle
    # orders the two names however it likes; when the two disagree the row already
    # names the correct `home_team` but the price attached to it belonged to the
    # other player.  Observed on the captured slate: 5 of 8 cross-book matches had
    # Pinnacle's prices mirrored against FanDuel's — Tommy Paul priced at 4.12
    # where FanDuel had him at 1.244.  Both are plausible prices, nothing on the
    # row looks wrong, and a position built from the pair backs the same player
    # twice while reporting a guaranteed profit.  That is a phantom arbitrage in
    # its purest form.
    if selection in (Selection.HOME, Selection.AWAY):
        priced_participant = (
            game.book_home_key if selection is Selection.HOME else _other_key(game)
        )
        selection = (
            Selection.HOME if priced_participant == game.home.key else Selection.AWAY
        )

    if selection is None:
        outcome.reject(
            SOURCE_KEY,
            "unknown_selection",
            f"designation {designation!r} on {market_type.value} for matchup "
            f"{game.matchup_id}",
            matchup_id=game.matchup_id,
            market_id=market_id,
        )
        return None

    if selection is Selection.DRAW and not draw_is_priced(game.sport, period):
        # A third runner on a window that cannot be drawn is a misparse, and the
        # two readings differ by the whole stake.  Hockey period 6 prices a draw;
        # hockey period 0, decided by the shootout, does not.
        outcome.reject(
            SOURCE_KEY,
            "draw_not_priced",
            f"draw priced on {game.sport.value}/{period.value} for matchup "
            f"{game.matchup_id}",
            matchup_id=game.matchup_id,
            market_id=market_id,
        )
        return None

    american = price.get("price")
    if american is None:
        outcome.skipped["price_without_odds"] += 1
        return None

    points = price.get("points")
    if market_type in MARKETS_REQUIRING_LINE and points is None:
        outcome.reject(
            SOURCE_KEY,
            "missing_line",
            f"{market_type.value} price without points on matchup {game.matchup_id}",
            matchup_id=game.matchup_id,
            market_id=market_id,
        )
        return None

    try:
        decimal_odds = american_to_decimal(american)
        # -0.0 is real in Pinnacle payloads (a pick'em handicap prices the away
        # side at -0.0); normalizing the sign keeps a line and its mirror
        # comparing and serializing identically.
        line = None if market_type not in MARKETS_REQUIRING_LINE else float(points) + 0.0
        return Quote(
            source=SOURCE_KEY,
            observed_at=observed_at,
            raw_ref=raw_ref,
            identity_raw_ref=identity_raw_ref,
            sport=game.sport,
            league=game.competition.key,
            event_key=event_key,
            source_event_id=game.matchup_id,
            home_participant=game.home.key,
            away_participant=game.away.key,
            home_team=game.home.name,
            away_team=game.away.name,
            commence_time=game.commence_time,
            market=market_type,
            period=period,
            selection=selection,
            side=side,
            line=line,
            is_alternate=is_alternate,
            decimal_odds=decimal_odds,
            american_odds=int(american),
            implied_probability=implied_probability(decimal_odds),
            source_market_id=market_id,
            source_selection_id=designation,
            limit_amount=limit_amount,
            status=status,
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            SOURCE_KEY,
            "invalid_quote",
            f"{market_type.value}/{selection.value} on matchup {game.matchup_id}: {exc}",
            matchup_id=game.matchup_id,
            market_id=market_id,
        )
        return None


def _status(matchup_status: Any, market_status: Any) -> QuoteStatus:
    for value in (market_status, matchup_status):
        if str(value or "").lower() in _CLOSED_STATUSES:
            return QuoteStatus.SUSPENDED
    return QuoteStatus.ACTIVE


def _max_risk(limits: Any) -> float | None:
    if not isinstance(limits, list):
        return None
    for limit in limits:
        if isinstance(limit, dict) and limit.get("type") == "maxRiskStake":
            try:
                return float(limit["amount"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
