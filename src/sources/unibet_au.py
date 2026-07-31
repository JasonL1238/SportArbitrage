"""Unibet Australia sportsbook-feeds — structured list view with decimal×1000 odds.

``www.unibet.com.au/sportsbook-feeds/views/filter/{sport}/all/matches`` returns
events with ``homeName``/``awayName``, ``start``, ``state``, and ``betOffers``.
The filter view carries moneylines; capabilities declare that honestly.
"""
from __future__ import annotations

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
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Sport, draw_is_priced
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

#: Criterion labels on the list view that are the game moneyline (incl. OT /
#: regulation variants).  Normalised by stripping punctuation to a token set.
_MONEYLINE_CRITERIA = frozenset({
    "moneyline",
    "match odds",
    "match",
    "win",
    "full time",
    "moneyline including overtime",
    "match odds regular time",
})

SOURCE_KEY = "unibet_au"
DEFAULT_BASE_URL = "https://www.unibet.com.au/sportsbook-feeds/views/filter"
HOST_INTERVAL = 0.45

SPORT_PATHS: tuple[tuple[str, Sport], ...] = (
    ("baseball", Sport.BASEBALL),
    ("basketball", Sport.BASKETBALL),
    ("tennis", Sport.TENNIS),
    ("football", Sport.SOCCER),
    ("american-football", Sport.FOOTBALL),
    ("ice-hockey", Sport.HOCKEY),
)

LEAGUE_BY_TERM: dict[str, str] = {
    "mlb": "MLB",
    "wnba": "WNBA",
    "nba": "NBA",
    "nfl": "NFL",
    "nhl": "NHL",
    "atp": "ATP",
    "wta": "WTA",
    "premier_league": "EPL",
    "english_premier_league": "EPL",
    "mls": "MLS",
}

DEFAULT_LEAGUES: tuple[str, ...] = ("MLB", "WNBA", "NBA", "NFL", "NHL", "ATP", "WTA", "EPL", "MLS")


@dataclass(frozen=True)
class _Fixture:
    event_id: str
    sport: Sport
    competition: League
    home: Participant
    away: Participant
    commence_time: datetime
    base_key: str


class UnibetAuAdapter:
    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._leagues = tuple(leagues)
        self._wanted = frozenset(leagues)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, host_interval=HOST_INTERVAL
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """List view exposes moneyline only — declare that so coverage is honest."""
        del tier
        markets = frozenset({Market.MONEYLINE})
        return capabilities_from({league: markets for league in self._leagues}, self._leagues)

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for path, _sport in SPORT_PATHS:
            tally.requested(path)
            try:
                raw = self._http.get(
                    f"{self.base_url}/{path}/all/matches",
                    endpoint=f"filter-{path}",
                    params={"includeParticipants": "true"},
                )
            except SourceError as exc:
                tally.failed(path, exc)
                continue
            raws.append(raw)
            tally.produced(path, 1)
        tally.require_something(what="unibet.au view")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_unibet_au(raws)

    def close(self) -> None:
        self._http.close()


def parse_unibet_au(
    raws: Sequence[RawResponse], *, wanted: frozenset[str] | None = None
) -> ParseOutcome:
    wanted = wanted if wanted is not None else frozenset(DEFAULT_LEAGUES)
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    fixtures: dict[str, _Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], _Fixture]] = []

    for raw in latest_per_endpoint(raws):
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: expected object")
        for packed in _iter_events(payload):
            fixture = _accept(packed, source, raw.fetched_at, wanted, outcome)
            if fixture is None:
                continue
            fixtures[fixture.event_id] = fixture
            work.append((raw, packed, fixture))

    resolved = resolve_doubleheaders(
        {f.event_id: (f.base_key, f.commence_time) for f in fixtures.values()}
    )
    for raw, packed, fixture in work:
        event_key = resolved.get(fixture.event_id, fixture.base_key)
        # Filter view often lists both "Moneyline" and "Match Odds" for the same
        # contract — emit at most one moneyline offer per event.
        emitted_ml = False
        for offer in packed.get("betOffers") or []:
            if not isinstance(offer, dict):
                continue
            before = len(outcome.quotes)
            _emit_offer(offer, raw, source, fixture, event_key, outcome)
            if len(outcome.quotes) > before:
                emitted_ml = True
                break
        del emitted_ml

    drop_duplicate_selections(source, outcome)
    return outcome


def _iter_events(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            event = node.get("event")
            if isinstance(event, dict) and isinstance(node.get("betOffers"), list):
                event_id = str(event.get("id") or "")
                if event_id and event_id not in seen:
                    seen.add(event_id)
                    found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _league_from_event(event: Mapping[str, Any]) -> str | None:
    term = str(event.get("group") or "").strip()
    for path in event.get("path") or []:
        if isinstance(path, dict) and path.get("termKey"):
            term = str(path["termKey"])
    key = LEAGUE_BY_TERM.get(term.lower().replace(" ", "_"))
    if key:
        return key
    # path english names
    for path in event.get("path") or []:
        if not isinstance(path, dict):
            continue
        name = str(path.get("englishName") or path.get("name") or "").upper()
        for fragment, league in (
            ("MLB", "MLB"),
            ("WNBA", "WNBA"),
            ("NBA", "NBA"),
            ("NFL", "NFL"),
            ("NHL", "NHL"),
            ("ATP", "ATP"),
            ("WTA", "WTA"),
            ("PREMIER LEAGUE", "EPL"),
            ("ENGLISH PREMIER", "EPL"),
            ("MAJOR LEAGUE SOCCER", "MLS"),
            ("MLS", "MLS"),
            ("LA LIGA", "LA_LIGA"),
            ("SERIE A", "SERIE_A"),
            ("BUNDESLIGA", "BUNDESLIGA"),
            ("LIGUE 1", "LIGUE_1"),
        ):
            if fragment in name:
                return league
    group = str(event.get("group") or "").upper()
    for fragment, league in (
        ("MLB", "MLB"),
        ("WNBA", "WNBA"),
        ("NBA", "NBA"),
        ("NFL", "NFL"),
        ("NHL", "NHL"),
        ("ATP", "ATP"),
        ("WTA", "WTA"),
        ("PREMIER LEAGUE", "EPL"),
        ("MLS", "MLS"),
    ):
        if fragment == group or fragment in group:
            return league
    return None


def _accept(
    packed: Mapping[str, Any],
    source: str,
    captured_at: datetime,
    wanted: frozenset[str],
    outcome: ParseOutcome,
) -> _Fixture | None:
    event = packed.get("event") or {}
    if not isinstance(event, dict):
        return None
    if str(event.get("state") or "") != "NOT_STARTED":
        outcome.skipped[f"state:{event.get('state')}"] += 1
        return None
    event_id = str(event.get("id") or "")
    if not event_id:
        outcome.skipped["missing_event_id"] += 1
        return None
    league_key = _league_from_event(event)
    if league_key is None or league_key not in wanted:
        outcome.skipped["competition_out_of_scope"] += 1
        return None
    competition = league_registry.league(league_key)

    home_name = str(event.get("homeName") or "").strip()
    away_name = str(event.get("awayName") or "").strip()
    for part in event.get("participants") or []:
        if not isinstance(part, dict):
            continue
        if part.get("home") is True:
            home_name = str(part.get("name") or home_name).strip()
        elif part.get("home") is False:
            away_name = str(part.get("name") or away_name).strip()
    if not home_name or not away_name:
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

    commence_time = parse_iso_time(event.get("start"))
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
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _emit_offer(
    offer: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    label = str(
        (offer.get("criterion") or {}).get("englishLabel")
        or (offer.get("criterion") or {}).get("label")
        or ""
    ).strip()
    normalised = " ".join("".join(ch if ch.isalnum() else " " for ch in label.lower()).split())
    if normalised not in _MONEYLINE_CRITERIA:
        outcome.skipped[f"market:{label.lower() or 'unknown'}"] += 1
        return
    if offer.get("suspended"):
        outcome.skipped["offer_suspended"] += 1
        return
    # NHL list view posts regulation 1X2.  Dropping the draw and keeping the
    # two sides underrounds the market; skip the whole offer instead.
    if (
        not draw_is_priced(fixture.sport, Period.FULL_GAME)
        and "regular time" in normalised
    ):
        outcome.skipped["regulation_three_way_unpriced"] += 1
        return
    market_id = str(offer.get("id") or label)
    pending: list[tuple[Selection, float, str | None]] = []
    saw_draw = False
    for entry in offer.get("outcomes") or []:
        if not isinstance(entry, dict):
            outcome.skipped["selection_not_an_object"] += 1
            continue
        try:
            # Odds are decimal × 1000 (1970 → 1.970).
            decimal_odds = float(entry["odds"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            outcome.skipped["unreadable_odds"] += 1
            continue
        if not is_plausible_decimal_odds(decimal_odds):
            outcome.skipped["implausible_odds"] += 1
            continue
        name = str(entry.get("label") or entry.get("participant") or "").strip()
        selection = _outcome_selection(name, fixture)
        if selection is None:
            outcome.skipped["unmapped_outcome_label"] += 1
            continue
        if selection is Selection.DRAW:
            saw_draw = True
            if not draw_is_priced(fixture.sport, Period.FULL_GAME):
                outcome.skipped["draw_not_priced"] += 1
                continue
        sel_id = str(entry.get("id")) if entry.get("id") is not None else None
        pending.append((selection, decimal_odds, sel_id))

    if saw_draw and not draw_is_priced(fixture.sport, Period.FULL_GAME):
        outcome.skipped["regulation_three_way_unpriced"] += 1
        return

    for selection, decimal_odds, sel_id in pending:
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
                    market=Market.MONEYLINE,
                    period=Period.FULL_GAME,
                    selection=selection,
                    line=None,
                    is_alternate=False,
                    decimal_odds=decimal_odds,
                    american_odds=decimal_to_american(decimal_odds),
                    implied_probability=implied_probability(decimal_odds),
                    source_market_id=market_id,
                    source_selection_id=sel_id,
                    status=QuoteStatus.ACTIVE,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)


def _outcome_selection(name: str, fixture: _Fixture) -> Selection | None:
    """Map a Unibet outcome label onto HOME / AWAY / DRAW.

    List-view soccer and hockey often use ``1`` / ``X`` / ``2`` instead of
    team names; baseball / tennis use the participant string.
    """
    token = name.strip().casefold()
    if token in {"x", "draw", "tie", "the draw"}:
        return Selection.DRAW
    if token in {"1", "home"}:
        return Selection.HOME
    if token in {"2", "away"}:
        return Selection.AWAY
    part = canonical_participant(name, fixture.competition)
    if part is not None:
        if part.key == fixture.home.key:
            return Selection.HOME
        if part.key == fixture.away.key:
            return Selection.AWAY
    if name == fixture.home.name:
        return Selection.HOME
    if name == fixture.away.name:
        return Selection.AWAY
    return None
