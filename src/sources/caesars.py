"""Caesars Sportsbook pregame markets, from the American Wagering API.

``api.americanwagering.com/regions/us/locations/{state}/brands/czr/sb/v3/`` is
the JSON the public sportsbook page reads.  Highlights by ``competitionId``
list events; each event detail carries ``markets`` with decimal ``price.d``.

From California the CloudFront edge answers ``403`` without a licensed-state
``ODDS_HTTP_PROXY``.  The parser is pinned against captured / synthetic
envelopes and does not depend on that wall.
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

SOURCE_KEY = "caesars"
DEFAULT_BASE_URL = "https://api.americanwagering.com/regions/us/locations/nj/brands/czr/sb/v3"
DEFAULT_ORIGIN = "https://www.caesars.com/sportsbook-and-casino"
HOST_INTERVAL = 0.6

#: competitionId → (sport, league).  UUIDs observed on the public API.
#:
#: All five confirmed against the Illinois sports menu captured
#: 2026-08-04T00:43Z (``data/research/caesars/IL/20260804T004333Z``,
#: response-010): the NFL and NBA ids already here appeared unchanged, and the
#: menu supplied the three seasonal ids that were missing.  Seasonal ids can
#: rotate — re-read them from a fresh menu capture, never guess.
COMPETITIONS: tuple[tuple[str, Sport, str], ...] = (
    ("007d7c61-07a7-4e18-bb40-15104b6eac92", Sport.FOOTBALL, "NFL"),
    ("5806c896-4eec-4de1-874f-afed93114b8c", Sport.BASKETBALL, "NBA"),
    ("04f90892-3afa-4e84-acce-5b89f151063d", Sport.BASEBALL, "MLB"),
    ("b7b715a9-c7e8-4c47-af0a-77385b525e09", Sport.HOCKEY, "NHL"),
    ("fa3dd530-9699-4731-8ff2-6b3df29ae403", Sport.BASKETBALL, "WNBA"),
)

MARKET_NAMES: dict[str, tuple[Market, Period]] = {
    "money line": (Market.MONEYLINE, Period.FULL_GAME),
    "moneyline": (Market.MONEYLINE, Period.FULL_GAME),
    "spread": (Market.SPREAD, Period.FULL_GAME),
    "point spread": (Market.SPREAD, Period.FULL_GAME),
    "run line": (Market.SPREAD, Period.FULL_GAME),
    "puck line": (Market.SPREAD, Period.FULL_GAME),
    "total": (Market.TOTAL, Period.FULL_GAME),
    "total points": (Market.TOTAL, Period.FULL_GAME),
    "total goals": (Market.TOTAL, Period.FULL_GAME),
    "total runs": (Market.TOTAL, Period.FULL_GAME),
}

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
}


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


class CaesarsAdapter:
    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 25.0,
        client: httpx.Client | None = None,
        proxy_state: str | None = None,
    ) -> None:
        wanted = (
            frozenset(leagues)
            if leagues is not None
            else frozenset(league for _, _, league in COMPETITIONS)
        )
        comps = [
            (cid, sport, league)
            for cid, sport, league in COMPETITIONS
            if league in wanted
        ]
        if not comps:
            raise ValueError(f"CaesarsAdapter has no competition for {sorted(wanted)!r}")
        self._comps = tuple(comps)
        self._wanted = wanted
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=HOST_INTERVAL,
            proxy_state=proxy_state,
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return tuple(league for _, _, league in self._comps)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {league: MARKETS_BY_SPORT[sport] for _, sport, league in self._comps},
            self.leagues,
        )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        headers = {
            "Origin": "https://www.caesars.com",
            "Referer": DEFAULT_ORIGIN,
        }
        for competition_id, _sport, league in self._comps:
            label = f"competition:{league}"
            tally.requested(label)
            try:
                highlights = self._http.get(
                    f"{self.base_url}/events/highlights/",
                    endpoint=f"highlights-{league}",
                    params={"competitionId": competition_id},
                    headers=headers,
                )
            except SourceError as exc:
                tally.failed(label, exc)
                continue
            raws.append(highlights)
            event_ids = _event_ids_from_highlights(highlights, competition_id)
            details = 0
            for event_id in event_ids:
                try:
                    detail = self._http.get(
                        f"{self.base_url}/events/{event_id}",
                        endpoint=f"event-{event_id}",
                        headers=headers,
                    )
                except SourceError as exc:
                    log.info("%s: event %s unavailable: %s", self._source_key, event_id, exc)
                    continue
                raws.append(detail)
                details += 1
            if details:
                tally.produced(label, details)
            else:
                tally.failed(
                    label,
                    SourceError(
                        f"{self._source_key}:{label}: no event details — "
                        "ODDS_HTTP_PROXY to a licensed state may be required"
                    ),
                )
        tally.require_something(what="pregame Caesars event")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_caesars(raws)

    def close(self) -> None:
        self._http.close()


def _event_ids_from_highlights(raw: RawResponse, competition_id: str) -> list[str]:
    try:
        payload = raw.json()
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    ids: list[str] = []
    for comp in payload.get("competitions") or []:
        if not isinstance(comp, dict):
            continue
        if str(comp.get("id") or "") != competition_id:
            continue
        for event in comp.get("events") or []:
            if isinstance(event, dict) and event.get("id"):
                ids.append(str(event["id"]))
            elif isinstance(event, str):
                ids.append(event)
    # Some payloads nest event ids one level flatter.
    if not ids:
        for event in payload.get("events") or []:
            if isinstance(event, dict) and event.get("id"):
                ids.append(str(event["id"]))
    return ids


def parse_caesars(raws: Sequence[RawResponse]) -> ParseOutcome:
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    fixtures: dict[str, _Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], _Fixture]] = []

    for raw in latest_per_endpoint(raws):
        if not raw.endpoint.startswith("event-"):
            continue
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: expected event object")
        fixture = _accept_event(payload, source, raw.fetched_at, outcome)
        if fixture is None:
            continue
        fixtures[fixture.event_id] = fixture
        work.append((raw, payload, fixture))

    if not fixtures:
        return outcome

    resolved = resolve_doubleheaders(
        {eid: (f.base_key, f.commence_time) for eid, f in fixtures.items()}
    )
    for raw, payload, fixture in work:
        _emit_markets(
            payload.get("markets") or [],
            raw,
            source,
            fixture,
            resolved[fixture.event_id],
            outcome,
        )
    drop_duplicate_selections(source, outcome)
    return outcome


def _accept_event(
    event: Mapping[str, Any],
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> _Fixture | None:
    event_id = str(event.get("id") or "")
    if not event_id:
        outcome.skipped["missing_event_id"] += 1
        return None

    league_key = _league_from_event(event)
    if league_key is None:
        outcome.skipped["competition_out_of_scope"] += 1
        return None
    competition = league_registry.league(league_key)

    name = str(event.get("name") or "").replace("|", "").strip()
    away_name, home_name = _split_name(name, event)
    if not away_name or not home_name:
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

    commence_time = parse_iso_time(
        event.get("startTime") or event.get("start") or event.get("eventTime")
    )
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


def _league_from_event(event: Mapping[str, Any]) -> str | None:
    comp = event.get("competition") or {}
    if isinstance(comp, dict):
        cid = str(comp.get("id") or "")
        for known_id, _sport, league in COMPETITIONS:
            if cid == known_id:
                return league
        name = str(comp.get("name") or "").casefold()
        for _cid, _sport, league in COMPETITIONS:
            if league.casefold() in name:
                return league
    # Fall back to competition id on the event itself.
    cid = str(event.get("competitionId") or "")
    for known_id, _sport, league in COMPETITIONS:
        if cid == known_id:
            return league
    return None


def _split_name(name: str, event: Mapping[str, Any]) -> tuple[str, str]:
    teams = event.get("teams") or event.get("competitors") or []
    if isinstance(teams, list) and len(teams) >= 2:
        by_side: dict[str, str] = {}
        for team in teams:
            if not isinstance(team, dict):
                continue
            side = str(team.get("side") or team.get("type") or "").casefold()
            label = str(team.get("name") or "").replace("|", "").strip()
            if side and label:
                by_side[side] = label
        if "home" in by_side and "away" in by_side:
            return by_side["away"], by_side["home"]
    if " @ " in name:
        away, home = name.split(" @ ", 1)
        return away.strip(), home.strip()
    # ``vs`` order is not a reliable home/away signal on Caesars — refuse
    # rather than guess and flip every selection.
    return "", ""


def _emit_markets(
    markets: Sequence[Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    allowed = MARKETS_BY_SPORT.get(fixture.sport, frozenset())
    for market in markets:
        if not isinstance(market, dict):
            continue
        if not market.get("display", True) or not market.get("active", True):
            outcome.skipped["market_inactive"] += 1
            continue
        name = str(market.get("name") or "").replace("|", "").strip().casefold()
        mapped = MARKET_NAMES.get(name)
        if mapped is None:
            outcome.skipped["market_out_of_scope"] += 1
            continue
        market_kind, period = mapped
        if market_kind not in allowed:
            outcome.skipped["market_out_of_scope"] += 1
            continue
        try:
            market_line = float(market["line"]) if market.get("line") is not None else None
        except (TypeError, ValueError):
            market_line = None

        for sel in market.get("selections") or []:
            if not isinstance(sel, dict):
                continue
            if not sel.get("display", True) or not sel.get("active", True):
                outcome.skipped["selection_inactive"] += 1
                continue
            selection = _selection_for(sel, market_kind, fixture, outcome, source)
            if selection is None:
                continue
            price = sel.get("price") or {}
            try:
                decimal = float(price["d"] if isinstance(price, dict) else price)
            except (KeyError, TypeError, ValueError):
                outcome.reject(
                    source,
                    "missing_odds",
                    f"event {fixture.event_id} {name}",
                    event_id=fixture.event_id,
                )
                continue
            if not is_plausible_decimal_odds(decimal):
                outcome.reject(
                    source,
                    "implausible_odds",
                    f"event {fixture.event_id} {decimal}",
                    event_id=fixture.event_id,
                )
                continue
            line = market_line
            if market_kind is Market.SPREAD and line is not None:
                # Caesars publishes one market line; sign from the selection side.
                line = -abs(line) if selection is Selection.HOME else abs(line)
            if market_kind in (Market.SPREAD, Market.TOTAL) and line is None:
                outcome.reject(
                    source,
                    "missing_line",
                    f"event {fixture.event_id} {name}",
                    event_id=fixture.event_id,
                )
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
                        market=market_kind,
                        period=period,
                        selection=selection,
                        line=None if market_kind is Market.MONEYLINE else line,
                        decimal_odds=decimal,
                        american_odds=decimal_to_american(decimal),
                        implied_probability=implied_probability(decimal),
                        status=QuoteStatus.ACTIVE,
                        source_market_id=str(market.get("id") or "") or None,
                    )
                )
            except (TypeError, ValueError) as exc:
                outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)


def _selection_for(
    sel: Mapping[str, Any],
    market_kind: Market,
    fixture: _Fixture,
    outcome: ParseOutcome,
    source: str,
) -> Selection | None:
    name = str(sel.get("name") or "").replace("|", "").strip()
    folded = name.casefold()
    if market_kind is Market.TOTAL:
        if folded.startswith("over"):
            return Selection.OVER
        if folded.startswith("under"):
            return Selection.UNDER
        outcome.skipped["unknown_total_side"] += 1
        return None
    participant = canonical_participant(name, fixture.competition)
    if participant is None:
        outcome.reject(
            source,
            "unresolved_selection",
            f"event {fixture.event_id}: {name!r}",
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
        f"event {fixture.event_id}: {name!r}",
        event_id=fixture.event_id,
    )
    return None


__all__ = ["CaesarsAdapter", "SOURCE_KEY", "parse_caesars"]
