"""1xBet LineFeed game markets, from the public ``Get1x2_VZip`` feed.

Championship discovery uses ``GetChampsZip``; events come from
``Get1x2_VZip?sports={id}&champs={li}&count=40&lng=en&…`` with short-key
fields: ``O1``/``O2`` teams, ``S`` unix start, ``L`` league label, and ``E``
selections ``{T, P, C, G, CE?}``.

Clear types only: ``T=1/2/3`` moneyline (home/draw/away), ``T=7/8`` handicap,
``T=9/10`` total over/under.  Unknown ``T`` values increment the skipped counter.
"""
from __future__ import annotations

import logging
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
from src.participants import canonical_participant, is_pairing
from src.raw_store import RawResponse
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Sport, draw_is_priced
from src.sources._common import (
    Fixture,
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_per_endpoint,
    parse_epoch_time,
    priced_quote,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "onexbet"
DEFAULT_BASE_URL = "https://1xbet.com/service-api/LineFeed"
HOST_INTERVAL = 0.4
EVENT_COUNT = 40

SPORT_IDS: dict[int, Sport] = {
    5: Sport.BASEBALL,
    3: Sport.BASKETBALL,
    13: Sport.FOOTBALL,
    2: Sport.HOCKEY,
    4: Sport.TENNIS,
    1: Sport.SOCCER,
}

#: Exact / fragment league labels (upper) → canonical key.  Longer first.
LEAGUE_FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("USA. MLB", "MLB"),
    ("ENGLAND. PREMIER LEAGUE", "EPL"),
    ("SPAIN. LA LIGA", "LA_LIGA"),
    ("ITALY. SERIE A", "SERIE_A"),
    ("GERMANY. BUNDESLIGA", "BUNDESLIGA"),
    ("FRANCE. LIGUE 1", "LIGUE_1"),
    ("USA. MLS", "MLS"),
    ("USA. NFL", "NFL"),
    ("NFL. PRESEASON", "NFL"),
    ("WNBA", "WNBA"),
    ("NBA", "NBA"),
    ("NHL", "NHL"),
    ("NFL", "NFL"),
    ("ATP", "ATP"),
    ("WTA", "WTA"),
    ("MLS", "MLS"),
)

LEAGUE_NOISE: tuple[str, ...] = (
    "ALTERNATIVE",
    "STATISTICS",
    "DOUBLES",
    "WINNER",
    "SPECIALS",
    "NEXT PRO",
    "MLS+",
    "WOMEN",
    "2. BUNDESLIGA",
)

DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB", "WNBA", "NBA", "NFL", "NHL",
    "ATP", "WTA", "ATP_CHALLENGER", "ITF",
    "EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1",
)

SELECTION_TYPES: dict[int, tuple[Market, Selection]] = {
    1: (Market.MONEYLINE, Selection.HOME),
    2: (Market.MONEYLINE, Selection.DRAW),
    3: (Market.MONEYLINE, Selection.AWAY),
    # Two-way moneylines (basketball / American football without a regulation draw).
    401: (Market.MONEYLINE, Selection.HOME),
    402: (Market.MONEYLINE, Selection.AWAY),
    # With a ``P`` line these are handicaps; without ``P`` on hockey they are the
    # OT/SO moneyline (G=2).  Resolved in ``_build_quote``.
    7: (Market.SPREAD, Selection.HOME),
    8: (Market.SPREAD, Selection.AWAY),
    9: (Market.TOTAL, Selection.OVER),
    10: (Market.TOTAL, Selection.UNDER),
}

#: Sports that settle a moneyline in OT/SO.  Their LineFeed 1X2 (T=1/2/3) is
#: regulation-only; the priced full-game moneyline is T=7/8 with no line.
_OT_MONEYLINE_SPORTS = frozenset({Sport.HOCKEY})

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.SOCCER: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.TENNIS: frozenset({Market.MONEYLINE}),
}

LINE_PARAMS = {"lng": "en", "tf": "2200000", "tz": "0", "mode": "4", "country": "1"}


class OneXBetAdapter:
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
        self._sport_ids = tuple(sid for sid, sport in SPORT_IDS.items() if sport in sports)
        if not self._sport_ids:
            raise ValueError(f"OneXBetAdapter has no sport for leagues {list(wanted)!r}")
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
        """Moneyline/spread/total for team sports; tennis moneyline only."""
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
        for sport_id in self._sport_ids:
            label = f"sport:{sport_id}"
            tally.requested(label)
            try:
                batch = self._fetch_sport(sport_id)
            except SourceError as exc:
                log.info("%s: sport %s unavailable: %s", self._source_key, sport_id, exc)
                tally.failed(label, exc)
                continue
            raws.extend(batch)
            tally.produced(
                label,
                sum(_event_count(r) for r in batch if r.endpoint.startswith("events:")),
            )
        tally.require_something(what="pregame event")
        return raws

    def _fetch_sport(self, sport_id: int) -> list[RawResponse]:
        # GetChampsZip rejects ``mode`` (HTTP 406); keep the shared locale params only.
        champs = self._http.get(
            f"{self.base_url}/GetChampsZip",
            endpoint=f"champs:sport-{sport_id}",
            params={
                "sport": str(sport_id),
                "lng": LINE_PARAMS["lng"],
                "tf": LINE_PARAMS["tf"],
                "tz": LINE_PARAMS["tz"],
                "country": LINE_PARAMS["country"],
            },
        )
        pages = [champs]
        for champ_id, league in _wanted_champs(champs, self._wanted):
            try:
                pages.append(
                    self._http.get(
                        f"{self.base_url}/Get1x2_VZip",
                        endpoint=f"events:sport-{sport_id}:champ-{champ_id}",
                        params={
                            "sports": str(sport_id),
                            "champs": str(champ_id),
                            "count": str(EVENT_COUNT),
                            **LINE_PARAMS,
                        },
                    )
                )
            except SourceError as exc:
                log.info(
                    "%s: champ %s (%s) unavailable: %s",
                    self._source_key,
                    champ_id,
                    league,
                    exc,
                )
        return pages

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        # Pure: league narrowing belongs to fetch / collector filter, not parse.
        return parse_onexbet(raws)

    def close(self) -> None:
        self._http.close()


def _event_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    value = payload.get("Value") if isinstance(payload, dict) else None
    return len(value) if isinstance(value, list) else 0


def league_from_label(label: str) -> str | None:
    upper = label.strip().upper()
    if not upper:
        return None
    # Tennis first — tour tokens beat the noise filter (no "WOMEN" in "WTA").
    if "DOUBLE" in upper or "WINNER" in upper or "SPECIAL" in upper:
        return None
    if "WTA" in upper:
        return "WTA"
    if "ATP" in upper:
        return "ATP_CHALLENGER" if "CHALLENGER" in upper else "ATP"
    if "ITF" in upper:
        return "ITF"
    if any(noise in upper for noise in LEAGUE_NOISE):
        return None
    for fragment, key in LEAGUE_FRAGMENTS:
        if fragment in upper:
            if key == "BUNDESLIGA" and "2." in upper:
                continue
            if key == "NBA" and "WNBA" in upper:
                continue
            return key
    return None


def _wanted_champs(
    raw: RawResponse, wanted: frozenset[str]
) -> list[tuple[int, str]]:
    try:
        payload = raw.json()
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    for row in payload.get("Value") or []:
        if not isinstance(row, dict):
            continue
        league = league_from_label(str(row.get("L") or ""))
        if league is None or league not in wanted:
            continue
        try:
            champ_id = int(row["LI"])
        except (KeyError, TypeError, ValueError):
            continue
        if champ_id in seen:
            continue
        seen.add(champ_id)
        found.append((champ_id, league))
    return found


def parse_onexbet(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Pure: no network, no clock, no filesystem.  Source from the envelope."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    fixtures: dict[str, Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], Fixture]] = []

    for raw in latest_per_endpoint(raws):
        sport = _sport_of_endpoint(raw.endpoint)
        if sport is None:
            # champs: / listing envelopes are fetch scaffolding, not priced rows.
            if raw.endpoint.startswith(("champs:", "events-list")):
                continue
            raise FormatChangeError(f"{source}: unknown sport endpoint {raw.endpoint!r}")
        if not (raw.body or "").strip():
            outcome.skipped["empty_linefeed"] += 1
            continue
        try:
            payload = raw.json()
        except ValueError as exc:
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: LineFeed body is not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: expected object")
        for event in payload.get("Value") or []:
            if not isinstance(event, dict):
                outcome.skipped["event_not_an_object"] += 1
                continue
            fixture = _accept(event, source, raw.fetched_at, outcome)
            if fixture is None:
                continue
            if fixture.event_id in fixtures:
                outcome.skipped["duplicate_event"] += 1
                continue
            fixtures[fixture.event_id] = fixture
            work.append((raw, event, fixture))

    if not fixtures:
        return outcome

    resolved = resolve_doubleheaders(
        {eid: (f.base_key, f.commence_time) for eid, f in fixtures.items()}
    )
    for raw, event, fixture in work:
        for row in event.get("E") or []:
            if not isinstance(row, dict):
                outcome.skipped["selection_not_an_object"] += 1
                continue
            quote = _build_quote(
                row, raw, source, fixture, resolved[fixture.event_id], outcome
            )
            if quote is not None:
                outcome.quotes.append(quote)

    drop_duplicate_selections(source, outcome)
    return outcome


def _sport_id_of(endpoint: str) -> int:
    try:
        return int(endpoint.split(":")[1].removeprefix("sport-"))
    except (IndexError, ValueError) as exc:
        raise FormatChangeError(f"unparseable 1xBet endpoint {endpoint!r}") from exc


def _sport_of_endpoint(endpoint: str) -> Sport | None:
    """Map an envelope label to a sport (``events:sport-N:champ-…``)."""
    if endpoint.startswith("events:"):
        return SPORT_IDS.get(_sport_id_of(endpoint))
    return None


def _accept(
    event: Mapping[str, Any],
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> Fixture | None:
    event_id = str(event.get("I") or "")
    if not event_id:
        outcome.skipped["missing_event_id"] += 1
        return None
    league_key = league_from_label(str(event.get("L") or ""))
    if league_key is None:
        outcome.skipped["competition_out_of_scope"] += 1
        return None
    competition = league_registry.league(league_key)

    home_name = str(event.get("O1") or "").strip()
    away_name = str(event.get("O2") or "").strip()
    if not home_name or not away_name:
        outcome.skipped["missing_home_away"] += 1
        return None
    lowered = f"{home_name} {away_name}".lower()
    if "(points)" in lowered or home_name.lower().startswith("home ("):
        outcome.skipped["statistic_not_a_fixture"] += 1
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

    commence_time = parse_epoch_time(event.get("S"), unit="s")
    if commence_time is None:
        outcome.reject(source, "missing_commence_time", f"event {event_id}", event_id=event_id)
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    away_side, home_side = orient(
        away, home, competition, home=home if competition.has_home_away else None
    )
    return Fixture(
        event_id=event_id,
        sport=competition.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=home.key,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _build_quote(
    row: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> Quote | None:
    try:
        type_code = int(row["T"])
        decimal_odds = float(row["C"])
    except (KeyError, TypeError, ValueError):
        outcome.skipped["bad_price_row"] += 1
        return None
    mapped = SELECTION_TYPES.get(type_code)
    if mapped is None:
        outcome.skipped[f"unknown_selection_type:{type_code}"] += 1
        return None
    market, selection = mapped
    # Hockey's OT/SO moneyline reuses the handicap type codes with no ``P``.
    if (
        type_code in (7, 8)
        and row.get("P") is None
        and fixture.sport in _OT_MONEYLINE_SPORTS
    ):
        market = Market.MONEYLINE
    if market not in MARKETS_BY_SPORT.get(fixture.sport, frozenset()):
        outcome.skipped[f"sport_market_out_of_scope:{market.value}"] += 1
        return None
    # Regulation 1X2 on hockey underrounds once the draw is dropped; use T=7/8.
    if (
        type_code in (1, 2, 3)
        and fixture.sport in _OT_MONEYLINE_SPORTS
        and market is Market.MONEYLINE
    ):
        outcome.skipped["regulation_three_way_unpriced"] += 1
        return None
    if not is_plausible_decimal_odds(decimal_odds):
        outcome.skipped["implausible_odds"] += 1
        return None

    if selection in (Selection.HOME, Selection.AWAY):
        selection = fixture.our_side(selection)
    if selection is Selection.DRAW and not draw_is_priced(fixture.sport, Period.FULL_GAME):
        outcome.skipped["draw_not_priced"] += 1
        return None

    line: float | None = None
    if market in (Market.SPREAD, Market.TOTAL):
        if row.get("P") is None:
            outcome.skipped["missing_line"] += 1
            return None
        try:
            line = float(row["P"]) + 0.0
        except (TypeError, ValueError):
            outcome.skipped["missing_line"] += 1
            return None
        # Corners / cards / team-shots arrive as huge "totals" on the same
        # LineFeed as match goals.  Drop anything outside the league ladder.
        if market is Market.TOTAL:
            low, high = fixture.competition.plausible_line_range
            if not (low <= line <= high):
                outcome.skipped["soccer_total_out_of_scale" if fixture.sport is Sport.SOCCER else "total_out_of_band"] += 1
                return None
        elif abs(line) > 12.0 and fixture.sport in (
            Sport.SOCCER, Sport.HOCKEY, Sport.BASEBALL
        ):
            outcome.skipped["line_out_of_scale"] += 1
            return None

    is_alternate = market in (Market.SPREAD, Market.TOTAL) and row.get("CE") != 1
    # Type codes are per-side (1 vs 3, 7 vs 8, 9 vs 10).  Market identity must
    # be side-independent so home/away (or over/under) group as one market.
    group = row.get("G")
    if market is Market.SPREAD and line is not None:
        market_id = f"SP:G{group}:L{abs(line):g}"
    elif market is Market.TOTAL and line is not None:
        market_id = f"TOT:G{group}:L{line:g}"
    else:
        market_id = f"ML:G{group}"

    try:
        return priced_quote(
            fixture,
            source=source,
            raw=raw,
            event_key=event_key,
            market=market,
            period=Period.FULL_GAME,
            selection=selection,
            line=line,
            is_alternate=is_alternate,
            decimal_odds=decimal_odds,
            american_odds=decimal_to_american(decimal_odds),
            implied_probability=implied_probability(decimal_odds),
            source_market_id=market_id,
            status=QuoteStatus.ACTIVE,
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)
        return None
