"""DraftKings US sportsbook pregame markets, from the public sportsbook APIs.

The legacy ``api/v5/eventgroups/{id}`` payload remains supported for replay.
Live Illinois pages now read ``api/sportscontent/.../leagueSubcategory/v1``;
that endpoint is used for leagues whose current Game Lines subcategory has been
observed.  No account is involved.

The Akamai edge rejects a plain HTTP client even from Illinois.  When no client
was injected, collection therefore retries the current endpoint through the
project's browser transport after seeding the public league page.  Parsing stays
pure and supports both generations of captured bytes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
from src.normalize import (
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import Participant, canonical_participant, is_pairing
from src.raw_store import RawResponse
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Sport
from src.sources._common import (
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_per_endpoint,
    parse_iso_time,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "draftkings"
DEFAULT_BASE_URL = "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/v5"
DEFAULT_CONTENT_BASE_URL = (
    "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/"
    "sportscontent/controldata/league/leagueSubcategory/v1"
)
DEFAULT_ORIGIN = "https://sportsbook.draftkings.com/"
HOST_INTERVAL = 0.5

#: Event-group id → (sport, league key).  One request covers the whole league.
EVENT_GROUPS: tuple[tuple[int, Sport, str], ...] = (
    (84240, Sport.BASEBALL, "MLB"),
    (94682, Sport.BASKETBALL, "WNBA"),
    (42648, Sport.BASKETBALL, "NBA"),
    (88808, Sport.FOOTBALL, "NFL"),
    (42133, Sport.HOCKEY, "NHL"),
    (40253, Sport.SOCCER, "EPL"),
)

#: Current public page route and Game Lines subcategory.  IDs are discovered
#: from the page's own request, not inferred from the retired v5 endpoint.
CONTENT_ROUTES: dict[int, tuple[str, str]] = {
    84240: ("baseball/mlb", "4519"),
    88808: ("football/nfl", "10500"),
}

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.SOCCER: frozenset({Market.MONEYLINE, Market.TOTAL}),
}

#: DK market ``label`` → (Market, Period).  Only full-game Game Lines.
MARKET_LABELS: dict[str, tuple[Market, Period]] = {
    "moneyline": (Market.MONEYLINE, Period.FULL_GAME),
    "spread": (Market.SPREAD, Period.FULL_GAME),
    "run line": (Market.SPREAD, Period.FULL_GAME),
    "puck line": (Market.SPREAD, Period.FULL_GAME),
    "total": (Market.TOTAL, Period.FULL_GAME),
    "total points": (Market.TOTAL, Period.FULL_GAME),
    "total goals": (Market.TOTAL, Period.FULL_GAME),
    "total runs": (Market.TOTAL, Period.FULL_GAME),
}

#: Event statuses that mean the game is no longer a pregame offer.
LIVE_STATES = frozenset({
    "STARTED", "IN_PROGRESS", "LIVE", "FINAL", "ENDED", "CLOSED", "POSTPONED",
})


@dataclass(frozen=True)
class _GroupScope:
    event_group_id: int
    sport: Sport
    league: str


@dataclass(frozen=True)
class _Fixture:
    event_id: str
    sport: Sport
    competition: League
    home: Participant
    away: Participant
    book_home_key: str
    commence_time: datetime
    base_key: str


class DraftKingsAdapter:
    """Collects DraftKings Illinois pregame game markets for configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        content_base_url: str = DEFAULT_CONTENT_BASE_URL,
        timeout: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        wanted = (
            frozenset(leagues)
            if leagues is not None
            else frozenset(league for _, _, league in EVENT_GROUPS)
        )
        scopes = [
            _GroupScope(gid, sport, league)
            for gid, sport, league in EVENT_GROUPS
            if league in wanted
        ]
        if not scopes:
            raise ValueError(
                f"DraftKingsAdapter has no event group for leagues {sorted(wanted)!r}"
            )
        self._scopes = tuple(scopes)
        self._wanted = wanted
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.content_base_url = content_base_url.rstrip("/")
        self._allow_browser_fallback = client is None
        self._browser_http: SourceClient | None = None
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, host_interval=HOST_INTERVAL
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return tuple(scope.league for scope in self._scopes if scope.league in self._wanted)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {
                scope.league: MARKETS_BY_SPORT[scope.sport]
                for scope in self._scopes
                if scope.league in self._wanted
            },
            self.leagues,
        )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for scope in self._scopes:
            label = f"eventgroup:{scope.event_group_id}"
            tally.requested(label)
            try:
                raw = self._fetch_scope(scope)
            except SourceError as exc:
                log.info(
                    "%s: event group %s unavailable: %s",
                    self._source_key, scope.event_group_id, exc,
                )
                tally.failed(label, exc)
                continue
            raws.append(raw)
            tally.produced(label, _event_count(raw))
        tally.require_something(what="pregame eventgroup")
        return raws

    def _fetch_scope(self, scope: _GroupScope) -> RawResponse:
        current = CONTENT_ROUTES.get(scope.event_group_id)
        if current is None:
            return self._http.get(
                f"{self.base_url}/eventgroups/{scope.event_group_id}",
                endpoint=f"eventgroup-{scope.event_group_id}",
                params={"format": "json"},
                headers={"Origin": DEFAULT_ORIGIN.rstrip("/"), "Referer": DEFAULT_ORIGIN},
            )

        page_path, subcategory_id = current
        page_url = f"{DEFAULT_ORIGIN}leagues/{page_path}"
        params = {
            "isBatchable": "false",
            "templateVars": f"{scope.event_group_id},{subcategory_id}",
            "eventsQuery": (
                f"$filter=leagueId eq '{scope.event_group_id}' AND "
                "clientMetadata/Subcategories/any(s: "
                f"s/Id eq '{subcategory_id}')"
            ),
            "marketsQuery": (
                "$filter=clientMetadata/subCategoryId eq "
                f"'{subcategory_id}' AND tags/all(t: t ne 'SportcastBetBuilder')"
            ),
            "include": "Events",
            "entity": "events",
        }
        headers = {"Origin": DEFAULT_ORIGIN.rstrip("/"), "Referer": page_url}
        try:
            return self._http.get(
                f"{self.content_base_url}/markets",
                endpoint=f"sportscontent-{scope.event_group_id}",
                params=params,
                headers=headers,
            )
        except SourceError:
            if not self._allow_browser_fallback:
                raise
        if self._browser_http is None:
            from src.sources.browser import build_browser_client

            self._browser_http = SourceClient(
                self._source_key,
                client=build_browser_client(timeout=25.0, seed_url=page_url),
                host_interval=HOST_INTERVAL,
            )
        return self._browser_http.get(
            f"{self.content_base_url}/markets",
            endpoint=f"sportscontent-{scope.event_group_id}",
            params=params,
            headers=headers,
        )

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_draftkings(raws)

    def close(self) -> None:
        self._http.close()
        if self._browser_http is not None:
            self._browser_http.close()


def _event_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    events = payload.get("events")
    if not isinstance(events, list):
        events = (payload.get("eventGroup") or {}).get("events")
    return len(events) if isinstance(events, list) else 0


def parse_draftkings(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Pure: no network, no clock, no filesystem.  Source from the envelope."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    fixtures: dict[str, _Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], _Fixture, list[Mapping[str, Any]]]] = []

    for raw in latest_per_endpoint(raws):
        if not raw.endpoint.startswith(("eventgroup", "sportscontent")):
            continue
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: expected object")
        group = payload.get("eventGroup")
        if group is None and raw.endpoint.startswith("sportscontent"):
            group = _eventgroup_from_sportscontent(payload, raw.endpoint)
        if not isinstance(group, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: missing eventGroup")

        league = _league_for_group(group, raw.endpoint)
        if league is None:
            outcome.skipped["eventgroup_out_of_scope"] += 1
            continue
        competition = league_registry.league(league)

        by_id: dict[str, Mapping[str, Any]] = {}
        for event in group.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("eventId") or "")
            if event_id:
                by_id[event_id] = event

        offers_by_event = _game_line_offers(group)
        for event_id, markets in offers_by_event.items():
            event = by_id.get(event_id)
            if event is None:
                outcome.skipped["offer_without_event"] += 1
                continue
            fixture = _accept_event(event, competition, source, raw.fetched_at, outcome)
            if fixture is None:
                continue
            fixtures[event_id] = fixture
            work.append((raw, event, fixture, markets))

    if not fixtures:
        return outcome

    resolved = resolve_doubleheaders(
        {eid: (f.base_key, f.commence_time) for eid, f in fixtures.items()}
    )
    for raw, _event, fixture, markets in work:
        event_key = resolved[fixture.event_id]
        for market in markets:
            _emit_market(market, raw, source, fixture, event_key, outcome)

    drop_duplicate_selections(source, outcome)
    return outcome


def _eventgroup_from_sportscontent(
    payload: Mapping[str, Any], endpoint: str
) -> Mapping[str, Any]:
    """Translate the current normalized DK store into the replay-stable shape."""
    try:
        group_id = int(endpoint.rsplit("-", 1)[-1])
    except ValueError as exc:
        raise FormatChangeError(f"draftkings:{endpoint}: missing league id") from exc
    events = payload.get("events")
    markets = payload.get("markets")
    selections = payload.get("selections")
    if not all(isinstance(rows, list) for rows in (events, markets, selections)):
        raise FormatChangeError(
            f"draftkings:{endpoint}: expected events/markets/selections lists"
        )

    converted_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        participants = event.get("participants") or []
        away = next(
            (p for p in participants if isinstance(p, Mapping) and p.get("venueRole") == "Away"),
            {},
        )
        home = next(
            (p for p in participants if isinstance(p, Mapping) and p.get("venueRole") == "Home"),
            {},
        )
        converted_events.append(
            {
                "eventId": event.get("id"),
                "name": event.get("name"),
                "teamName1": away.get("name"),
                "teamName2": home.get("name"),
                "startDate": event.get("startEventDate"),
                "eventStatus": {"state": event.get("status")},
            }
        )

    by_market: dict[str, list[Mapping[str, Any]]] = {}
    for selection in selections:
        if isinstance(selection, Mapping) and selection.get("marketId"):
            by_market.setdefault(str(selection["marketId"]), []).append(selection)
    converted_markets: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, Mapping):
            continue
        tags = set(market.get("tags") or [])
        converted_outcomes = []
        for selection in by_market.get(str(market.get("id") or ""), []):
            display = selection.get("displayOdds") or {}
            american = str(display.get("american") or "").replace("−", "-")
            converted_outcomes.append(
                {
                    "id": selection.get("id"),
                    "label": selection.get("label"),
                    "oddsDecimal": display.get("decimal") or selection.get("trueOdds"),
                    "oddsAmerican": american,
                    "line": selection.get("points"),
                    "hidden": False,
                }
            )
        converted_markets.append(
            {
                "id": market.get("id"),
                "eventId": market.get("eventId"),
                "label": market.get("name"),
                "isSuspended": False,
                "isOpen": True,
                "main": "PrimaryMarket" in tags,
                "outcomes": converted_outcomes,
            }
        )

    return {
        "eventGroupId": group_id,
        "events": converted_events,
        "offerCategories": [
            {
                "name": "Game Lines",
                "offerSubcategoryDescriptors": [
                    {
                        "name": "Game",
                        "offerSubcategory": {"offers": [converted_markets]},
                    }
                ],
            }
        ],
    }


def _league_for_group(group: Mapping[str, Any], endpoint: str) -> str | None:
    gid = group.get("eventGroupId")
    try:
        gid_int = int(gid)
    except (TypeError, ValueError):
        gid_int = None
    for eg_id, _sport, league in EVENT_GROUPS:
        if gid_int == eg_id or endpoint.endswith(str(eg_id)):
            return league
    name = str(group.get("name") or "").strip().upper()
    for _eg_id, _sport, league in EVENT_GROUPS:
        if name == league or name.replace(" ", "_") == league:
            return league
    return None


def _game_line_offers(group: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Collect open Game-line markets keyed by event id."""
    by_event: dict[str, list[Mapping[str, Any]]] = {}
    for category in group.get("offerCategories") or []:
        if not isinstance(category, dict):
            continue
        if str(category.get("name") or "").casefold() not in {"game lines", "game line"}:
            continue
        for desc in category.get("offerSubcategoryDescriptors") or []:
            if not isinstance(desc, dict):
                continue
            sub_name = str(desc.get("name") or "").casefold()
            if sub_name not in {"game", "games"}:
                continue
            sub = desc.get("offerSubcategory") or {}
            if not isinstance(sub, dict):
                continue
            for bundle in sub.get("offers") or []:
                if not isinstance(bundle, list):
                    continue
                for market in bundle:
                    if not isinstance(market, dict):
                        continue
                    if market.get("isSuspended") or not market.get("isOpen", True):
                        continue
                    if not market.get("main", True):
                        continue
                    event_id = str(market.get("eventId") or "")
                    if not event_id:
                        continue
                    by_event.setdefault(event_id, []).append(market)
    return by_event


def _accept_event(
    event: Mapping[str, Any],
    competition: League,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> _Fixture | None:
    event_id = str(event.get("eventId") or "")
    if not event_id:
        outcome.skipped["missing_event_id"] += 1
        return None

    status = event.get("eventStatus") or {}
    state = str(status.get("state") or "").upper()
    if state in LIVE_STATES:
        outcome.skipped[f"status:{state or 'unknown'}"] += 1
        return None

    # DK names Away @ Home; teamName1 is away, teamName2 is home.
    away_name = str(event.get("teamName1") or "").strip()
    home_name = str(event.get("teamName2") or "").strip()
    if not home_name or not away_name:
        # Fall back to splitting the event name.
        name = str(event.get("name") or "")
        if " @ " in name:
            away_name, home_name = (part.strip() for part in name.split(" @ ", 1))
        elif " vs " in name.casefold():
            # soccer-style; order is not reliable — reject rather than guess home.
            outcome.skipped["ambiguous_home_away"] += 1
            return None
        else:
            outcome.skipped["missing_home_away"] += 1
            return None

    if any(is_pairing(part, competition.sport) for part in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    home = canonical_participant(home_name, competition)
    away = canonical_participant(away_name, competition)
    if home is None or away is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id}: {away_name!r}/{home_name!r}",
            event_id=event_id,
        )
        return None

    commence_time = parse_iso_time(event.get("startDate"))
    if commence_time is None:
        outcome.reject(source, "missing_commence_time", f"event {event_id}", event_id=event_id)
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    away_side, home_side = orient(
        away, home, competition, home=home if competition.has_home_away else None
    )
    return _Fixture(
        event_id=event_id,
        sport=competition.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=home.key,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _emit_market(
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    label = str(market.get("label") or "").strip().casefold()
    mapped = MARKET_LABELS.get(label)
    if mapped is None:
        outcome.skipped["market_out_of_scope"] += 1
        return
    market_kind, period = mapped
    if market_kind not in MARKETS_BY_SPORT.get(fixture.sport, frozenset()):
        outcome.skipped["market_out_of_scope"] += 1
        return

    for outcome_row in market.get("outcomes") or []:
        if not isinstance(outcome_row, dict):
            continue
        if outcome_row.get("hidden"):
            outcome.skipped["outcome_hidden"] += 1
            continue
        selection = _selection_for(outcome_row, market_kind, fixture, outcome, source)
        if selection is None:
            continue
        try:
            decimal = float(outcome_row["oddsDecimal"])
        except (KeyError, TypeError, ValueError):
            outcome.reject(
                source,
                "missing_odds",
                f"event {fixture.event_id} {label}",
                event_id=fixture.event_id,
            )
            continue
        if not is_plausible_decimal_odds(decimal):
            outcome.reject(
                source,
                "implausible_odds",
                f"event {fixture.event_id} {label} {decimal}",
                event_id=fixture.event_id,
            )
            continue

        line = outcome_row.get("line")
        line_f: float | None
        if market_kind in (Market.SPREAD, Market.TOTAL):
            try:
                line_f = float(line)
            except (TypeError, ValueError):
                outcome.reject(
                    source,
                    "missing_line",
                    f"event {fixture.event_id} {label}",
                    event_id=fixture.event_id,
                )
                continue
        else:
            line_f = None

        american = decimal_to_american(decimal)
        try:
            american_pub = int(str(outcome_row.get("oddsAmerican") or american))
        except ValueError:
            american_pub = american

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
                    market=market_kind,
                    period=period,
                    selection=selection,
                    line=line_f,
                    decimal_odds=decimal,
                    american_odds=american_pub,
                    implied_probability=implied_probability(decimal),
                    status=QuoteStatus.ACTIVE,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)


def _selection_for(
    outcome_row: Mapping[str, Any],
    market_kind: Market,
    fixture: _Fixture,
    outcome: ParseOutcome,
    source: str,
) -> Selection | None:
    label = str(outcome_row.get("label") or "").strip()
    if not label:
        outcome.skipped["missing_outcome_label"] += 1
        return None
    folded = label.casefold()
    if market_kind is Market.TOTAL:
        if folded.startswith("over"):
            return Selection.OVER
        if folded.startswith("under"):
            return Selection.UNDER
        outcome.skipped["unknown_total_side"] += 1
        return None

    # Moneyline / spread: match participant by label.
    participant = canonical_participant(label, fixture.competition)
    if participant is None:
        outcome.reject(
            source,
            "unresolved_selection",
            f"event {fixture.event_id}: {label!r}",
            event_id=fixture.event_id,
        )
        return None
    if participant.key == fixture.home.key:
        return Selection.HOME
    if participant.key == fixture.away.key:
        return Selection.AWAY
    if folded in {"draw", "tie", "x"}:
        return Selection.DRAW
    outcome.reject(
        source,
        "selection_not_on_fixture",
        f"event {fixture.event_id}: {label!r}",
        event_id=fixture.event_id,
    )
    return None


__all__ = [
    "DraftKingsAdapter",
    "EVENT_GROUPS",
    "SOURCE_KEY",
    "parse_draftkings",
]
