"""Polymarket: a prediction market whose sports events are fully structured.

``gamma-api.polymarket.com`` is open and unauthenticated.  The plan this work
follows expected prose-titled binaries needing a confidence-scored decomposer,
and recorded that standardised game totals largely did not exist.  Neither is
true of the API as it stands, and the correction matters because it is the
difference between mapping from *fields* and guessing from *titles*:

* an event carries ``teams`` with an explicit ``ordering: "home" | "away"``, so
  which side is home is stated rather than inferred from a slug;
* it carries ``startTime``, so the fixture's clock is a timestamp;
* each market carries ``sportsMarketType`` (``moneyline``, ``spreads``,
  ``totals``) and a numeric ``line``.

Nothing here parses a question.  A market whose type is not one of the three is
counted out of scope by that type, and an event without structured ``teams`` is
skipped rather than having two competitors extracted from its title.

Two things about the prices are structural:

**A binary market has two tokens and the payload quotes one of them.**
``bestAsk`` and ``bestBid`` describe ``outcomes[0]``.  Buying ``outcomes[1]`` is
economically selling ``outcomes[0]``, so its cost is ``1 - bestBid``.  That is a
price you can actually get — a marketable sell at the bid — and it is never
*better* than the second token's own book, so using it understates the edge
rather than inventing one.  Understating is the safe direction: it loses an
opportunity, it cannot manufacture one.

**The fee is charged on entry.**  The market states its own schedule —
``{"rate": 0.05, "exponent": 1, "takerOnly": true}`` — which is
``0.05 × min(p, 1−p)`` per share, paid whether or not the share settles your way.
:mod:`src.commission` models it and :mod:`src.arb` prices every leg net of it.

It remains the weakest of the sources here, and for an honest reason: its slate
is thin, its game markets exist for a subset of fixtures, and its totals ladder
is short.  It is collected because a distinct counterparty that prices a handful
of shared fixtures is still a distinct counterparty.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
from src.normalize import decimal_to_american, implied_probability, is_plausible_decimal_odds
from src.participants import Participant, canonical_participant, is_pairing
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
    Market,
    Period,
    Quote,
    QuoteStatus,
    Selection,
    Sport,
    draw_is_priced,
)
from src.sources._common import (
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_capture,
    latest_per_endpoint,
    parse_iso_time,
    within_schedule_horizon,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError, require_list

log = logging.getLogger(__name__)

SOURCE_KEY = "polymarket"
DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"

#: Events per request, and how many pages.  Deliberately modest: an event object
#: carries every one of its markets inline with full descriptions, so a page of
#: 100 is several megabytes and most of it is props and futures that are counted
#: out of scope on arrival.  Ordered by ``endDate`` ascending, the first pages are
#: the imminent fixtures, which are the only ones another source has also listed.
DEFAULT_PAGE_SIZE = 40
MAX_PAGES_PER_TAG = 2


@dataclass(frozen=True)
class TagRoute:
    """One Gamma tag slug and the league its game events belong to."""

    slug: str
    sport: Sport
    league: str


#: Tags fetched.  One request per tag returns that league's open events.
TAG_ROUTES: tuple[TagRoute, ...] = (
    TagRoute("mlb", Sport.BASEBALL, "MLB"),
    TagRoute("wnba", Sport.BASKETBALL, "WNBA"),
    TagRoute("nba", Sport.BASKETBALL, "NBA"),
    TagRoute("nhl", Sport.HOCKEY, "NHL"),
    TagRoute("nfl", Sport.FOOTBALL, "NFL"),
    TagRoute("epl", Sport.SOCCER, "EPL"),
    TagRoute("mls", Sport.SOCCER, "MLS"),
)

ROUTE_BY_LEAGUE: dict[str, TagRoute] = {route.league: route for route in TAG_ROUTES}
ROUTE_BY_SLUG: dict[str, TagRoute] = {route.slug: route for route in TAG_ROUTES}

DEFAULT_LEAGUES: tuple[str, ...] = tuple(route.league for route in TAG_ROUTES)


@dataclass(frozen=True)
class MarketRule:
    market: Market
    period: Period


#: ``sportsMarketType`` -> what it settles as.  A structured field, so the
#: question text is never read.  ``child_moneyline`` and the ``baseball_team_*``
#: family are per-game or per-period derivatives and are counted out of scope by
#: their own names.
MARKET_TYPES: dict[str, MarketRule] = {
    "moneyline": MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    "spreads": MarketRule(Market.SPREAD, Period.FULL_GAME),
    "totals": MarketRule(Market.TOTAL, Period.FULL_GAME),
}

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    sport: frozenset(rule.market for rule in MARKET_TYPES.values()) for sport in Sport
}
#: Soccer is the exception, and it is a fact about the shape rather than a
#: choice: a soccer result is three-way, and Polymarket splits it into three
#: separate ``Yes``/``No`` contracts — one per outcome — whose ``outcomes`` array
#: is literally ``["Yes", "No"]`` and names no competitor.  Which side a contract
#: is on lives only in its prose title, and this adapter maps from fields.  So no
#: soccer moneyline is collected, and the claim says so: an undeclared absence
#: would be reported as a market that disappeared.
MARKETS_BY_SPORT[Sport.SOCCER] = frozenset({Market.SPREAD, Market.TOTAL})

_HOME = "home"
_AWAY = "away"


def events_endpoint(slug: str, page: int) -> str:
    return f"events:{slug}:{page:02d}"


class PolymarketAdapter:
    """Collects Polymarket's open sports game markets for the configured leagues."""

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
        routes: list[TagRoute] = []
        for key in leagues:
            route = ROUTE_BY_LEAGUE.get(key)
            if route is None:
                raise KeyError(
                    f"Polymarket has no tag for league {key!r}; known: "
                    f"{sorted(ROUTE_BY_LEAGUE)}"
                )
            if route not in routes:
                routes.append(route)
        if not routes:
            raise ValueError("PolymarketAdapter needs at least one league to collect")
        self._routes: tuple[TagRoute, ...] = tuple(routes)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
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
        """Open events per tag.  *tier* changes nothing: markets arrive inline."""
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for route in self._routes:
            tally.requested(route.slug)
            # ``into=raws`` rather than a returned list: a scope that fails
            # part-way through keeps the pages it already fetched.  They were
            # paid for out of somebody else's bandwidth, they are valid, and
            # discarding them turned "page 2 of 3 timed out" into "this whole
            # sport returned nothing" — 4 of 6 requests thrown away on a run
            # that reported OK.
            before = len(raws)
            try:
                self._fetch_tag(route, into=raws)
            except SourceError as exc:
                log.warning("%s: tag %s failed: %s", self._source_key, route.slug, exc)
                tally.failed(route.slug, exc)
                # Counted as failed, not as produced.  The pages it managed to
                # fetch are kept — that is the point of ``into=`` — but a scope
                # that raised has not produced a slate, and letting its partial
                # pages mark it "produced" would satisfy ``require_something``
                # and turn a source that lost every sport into a healthy one.
                continue
            pages = raws[before:]
            tally.produced(route.slug, sum(_event_count(page) for page in pages))
        tally.require_something(what="open event")
        return raws

    def _fetch_tag(
        self, route: TagRoute, *, into: list[RawResponse] | None = None
    ) -> list[RawResponse]:
        """Every page of this scope, appended to *into* as they arrive.

        Appending as it goes rather than returning at the end is what lets a
        failure part-way through keep the pages already fetched; see the caller.
        """
        pages: list[RawResponse] = [] if into is None else into
        offset = 0
        for page in range(1, MAX_PAGES_PER_TAG + 1):
            raw = self._http.get(
                f"{self.base_url}/events",
                endpoint=events_endpoint(route.slug, page),
                params={
                    "tag_slug": route.slug,
                    "closed": "false",
                    "active": "true",
                    "limit": str(self.page_size),
                    "offset": str(offset),
                    "order": "endDate",
                    "ascending": "true",
                },
            )
            count = _event_count(raw)
            # Kept even when it holds nothing: an empty first page is what a
            # renamed field looks like, and the EmptyResponseError that follows
            # carries no payload of its own, so the failure would otherwise
            # arrive with nothing to read.
            pages.append(raw)
            if not count:
                break
            offset += count
            if count < self.page_size:
                break
        return pages

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_polymarket(raws)

    def close(self) -> None:
        self._http.close()


def _event_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    return len(payload) if isinstance(payload, list) else 0


# ── parsing (pure) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Fixture:
    event_id: str
    sport: Sport
    competition: League
    home: Participant
    away: Participant
    commence_time: datetime
    base_key: str


def _stated_league(event: Mapping[str, Any]) -> TagRoute | None:
    """The competition the payload itself names, as a route, or ``None``.

    ``teams[].league`` first, and only when every team agrees, since a
    disagreement between them is not evidence of anything.  ``seriesSlug`` is
    the fallback and is **not** compared verbatim: it carries the season, so the
    live slate states ``mls-2025`` and ``nfl-2026`` against routes named ``mls``
    and ``nfl``.  A string comparison rejected 124 real events on the first live
    run and marked the source unhealthy — the committed capture happens to hold
    only the unsuffixed ``mlb``, so the test written with it passed.

    Resolved to a :class:`TagRoute` rather than compared as text, and ``None``
    when it resolves to nothing: a name this adapter does not route is a name it
    cannot judge, and abstaining is the only honest answer there.  This guard
    exists to catch an event tagged into the *wrong* competition, not to insist
    the venue spell its own competitions the way we do.
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
    # "mls-2025" -> "mls".  Only a trailing season is stripped; anything else
    # is left alone and simply does not resolve.
    head, _, tail = slug.rpartition("-")
    return ROUTE_BY_SLUG.get(head) if tail.isdigit() else None


def parse_polymarket(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Polymarket responses into normalized rows."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

    fixtures: dict[str, _Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], _Fixture]] = []
    for raw in latest_per_endpoint(latest_capture(raws)):
        route = ROUTE_BY_SLUG.get(_slug_of(raw.endpoint))
        if route is None:
            raise FormatChangeError(
                f"{source}: stored response for unknown tag {raw.endpoint!r}"
            )
        events = require_list(raw.json(), source=source, endpoint=raw.endpoint)
        for event in events:
            if not isinstance(event, dict):
                outcome.skipped["event_not_an_object"] += 1
                continue
            event_id = str(event.get("id") or event.get("slug") or "")
            if not event_id:
                outcome.reject(source, "missing_event_id", f"event without an id in {raw.endpoint}")
                continue
            if event_id in fixtures:
                outcome.skipped["duplicate_event"] += 1
                continue
            stated = _stated_league(event)
            if stated is not None and stated.league != route.league:
                # The competition came entirely from the *request's* tag slug,
                # and the payload states it four ways — ``seriesSlug``,
                # ``teams[].league``, ``sport.sport`` and ``tags[].slug``.  All
                # 80 captured events agree with their route, so nothing misfires
                # today; it matters because participant resolution is not
                # injective across leagues.  Measured:
                # ``canonical_participant("San Francisco Giants", NFL)`` is
                # NFL-NYG and ``("New York Giants", MLB)`` is MLB-SF.  So one
                # cross-tagged event would become a wrong-league fixture with
                # entirely *plausible* participants rather than a loud refusal,
                # and nothing downstream could see it.  Kalshi reads its league
                # from the series ticker and SX Bet from ``leagueLabel``; this
                # was the one adapter taking it from what we asked for.
                outcome.reject(
                    source,
                    "event_league_disagrees_with_its_route",
                    f"event {event_id} was fetched from the {route.slug!r} tag but "
                    f"states {stated.slug!r}",
                    event_id=event_id,
                )
                continue
            fixture = _accept_event(event, route, source, raw.fetched_at, outcome)
            if fixture is None:
                continue
            fixtures[event_id] = fixture
            work.append((raw, event, fixture))

    if not fixtures:
        return outcome

    event_keys = resolve_doubleheaders(
        {event_id: (f.base_key, f.commence_time) for event_id, f in fixtures.items()}
    )

    for raw, event, fixture in work:
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                outcome.skipped["market_not_an_object"] += 1
                continue
            _parse_market(
                market=market,
                raw=raw,
                source=source,
                fixture=fixture,
                event_key=event_keys[fixture.event_id],
                outcome=outcome,
            )

    drop_duplicate_selections(source, outcome)
    return outcome


def _slug_of(endpoint: str) -> str:
    parts = endpoint.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "events" else ""


def _accept_event(
    event: Mapping[str, Any],
    route: TagRoute,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> _Fixture | None:
    event_id = str(event.get("id") or event.get("slug"))
    competition = league_registry.league(route.league)

    if event.get("closed") or event.get("archived"):
        outcome.skipped["event_closed"] += 1
        return None
    if event.get("live"):
        outcome.skipped["event_live"] += 1
        return None

    sides = _teams(event)
    if sides is None:
        # A futures or awards event carries no structured ``teams`` with a home
        # and an away side.  Not parsed out of the title: the whole point of this
        # adapter is that identity comes from fields.
        outcome.skipped["event_without_structured_teams"] += 1
        return None
    home_name, away_name = sides
    if any(is_pairing(part, competition.sport) for part in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    home = canonical_participant(home_name, competition)
    away = canonical_participant(away_name, competition)
    if home is None or away is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} ({event.get('slug')!r}) in {competition.key}: "
            f"{away_name!r} -> {away.key if away else None}, "
            f"{home_name!r} -> {home.key if home else None}",
            event_id=event_id,
        )
        return None

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
        # Polymarket lists real fixtures months ahead.  Not a futures leak — two
        # competitors, a real start time — but nothing else here has listed it
        # yet, so it can never be compared.  See within_schedule_horizon.
        outcome.skipped["event_beyond_the_leagues_schedule_horizon"] += 1
        return None

    away_side, home_side = orient(
        away, home, competition, home=home if competition.has_home_away else None
    )
    return _Fixture(
        event_id=event_id,
        sport=route.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _teams(event: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(home name, away name)`` from the event's structured ``teams``."""
    home: list[str] = []
    away: list[str] = []
    for team in event.get("teams") or []:
        if not isinstance(team, dict):
            continue
        name = str(team.get("name") or "").strip()
        ordering = str(team.get("ordering") or "").strip().lower()
        if not name:
            continue
        if ordering == _HOME:
            home.append(name)
        elif ordering == _AWAY:
            away.append(name)
    if len(home) != 1 or len(away) != 1:
        return None
    return home[0], away[0]


def _outcomes(market: Mapping[str, Any]) -> list[str]:
    """``outcomes`` as a list.  Gamma sends it as a JSON string."""
    raw = market.get("outcomes")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []
    return []


def _price(market: Mapping[str, Any], field: str) -> float | None:
    value = market.get(field)
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if 0.0 < price < 1.0 else None


def _selection_for(label: str, fixture: _Fixture) -> Selection | None:
    text = label.strip()
    lowered = text.lower()
    if lowered in ("over", "under"):
        return Selection.OVER if lowered == "over" else Selection.UNDER
    if lowered.startswith("over "):
        return Selection.OVER
    if lowered.startswith("under "):
        return Selection.UNDER
    if lowered == "draw":
        return Selection.DRAW
    who = canonical_participant(text, fixture.competition)
    if who is None:
        return None
    if who.key == fixture.home.key:
        return Selection.HOME
    if who.key == fixture.away.key:
        return Selection.AWAY
    return None


def _parse_market(
    *,
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    market_type = str(market.get("sportsMarketType") or "")
    rule = MARKET_TYPES.get(market_type)
    if rule is None:
        # Same distinction Smarkets draws: a "first five innings" winner, spread
        # or total is one of the four kinds collected here, on a period this
        # adapter does not fetch — a coverage choice, not a fact about the
        # market.  Filed as "out of scope" it read as the opposite.
        reason = (
            "market_in_scope_but_not_fetched"
            if str(market_type or "").startswith("baseball_team_first_five")
            else "market_type_out_of_scope"
        )
        outcome.skipped[f"{reason}:{market_type or 'missing'}"] += 1
        return
    # The same table :meth:`capabilities` publishes, so a claim and what is
    # actually emitted cannot drift apart.  Soccer is the live case: its result
    # is three-way and Polymarket splits it into three Yes/No contracts, so no
    # soccer moneyline is claimed — and without this check a payload change that
    # gave one two competitor-named outcomes would have produced a two-way
    # soccer moneyline the source had promised not to produce.
    if rule.market not in MARKETS_BY_SPORT.get(fixture.sport, frozenset()):
        outcome.skipped[
            f"market_out_of_scope:{fixture.sport.value}:{rule.market.value}"
        ] += 1
        return
    if market.get("closed") or not market.get("active", True):
        outcome.skipped["market_closed"] += 1
        return
    if not market.get("acceptingOrders", True):
        outcome.skipped["market_not_accepting_orders"] += 1
        return

    market_id = str(market.get("id") or market.get("conditionId") or "")
    if not market_id:
        outcome.reject(
            source,
            "missing_market_id",
            f"{market_type} market on event {fixture.event_id} has no id",
            event_id=fixture.event_id,
        )
        return

    labels = _outcomes(market)
    if len(labels) != 2:
        outcome.skipped[f"market_with_{len(labels)}_outcomes"] += 1
        return
    if {label.strip().lower() for label in labels} == {"yes", "no"}:
        # A per-team "will X win" contract inside a multi-market event.  It is
        # typed ``moneyline`` but names no competitor, so which side it is on
        # cannot be read from a field — and this adapter maps from fields.
        outcome.skipped["yes_no_market_names_no_competitor"] += 1
        return

    line: float | None = None
    if rule.market in MARKETS_REQUIRING_LINE:
        try:
            line = float(market["line"])
        except (KeyError, TypeError, ValueError):
            outcome.reject(
                source,
                "market_without_line",
                f"{market_type} market {market_id} carries no readable line",
                event_id=fixture.event_id,
            )
            return

    # ``bestAsk``/``bestBid`` describe the *first* token.  Buying the second is
    # selling the first, so its cost is ``1 - bestBid`` — a price that can be
    # taken, and never better than the second token's own book, so it understates
    # the edge rather than inventing one.
    ask = _price(market, "bestAsk")
    bid = _price(market, "bestBid")
    prices: list[float | None] = [ask, None if bid is None else 1.0 - bid]

    for index, label in enumerate(labels):
        price = prices[index]
        if price is None:
            outcome.skipped["outcome_without_a_takeable_price"] += 1
            continue
        selection = _selection_for(label, fixture)
        if selection is None:
            outcome.reject(
                source,
                "unresolved_outcome",
                f"outcome {label!r} on {market_type} market {market_id} is neither a "
                f"competitor of this fixture nor an over/under label",
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

        # ``line`` is stated from the *first* outcome's perspective on a spread:
        # ``outcomes: ["Cincinnati Reds", "Cleveland Guardians"]`` at line -1.5
        # is the Reds giving 1.5.
        own_line = line
        if line is not None and rule.market is Market.SPREAD and index == 1:
            own_line = -line

        decimal_odds = 1.0 / price
        # An extreme is not corruption on an order-driven venue.  A resting
        # offer at 99.99% or at a tenth of a percent is a real order somebody
        # placed, unlike a sportsbook's posted price where a number outside the
        # band is a units error.  Counted out of scope rather than rejected: a
        # rejection marks the whole source unhealthy for a market working
        # normally, which is what took Smarkets down on its first live run.
        if not is_plausible_decimal_odds(decimal_odds):
            outcome.skipped["price_outside_the_plausible_band"] += 1
            continue
        try:
            outcome.quotes.append(
                Quote(
                    source=source,
                    observed_at=raw.fetched_at,
                    raw_ref=raw.ref,
                    sport=fixture.sport,
                    league=fixture.competition.key,
                    event_key=event_key,
                    source_event_id=fixture.event_id,
                    home_participant=fixture.home.key,
                    away_participant=fixture.away.key,
                    home_team=fixture.home.name,
                    away_team=fixture.away.name,
                    commence_time=fixture.commence_time,
                    market=rule.market,
                    period=rule.period,
                    selection=selection,
                    line=None if own_line is None else own_line + 0.0,
                    is_alternate=False,
                    decimal_odds=decimal_odds,
                    american_odds=decimal_to_american(decimal_odds),
                    implied_probability=implied_probability(decimal_odds),
                    source_market_id=market_id,
                    source_selection_id=str(index),
                    # Gamma states a dollar ``liquidity`` for the market as a
                    # whole rather than the size resting at the top of book, and
                    # the two are different numbers.  ``None`` is the schema's
                    # word for "unknown", which is the truth here.
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
