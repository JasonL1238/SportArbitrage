"""Cloudbet game markets, from the public sports API its own site reads.

List: ``/sports-api/c/v6/sports/events?limit=50&sport={key}`` returns
sports → competitions → events (often without full ``markets``).
Detail: ``/sports-api/c/v6/sports/events/{id}`` returns ``home``, ``away``,
``startTime``, ``status``, and a ``markets`` dict.

Market keys look like ``baseball.moneyline``, ``basketball.handicap``,
``soccer.match_odds``, ``baseball.moneyline_innings_1_to_5``.  Submarkets are
keyed by period; selections carry ``outcome``, decimal ``price``, and ``params``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.normalize import (
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import (
    canonical_participant,
    competition_marker,
    is_pairing,
    with_marker,
)
from src.raw_store import RawResponse
from src.schema import Market, Period, QuoteStatus, Selection, Sport, draw_is_priced
from src.sources._common import (
    Fixture,
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_per_endpoint,
    parse_iso_time,
    priced_quote,
    squashed_label,
    tennis_tour,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "cloudbet"
DEFAULT_BASE_URL = "https://www.cloudbet.com/sports-api/c/v6"
HOST_INTERVAL = 0.35
LIST_LIMIT = 50

SPORT_KEYS: tuple[tuple[str, Sport], ...] = (
    ("baseball", Sport.BASEBALL),
    ("basketball", Sport.BASKETBALL),
    ("american-football", Sport.FOOTBALL),
    ("ice-hockey", Sport.HOCKEY),
    ("tennis", Sport.TENNIS),
    ("soccer", Sport.SOCCER),
)

COMPETITION_LEAGUES: dict[str, str] = {
    "baseball-usa-mlb": "MLB",
    "basketball-usa-wnba": "WNBA",
    "basketball-usa-nba": "NBA",
    "american-football-usa-nfl": "NFL",
    "ice-hockey-usa-nhl": "NHL",
    "soccer-england-premier-league": "EPL",
    "soccer-usa-major-league-soccer": "MLS",
    "soccer-usa-mls": "MLS",
}


DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB", "WNBA", "NBA", "NFL", "NHL",
    "ATP", "WTA", "ATP_CHALLENGER", "ITF",
    "EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1", "SOCCER_OTHER",
)

#: Cloudbet keys a competition as ``soccer-<country>-<slug>``, on every one of
#: the six soccer competitions any stored listing has carried:
#: ``soccer-england-premier-league``, ``soccer-usa-major-league-soccer``,
#: ``soccer-argentina-superliga``, ``soccer-england-community-shield``,
#: ``soccer-international-clubs-copa-sudamericana`` and
#: ``soccer-international-clubs-uefa-super-cup``.
#:
#: So the senior competitions are matched on **country prefix plus name**, never
#: on the name alone.  "Premier League" is equally what Iceland, Wales, Malta,
#: Armenia, Tanzania and Canada call theirs, and the byte-identical substring
#: table this replaced — the same one BetMGM carried — filed all of them as EPL.
#:
#: Cloudbet's own keys for La Liga, Serie A, Bundesliga and Ligue 1 are
#: **unmeasured**: no stored listing has carried them, the European seasons
#: having not begun at capture time.  Their country prefixes are written from the
#: observed grammar and nothing else.  If a prefix is wrong the competition falls
#: to the catch-all and ``league_disagreement`` names it against the other books
#: — recoverable, and the reason no full slug is guessed here.
#: The name half is matched against the haystack with every separator removed,
#: because the same competition is spelled three ways in one record: the key says
#: ``la-liga``, the name says ``LaLiga``, and the old marker said ``la liga`` —
#: so it matched neither, exactly as BetMGM's did.
SOCCER_COUNTRY_LEAGUES: tuple[tuple[str, str, str], ...] = (
    ("soccer-england-", "premierleague", "EPL"),
    ("soccer-usa-", "majorleaguesoccer", "MLS"),
    ("soccer-usa-", "mls", "MLS"),
    ("soccer-spain-", "laliga", "LA_LIGA"),
    ("soccer-italy-", "seriea", "SERIE_A"),
    ("soccer-germany-", "bundesliga", "BUNDESLIGA"),
    ("soccer-france-", "ligue1", "LIGUE_1"),
)

#: Demotes a competition out of its country's senior tier.  Cloudbet writes
#: "2. Bundesliga" and "LaLiga 2", which a country-anchored name match alone
#: still files on the senior key — this replaces the ad-hoc ``"2."`` test that
#: covered Bundesliga and nothing else.  A false demotion costs a label and
#: lands in the catch-all; a false promotion puts a second-tier price on a
#: top-flight key, so the bias is deliberate.
#: A women's, youth or reserve tier is demoted by ``competition_marker``
#: instead, which already recognises the distinction in ten languages.
SECOND_TIER_MARKERS: tuple[str, ...] = (
    "2. ", "-2-", "-2", " 2", "segunda", "serie b", "ligue 2",
)

#: Every soccer competition not routed above.  Cloudbet dropped these at *fetch*
#: time with a bare ``continue``, so they were never requested and never counted
#: — ``docs/INPUT_CONTRACT.md`` calls that a contract violation, and it is a
#: worse position than BetMGM's counted skip because nothing recorded the loss.
SOCCER_CATCH_ALL = "SOCCER_OTHER"

_PARAM_NUM = re.compile(r"(?:handicap|total)=(-?\d+(?:\.\d+)?)")

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.SOCCER: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.TENNIS: frozenset({Market.MONEYLINE}),
}


@dataclass(frozen=True)
class _Fixture(Fixture):
    """The shared fixture plus the market namespace this sport is priced under."""

    market_prefix: str
    """Cloudbet keys every market by sport — ``baseball.moneyline`` — so the
    prefix resolved with the fixture is what the priced side is read against."""


class CloudbetAdapter:
    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        wanted = tuple(leagues)
        sports = {league_registry.league(key).sport for key in wanted}
        self._sport_keys = tuple(key for key, sport in SPORT_KEYS if sport in sports)
        if not self._sport_keys:
            raise ValueError(f"CloudbetAdapter has no sport for leagues {list(wanted)!r}")
        self._wanted = frozenset(wanted)
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
        return tuple(key for key in DEFAULT_LEAGUES if key in self._wanted)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Moneyline/spread/total for team sports; tennis moneyline only.

        *tier* is unused: prices live on per-event detail calls required either way.
        """
        del tier
        return capabilities_from(
            {
                key: MARKETS_BY_SPORT[league_registry.league(key).sport]
                for key in self.leagues
            },
            self.leagues,
        )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for sport_key in self._sport_keys:
            label = f"sport:{sport_key}"
            tally.requested(label)
            try:
                batch = self._fetch_sport(sport_key)
            except SourceError as exc:
                log.info("%s: sport %s unavailable: %s", self._source_key, sport_key, exc)
                tally.failed(label, exc)
                continue
            raws.extend(batch)
            tally.produced(label, sum(1 for r in batch if r.endpoint.startswith("event:")))
        tally.require_something(what="pregame event detail")
        return raws

    def _fetch_sport(self, sport_key: str) -> list[RawResponse]:
        listing = self._http.get(
            f"{self.base_url}/sports/events",
            endpoint=f"events:sport-{sport_key}",
            params={"limit": str(LIST_LIMIT), "sport": sport_key},
            headers={
                "Origin": "https://www.cloudbet.com",
                "Referer": "https://www.cloudbet.com/",
            },
        )
        pages = [listing]
        for event_id in _list_event_ids(listing, self._wanted):
            try:
                pages.append(
                    self._http.get(
                        f"{self.base_url}/sports/events/{event_id}",
                        endpoint=f"event:{event_id}",
                        headers={
                            "Origin": "https://www.cloudbet.com",
                            "Referer": "https://www.cloudbet.com/",
                        },
                    )
                )
            except SourceError as exc:
                log.info("%s: event %s unavailable: %s", self._source_key, event_id, exc)
        return pages

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_cloudbet(raws)

    def close(self) -> None:
        self._http.close()


def _list_event_ids(raw: RawResponse, wanted: frozenset[str]) -> list[str]:
    try:
        payload = raw.json()
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for sport in payload.get("sports") or []:
        if not isinstance(sport, dict):
            continue
        for comp in sport.get("competitions") or []:
            if not isinstance(comp, dict):
                continue
            league = _competition_league(comp)
            if league is None or league not in wanted:
                continue
            for event in comp.get("events") or []:
                if not isinstance(event, dict):
                    continue
                if str(event.get("status") or "") != "TRADING":
                    continue
                event_id = str(event.get("id") or "")
                if event_id and event_id not in seen:
                    seen.add(event_id)
                    ids.append(event_id)
    return ids


def _competition_league(comp: Mapping[str, Any]) -> str | None:
    key = str(comp.get("key") or "")
    name = str(comp.get("name") or "")
    haystack = f"{key} {name}".casefold()
    if "virtual" in haystack or "simulated" in haystack or "double" in haystack:
        return None
    found = COMPETITION_LEAGUES.get(key)
    if found:
        return found
    if "tennis" in key or "atp" in haystack or "wta" in haystack or "itf" in haystack:
        return tennis_tour(haystack)
    if key.startswith("soccer-"):
        # Tier on the raw text, where the separators still carry the ordinal
        # ("2. Bundesliga", "la-liga-2"); the league name on the squashed text,
        # where three spellings of one competition collapse to one.
        second_tier = any(token in haystack for token in SECOND_TIER_MARKERS) or bool(
            competition_marker(name, Sport.SOCCER)
        )
        squashed = squashed_label(haystack)
        for prefix, marker, league in SOCCER_COUNTRY_LEAGUES:
            if key.startswith(prefix) and marker in squashed and not second_tier:
                return league
        return SOCCER_CATCH_ALL
    if name in DEFAULT_LEAGUES:
        return name
    return None


def parse_cloudbet(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Pure: no network, no clock, no filesystem.  Source from the envelope."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    fixtures: dict[str, _Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], _Fixture]] = []

    for raw in latest_per_endpoint(raws):
        # Fetch labels use ``event:{id}``; older captures used ``event-{id}``.
        if not (raw.endpoint.startswith("event:") or raw.endpoint.startswith("event-")):
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
            payload.get("markets") or {},
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
    if str(event.get("status") or "") != "TRADING":
        outcome.skipped[f"status:{event.get('status')}"] += 1
        return None

    comp = event.get("competition") or {}
    if not isinstance(comp, dict):
        outcome.skipped["competition_out_of_scope"] += 1
        return None
    league_key = _competition_league(comp)
    if league_key is None:
        outcome.skipped["competition_out_of_scope"] += 1
        return None
    competition = league_registry.league(league_key)

    # Unrecognised competitions now share one catch-all league, so Cloudbet's own
    # name for the competition is read by nothing else — and a women's, reserve
    # or youth tier whose *name* carries the distinction while the club names do
    # not would otherwise produce a fixture byte-identical to the men's fixture
    # of the same two clubs at another book.
    marker = competition_marker(str(comp.get("name") or ""), competition.sport)
    home_name = str((event.get("home") or {}).get("name") or "").strip()
    away_name = str((event.get("away") or {}).get("name") or "").strip()
    home_name = with_marker(home_name, marker) or home_name
    away_name = with_marker(away_name, marker) or away_name
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

    commence_time = parse_iso_time(event.get("startTime"))
    if commence_time is None:
        outcome.reject(source, "missing_commence_time", f"event {event_id}", event_id=event_id)
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    sport_node = event.get("sport") or {}
    sport_key = str(sport_node.get("key") or "").strip()
    if not sport_key:
        outcome.skipped["missing_sport_key"] += 1
        return None
    # List/detail sport keys use hyphens; market map keys use underscores
    # (``american-football`` → ``american_football.moneyline``).
    market_prefix = {
        "american-football": "american_football",
        "ice-hockey": "ice_hockey",
    }.get(sport_key, sport_key)

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
        market_prefix=market_prefix,
    )


def _emit_markets(
    markets: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    if not isinstance(markets, dict):
        outcome.skipped["markets_missing"] += 1
        return
    prefix = fixture.market_prefix
    specs: list[tuple[str, Market, Period]] = [
        (f"{prefix}.moneyline", Market.MONEYLINE, Period.FULL_GAME),
        (f"{prefix}.match_odds", Market.MONEYLINE, Period.FULL_GAME),
        (f"{prefix}.winner", Market.MONEYLINE, Period.FULL_GAME),  # tennis
        # Baseball's main spread is ``run_line``; also emitting ``handicap``
        # duplicates the same ±1.5 contract under two market ids.
        *((
            (f"{prefix}.handicap", Market.SPREAD, Period.FULL_GAME),
            (f"{prefix}.asian_handicap", Market.SPREAD, Period.FULL_GAME),
        ) if prefix != "baseball" else ()),
        (f"{prefix}.run_line", Market.SPREAD, Period.FULL_GAME),
        (f"{prefix}.totals", Market.TOTAL, Period.FULL_GAME),
        (f"{prefix}.total", Market.TOTAL, Period.FULL_GAME),
        (f"{prefix}.total_goals", Market.TOTAL, Period.FULL_GAME),
        (f"{prefix}.moneyline_innings_1_to_5", Market.MONEYLINE, Period.FIRST_5_INNINGS),
        (f"{prefix}.handicap_innings_1_to_5", Market.SPREAD, Period.FIRST_5_INNINGS),
        (f"{prefix}.totals_innings_1_to_5", Market.TOTAL, Period.FIRST_5_INNINGS),
    ]
    allowed = MARKETS_BY_SPORT.get(fixture.sport, frozenset())
    for market_key, market, period in specs:
        if market not in allowed:
            continue
        blob = markets.get(market_key)
        if not isinstance(blob, dict):
            continue
        for sub_key, sub in (blob.get("submarkets") or {}).items():
            if not isinstance(sub, dict):
                continue
            sk = str(sub_key).casefold()
            if period is Period.FULL_GAME and "inning" in sk:
                outcome.skipped["period_out_of_scope"] += 1
                continue
            # Full-game windows: ``period=ft`` / ``ot&ft`` (team sports), or tennis
            # match ``period=default`` (may also list set1…set5 + walkover).
            if period is Period.FULL_GAME and sk.startswith("period="):
                if "period=ft" not in sk and "period=default" not in sk:
                    outcome.skipped["period_out_of_scope"] += 1
                    continue
            for sel in sub.get("selections") or []:
                if not isinstance(sel, dict):
                    continue
                if str(sel.get("status") or "SELECTION_ENABLED") != "SELECTION_ENABLED":
                    outcome.skipped["selection_disabled"] += 1
                    continue
                _emit_selection(
                    sel, raw, source, fixture, event_key, market, period, market_key, outcome
                )


def _emit_selection(
    sel: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    market: Market,
    period: Period,
    market_key: str,
    outcome: ParseOutcome,
) -> None:
    outcome_name = str(sel.get("outcome") or "").casefold()
    selection = {
        "home": Selection.HOME,
        "away": Selection.AWAY,
        "over": Selection.OVER,
        "under": Selection.UNDER,
        "draw": Selection.DRAW,
    }.get(outcome_name)
    if selection in (Selection.HOME, Selection.AWAY):
        selection = fixture.our_side(selection)
    if selection is None:
        outcome.skipped[f"outcome:{outcome_name or 'missing'}"] += 1
        return
    if selection is Selection.DRAW and not draw_is_priced(fixture.sport, period):
        outcome.reject(
            source,
            "draw_not_priced",
            f"draw on {fixture.sport.value}/{period.value} event {fixture.event_id}",
            event_id=fixture.event_id,
        )
        return

    try:
        decimal_odds = float(sel["price"])
    except (KeyError, TypeError, ValueError):
        outcome.skipped["missing_price"] += 1
        return
    if not is_plausible_decimal_odds(decimal_odds):
        outcome.skipped["implausible_odds"] += 1
        return

    line: float | None = None
    if market in (Market.SPREAD, Market.TOTAL):
        match = _PARAM_NUM.search(str(sel.get("params") or ""))
        if not match:
            outcome.skipped["missing_line"] += 1
            return
        stated = float(match.group(1)) + 0.0
        # ``handicap`` is stated from the home side's perspective, so one value
        # fixes both rows of a market: ``handicap=-1.5`` is home −1.5 / away +1.5.
        #
        # Both signs used to be treated as two framings of one contract, and
        # ``handicap &gt; 0`` was deleted as a "mirror restatement" — 358 rows in the
        # 2026-08-14 run. The bytes say otherwise. One baseball fixture,
        # ``baseball.run_line``, carries all four rows with independent prices:
        #
        #   handicap=+1.5   home 1.49   away 2.50
        #   handicap=-1.5   home 2.80   away 1.42
        #
        # 1.49 is not the mirror of 2.80. They are the two directions of the
        # handicap — the market where home is favoured, and the market where away
        # is — and a fixture has one of each. Keeping only the negative framing
        # made cloudbet's home line negative in **100%** of its stored rows, so
        # whenever the home team was the underdog the stored main line was the
        # wrong bet. That is what fired ``line_favours_the_other_competitor`` on
        # 12 of 33 shared fixtures.
        #
        # Only spreads negate. A total's two sides share one number — over 8.5
        # and under 8.5 — and negating the under produced ``NFL total -48.5``.
        if market is Market.SPREAD:
            line = stated if selection is Selection.HOME else -stated
        else:
            line = stated

    # One Cloudbet market blob holds the whole ladder.  Identity must include
    # the line so ±1.5 and ±2.5 (or 8.5 vs 9.5 totals) stay distinct markets.
    if market is Market.SPREAD and line is not None:
        # Signed, not absolute. The two handicap directions are different
        # contracts with different prices, and ``abs`` filed all four rows under
        # one id — two homes and two aways, which is the "two offers sharing a
        # key" shape ``src/schema.py`` warns fires ``spread_not_mirrored``.
        # ``stated`` is home-perspective and therefore constant across the two
        # rows of one market, which is exactly what a market id has to be.
        source_market_id = f"{market_key}|{stated:+g}"
    elif market is Market.TOTAL and line is not None:
        source_market_id = f"{market_key}|{line:g}"
    else:
        source_market_id = market_key

    is_alternate = False
    if market is Market.SPREAD and line is not None:
        if period is Period.FIRST_5_INNINGS:
            is_alternate = True
        elif fixture.sport is Sport.BASEBALL and abs(abs(line) - 1.5) > 1e-9:
            is_alternate = True
        elif fixture.sport is not Sport.BASEBALL and abs(line) != int(abs(line)) + 0.5:
            # Whole-number / non-half-point spreads are the ladder, not the main.
            is_alternate = True
    elif market is Market.TOTAL and line is not None:
        if period is Period.FIRST_5_INNINGS or abs(line - round(line)) < 1e-9:
            is_alternate = True

    try:
        outcome.quotes.append(
            priced_quote(
                fixture,
                source=source,
                raw=raw,
                event_key=event_key,
                market=market,
                period=period,
                selection=selection,
                line=line,
                is_alternate=is_alternate,
                decimal_odds=decimal_odds,
                american_odds=decimal_to_american(decimal_odds),
                implied_probability=implied_probability(decimal_odds),
                source_market_id=source_market_id,
                status=QuoteStatus.ACTIVE,
            )
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)
