"""Hard Rock Bet pregame markets, from the Amelco public stack.

Three calls make a jurisdiction-specific slate:

* ``/sportsbook/api/public/events/tree?segment=<state>`` — sport / competition
  map.
* ``/sportsbook/v1/api/getRootLadder`` — ``rootIndex`` → decimal / moneyline
  ladder.
* ``POST /java-graphql/graphql`` — events with markets whose selections carry
  ``rootIdx`` into that ladder.  The segment, online channel, and egress must
  all match the selected jurisdiction for live prices.

Prices are never taken as raw numbers off the GraphQL selection; a missing
ladder entry refuses the row rather than inventing an odds.
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
from src.participants import canonical_participant, is_pairing
from src.raw_store import RawResponse
from src.schema import Market, Period, QuoteStatus, Selection, Sport
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
    parse_iso_time,
    priced_quote,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "hardrock"
DEFAULT_API_BASE = "https://api.hardrocksportsbook.com"
DEFAULT_SEGMENT = "nj"
DEFAULT_CHANNEL = "NEW_JERSEY_ONLINE"
HOST_INTERVAL = 0.6

SPORT_SCOPES: tuple[tuple[str, Sport, tuple[str, ...]], ...] = (
    ("BASEBALL", Sport.BASEBALL, ("MLB",)),
    ("BASKETBALL", Sport.BASKETBALL, ("WNBA", "NBA")),
    ("AMERICAN_FOOTBALL", Sport.FOOTBALL, ("NFL",)),
    ("ICE_HOCKEY", Sport.HOCKEY, ("NHL",)),
    ("TENNIS", Sport.TENNIS, ("ATP", "WTA", "ATP_CHALLENGER", "ITF")),
    ("SOCCER", Sport.SOCCER, ("EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1")),
)

COMP_NAME_LEAGUES: tuple[tuple[str, str], ...] = (
    ("mlb", "MLB"),
    ("wnba", "WNBA"),
    ("nba", "NBA"),
    ("nfl", "NFL"),
    ("nhl", "NHL"),
    ("premier league", "EPL"),
    ("english premier", "EPL"),
    ("major league soccer", "MLS"),
    ("la liga", "LA_LIGA"),
    ("serie a", "SERIE_A"),
    ("bundesliga", "BUNDESLIGA"),
    ("ligue 1", "LIGUE_1"),
)

TENNIS_MARKERS: tuple[tuple[str, str], ...] = (
    ("itf", "ITF"),
    ("wta", "WTA"),
    ("challenger", "ATP_CHALLENGER"),
    ("atp", "ATP"),
)

#: Market ``type`` → (Market, Period).
#:
#: Baseball: only the ``FTEI`` (incl. extras) codes.  ``BASEBALL:FT:RR`` is a
#: different moneyline product that must not displace FTEI via dedup.
#: Soccer: only ``1X2`` (three-way).  Two-way ``FT:ML`` mashed with a draw from
#: 1X2 manufactures a false cross-book contract.
MARKET_TYPES: dict[str, tuple[Market, Period]] = {
    "BASEBALL:FTEI:ML": (Market.MONEYLINE, Period.FULL_GAME),
    "BASEBALL:FTEI:SPRD": (Market.SPREAD, Period.FULL_GAME),
    "BASEBALL:FTEI:OU": (Market.TOTAL, Period.FULL_GAME),
    "BASKETBALL:FT:ML": (Market.MONEYLINE, Period.FULL_GAME),
    "BASKETBALL:FT:SPRD": (Market.SPREAD, Period.FULL_GAME),
    "BASKETBALL:FT:OU": (Market.TOTAL, Period.FULL_GAME),
    "AMERICAN_FOOTBALL:FT:ML": (Market.MONEYLINE, Period.FULL_GAME),
    "AMERICAN_FOOTBALL:FT:SPRD": (Market.SPREAD, Period.FULL_GAME),
    "AMERICAN_FOOTBALL:FT:OU": (Market.TOTAL, Period.FULL_GAME),
    "ICE_HOCKEY:FT:ML": (Market.MONEYLINE, Period.FULL_GAME),
    "ICE_HOCKEY:FT:SPRD": (Market.SPREAD, Period.FULL_GAME),
    "ICE_HOCKEY:FT:OU": (Market.TOTAL, Period.FULL_GAME),
    "TENNIS:FT:ML": (Market.MONEYLINE, Period.FULL_GAME),
    "SOCCER:FT:1X2": (Market.MONEYLINE, Period.FULL_GAME),
    "SOCCER:FT:OU": (Market.TOTAL, Period.FULL_GAME),
}

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.TENNIS: frozenset({Market.MONEYLINE}),
    Sport.SOCCER: frozenset({Market.MONEYLINE, Market.TOTAL}),
}

EVENTS_QUERY = """
query betSync(
  $filters: [Filter]
  $segment: String
  $region: String
  $language: String
  $channel: String
  $sports: [String]
  $slice: Interval
  $marketTypes: [String]
) {
  betSync(
    cmsSegment: $segment
    region: $region
    language: $language
    channel: $channel
    eventParams: { marketTypes: $marketTypes, sportList: $sports }
  ) {
    events(filters: $filters, slice: $slice, sports: $sports) {
      data {
        id
        compId
        compName
        eventTime
        sport
        inplay
        outright
        name
        displayed
        markets(keyMarkets: true, marketTypes: $marketTypes) {
          id
          name
          type
          subtype
          line
          displayed
          suspended
          state
          selection {
            id
            name
            type
            displayed
            suspended
            rootIdx
          }
        }
      }
      count
    }
  }
}
""".strip()


class HardRockAdapter:
    """Collect Hard Rock Bet pregame game markets for one routed state."""

    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        api_base: str = DEFAULT_API_BASE,
        segment: str = DEFAULT_SEGMENT,
        channel: str = DEFAULT_CHANNEL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        proxy_state: str | None = None,
    ) -> None:
        wanted = (
            frozenset(leagues)
            if leagues is not None
            else frozenset(league for _, _, leagues_ in SPORT_SCOPES for league in leagues_)
        )
        scopes = [
            (code, sport, tuple(league for league in leagues_ if league in wanted))
            for code, sport, leagues_ in SPORT_SCOPES
        ]
        scopes = [(code, sport, leagues_) for code, sport, leagues_ in scopes if leagues_]
        if not scopes:
            raise ValueError(f"HardRockAdapter has no sport for leagues {sorted(wanted)!r}")
        self._scopes = tuple(scopes)
        self._wanted = wanted
        self._source_key = source_key
        self.api_base = api_base.rstrip("/")
        self.segment = segment
        self.channel = channel
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
        seen: dict[str, None] = {}
        for _code, _sport, leagues in self._scopes:
            for league in leagues:
                seen.setdefault(league, None)
        return tuple(seen)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {
                league: MARKETS_BY_SPORT[sport]
                for _code, sport, leagues in self._scopes
                for league in leagues
            },
            self.leagues,
        )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        headers = {
            "Origin": "https://app.hardrock.bet",
            "Referer": "https://app.hardrock.bet/",
            "Content-type": "application/json",
        }

        # Ladder and tree are prerequisites, not "scopes with a slate".  Counting
        # them as produced would let require_something pass when GraphQL is
        # geo-empty — the failure mode from CA without ODDS_HTTP_PROXY.
        try:
            raws.append(
                self._http.get(
                    f"{self.api_base}/sportsbook/v1/api/getRootLadder",
                    endpoint="ladder",
                    headers=headers,
                )
            )
        except SourceError as exc:
            raise SourceError(
                f"{self._source_key}:odds ladder unavailable: {exc}"
            ) from exc

        try:
            raws.append(
                self._http.get(
                    f"{self.api_base}/sportsbook/api/public/events/tree",
                    endpoint="tree",
                    params={"segment": self.segment},
                    headers=headers,
                )
            )
        except SourceError as exc:
            log.info("%s: event tree unavailable: %s", self._source_key, exc)

        for code, _sport, _leagues in self._scopes:
            label = f"events:{code}"
            tally.requested(label)
            try:
                raw = self._fetch_events(code, headers)
            except SourceError as exc:
                log.info("%s: %s unavailable: %s", self._source_key, code, exc)
                tally.failed(label, exc)
                continue
            raws.append(raw)
            count = _events_count(raw)
            if count == 0:
                tally.failed(
                    label,
                    SourceError(
                        f"{self._source_key}:{label}: GraphQL returned 0 events — "
                        "licensed-state ODDS_HTTP_PROXY is required from this egress"
                    ),
                )
            else:
                tally.produced(label, count)

        tally.require_something(what="pregame Hard Rock event")
        return raws

    def _fetch_events(self, sport_code: str, headers: Mapping[str, str]) -> RawResponse:
        market_types = [
            key for key in MARKET_TYPES if key.startswith(sport_code + ":")
        ]
        variables = {
            "channel": self.channel,
            "segment": self.segment,
            "region": "us",
            "language": "enus",
            "sports": [sport_code],
            "filters": [
                {"field": "displayed", "value": "true"},
                {"field": "outright", "value": "false"},
                # App bundle uses ``isInplay``, not ``inplay``.
                {"field": "isInplay", "value": "false"},
            ],
            "slice": {"from": 0, "to": 80},
            "marketTypes": market_types or None,
        }
        return self._http.post(
            f"{self.api_base}/java-graphql/graphql",
            endpoint=f"events-{sport_code}",
            json_body={
                "operationName": "betSync",
                "query": EVENTS_QUERY,
                "variables": variables,
            },
            headers=headers,
        )

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_hardrock(raws)

    def close(self) -> None:
        self._http.close()


def _events_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    events = (((payload.get("data") or {}).get("betSync") or {}).get("events") or {})
    data = events.get("data")
    return len(data) if isinstance(data, list) else 0


def parse_hardrock(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Pure: join GraphQL selections to the ladder by ``rootIdx``."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    latest = latest_per_endpoint(raws)
    ladder = _load_ladder(latest, source, outcome)
    if ladder is None:
        return outcome

    fixtures: dict[str, _Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], _Fixture]] = []

    for raw in latest:
        if not raw.endpoint.startswith("events-"):
            continue
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: expected object")
        if payload.get("errors"):
            outcome.reject(
                source,
                "graphql_errors",
                str(payload["errors"])[:200],
            )
            continue
        events = (((payload.get("data") or {}).get("betSync") or {}).get("events") or {})
        data = events.get("data")
        if not isinstance(data, list):
            raise FormatChangeError(f"{source}:{raw.endpoint}: missing events.data")
        for event in data:
            if not isinstance(event, dict):
                continue
            fixture = _accept_event(event, source, raw.fetched_at, outcome)
            if fixture is None:
                continue
            fixtures[fixture.event_id] = fixture
            work.append((raw, event, fixture))

    if not fixtures:
        return outcome

    resolved = resolve_doubleheaders(
        {eid: (f.base_key, f.commence_time) for eid, f in fixtures.items()}
    )
    for raw, event, fixture in work:
        _emit_markets(
            event.get("markets") or [],
            raw,
            source,
            fixture,
            resolved[fixture.event_id],
            ladder,
            outcome,
        )

    drop_duplicate_selections(source, outcome)
    return outcome


def _load_ladder(
    raws: Sequence[RawResponse], source: str, outcome: ParseOutcome
) -> dict[int, float] | None:
    for raw in raws:
        if raw.endpoint != "ladder":
            continue
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(f"{source}:ladder: expected object")
        rows = (
            (payload.get("PriceAdjustmentDetailsResponse") or {}).get("rootLadder")
            or payload.get("rootLadder")
        )
        if not isinstance(rows, list) or not rows:
            raise FormatChangeError(f"{source}:ladder: empty rootLadder")
        ladder: dict[int, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row["rootIndex"])
                decimal = float(row["decimal"])
            except (KeyError, TypeError, ValueError):
                continue
            ladder[idx] = decimal
        if not ladder:
            raise FormatChangeError(f"{source}:ladder: no usable rootIndex rows")
        return ladder
    outcome.reject(source, "missing_ladder", "no ladder envelope in capture")
    return None


@dataclass(frozen=True, kw_only=True)
class _Fixture(Fixture):
    """Adds the competitor named **first** in the Amelco event title.

    Amelco's ``A``/``B`` selection types are positional — ``A`` is the first name
    in ``event.name``, ``B`` the second — and which of those the book calls home
    depends on the title's separator (:data:`_SEPARATORS`).  Those were the same
    fact while every title read ``"Away @ Home"``, so the fallback mapped ``A`` to
    :attr:`Fixture.book_away`; under ``"Home vs Away"`` they are opposites, and a
    mapping through ``book_home`` would name the wrong side exactly when the
    selection name failed to resolve.  Position is what Amelco states, so position
    is what this carries.
    """

    title_first_key: str

    @property
    def title_second_key(self) -> str:
        """The competitor named second — whichever of ours the first one is not."""
        return (
            self.away.key if self.title_first_key == self.home.key else self.home.key
        )


def _accept_event(
    event: Mapping[str, Any],
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> _Fixture | None:
    if event.get("inplay") or event.get("outright"):
        outcome.skipped["inplay_or_outright"] += 1
        return None
    if event.get("displayed") is False:
        outcome.skipped["not_displayed"] += 1
        return None

    event_id = str(event.get("id") or "")
    if not event_id:
        outcome.skipped["missing_event_id"] += 1
        return None

    league_key = _competition_league(str(event.get("compName") or ""))
    if league_key is None:
        outcome.skipped["competition_out_of_scope"] += 1
        return None
    competition = league_registry.league(league_key)

    name = str(event.get("name") or "")
    split = _split_event_name(name)
    if split is None:
        outcome.skipped["missing_home_away"] += 1
        return None
    first_name, second_name, home_first = split
    if home_first is None and competition.has_home_away:
        # The separator carries no orientation this adapter has verified, and in a
        # league with a real home side guessing one is a wrong price rather than a
        # missing row: it inverts the event key, so the rows form their own fixture
        # and join nothing.  Skipped and counted instead — a loud gap.
        outcome.skipped["ambiguous_home_away_order"] += 1
        return None
    if any(is_pairing(part, competition.sport) for part in (first_name, second_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    first = canonical_participant(first_name, competition)
    second = canonical_participant(second_name, competition)
    if first is None or second is None or first.key == second.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id}: {first_name!r}/{second_name!r}",
            event_id=event_id,
        )
        return None

    event_time = event.get("eventTime")
    commence_time = (
        parse_epoch_time(event_time, unit="ms")
        if isinstance(event_time, (int, float))
        else parse_iso_time(event_time)
    )
    if commence_time is None:
        outcome.reject(source, "missing_commence_time", f"event {event_id}", event_id=event_id)
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    book_home = (first if home_first else second) if competition.has_home_away else None
    away_side, home_side = orient(first, second, competition, home=book_home)
    return _Fixture(
        event_id=event_id,
        sport=competition.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=book_home.key if book_home is not None else None,
        title_first_key=first.key,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


#: Event-name separator → whether the name lists the **home** side first.
#:
#: ``None`` means this adapter has not established an orientation for that
#: separator, which :func:`_accept_event` refuses to guess at in a league with a
#: real home side.
#:
#: Hard Rock changed template mid-season and the change was silent, because both
#: templates parse: the committed 2026-07-31 capture reads
#: ``"New York Yankees @ Boston Red Sox"`` and every capture from 2026-08-04
#: reads ``"Astros vs Blue Jays"`` — short names, new separator, **and the other
#: order**.  Read away-first, the 2026-08-11 IL slate put Hard Rock against the
#: other 33 feeds on 25 of 25 shared MLB fixtures and 15 of 15 where the pair
#: could be matched; validation's ``home_away_disagreement`` is what caught it.
#: Its rows had inverted event keys, so they formed their own fixtures and joined
#: nothing — Hard Rock silently stopped being comparable rather than pricing
#: anything wrong.
#:
#: ``" vs "`` meaning home-first is the same reading :mod:`src.sources.matchbook`
#: verified against Bovada's own ``competitors[].home`` flag.
_SEPARATORS: tuple[tuple[str, bool | None], ...] = (
    (" @ ", False),  # away first — the template served until 2026-08-01
    (" vs ", True),  # home first — the current template
    (" v ", None),   # unverified; tennis only, where orientation is imposed
    (" - ", None),   # unverified
)


def _split_event_name(name: str) -> tuple[str, str, bool | None] | None:
    """``(first, second, home_first)`` for a two-sided event name, or ``None``.

    *home_first* is ``None`` when the separator carries no orientation this
    adapter has verified.  That is harmless in tennis — those leagues set
    ``has_home_away=False`` and :func:`orient` reorders by participant key rather
    than trusting the book — and is refused everywhere else.
    """
    lower = name.casefold()
    for separator, home_first in _SEPARATORS:
        if separator in lower:
            idx = lower.index(separator)
            first = name[:idx].strip()
            second = name[idx + len(separator):].strip()
            if not first or not second:
                return None
            return first, second, home_first
    return None


def _competition_league(comp_name: str) -> str | None:
    hay = comp_name.casefold()
    if not hay:
        return None
    for marker, league in TENNIS_MARKERS:
        if marker in hay:
            return league
    for marker, league in COMP_NAME_LEAGUES:
        if marker in hay or hay == league.casefold():
            return league
    return None


def _emit_markets(
    markets: Sequence[Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    ladder: Mapping[int, float],
    outcome: ParseOutcome,
) -> None:
    allowed = MARKETS_BY_SPORT.get(fixture.sport, frozenset())
    for market in markets:
        if not isinstance(market, dict):
            continue
        if market.get("displayed") is False or market.get("suspended"):
            outcome.skipped["market_suspended_or_hidden"] += 1
            continue
        mtype = str(market.get("type") or "")
        mapped = MARKET_TYPES.get(mtype)
        if mapped is None:
            outcome.skipped["market_out_of_scope"] += 1
            continue
        market_kind, period = mapped
        if market_kind not in allowed:
            outcome.skipped["market_out_of_scope"] += 1
            continue

        market_line = market.get("line")
        try:
            market_line_f = float(market_line) if market_line is not None else None
        except (TypeError, ValueError):
            market_line_f = None

        for sel in market.get("selection") or []:
            if not isinstance(sel, dict):
                continue
            if sel.get("displayed") is False or sel.get("suspended"):
                outcome.skipped["selection_suspended_or_hidden"] += 1
                continue
            selection = _selection_for(sel, market_kind, fixture, outcome, source)
            if selection is None:
                continue
            try:
                root_idx = int(sel["rootIdx"])
            except (KeyError, TypeError, ValueError):
                outcome.reject(
                    source,
                    "missing_root_idx",
                    f"event {fixture.event_id} {mtype}",
                    event_id=fixture.event_id,
                )
                continue
            decimal = ladder.get(root_idx)
            if decimal is None:
                outcome.reject(
                    source,
                    "root_idx_not_on_ladder",
                    f"event {fixture.event_id} rootIdx={root_idx}",
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

            line = _line_for(
                selection,
                market_kind,
                market_line_f,
                selection_name=str(sel.get("name") or ""),
            )
            if market_kind in (Market.SPREAD, Market.TOTAL) and line is None:
                outcome.reject(
                    source,
                    "missing_line",
                    f"event {fixture.event_id} {mtype}",
                    event_id=fixture.event_id,
                )
                continue

            try:
                outcome.quotes.append(
                    priced_quote(
                        fixture,
                        source=source,
                        raw=raw,
                        event_key=event_key,
                        market=market_kind,
                        period=period,
                        selection=selection,
                        line=line,
                        decimal_odds=float(decimal),
                        american_odds=decimal_to_american(float(decimal)),
                        implied_probability=implied_probability(float(decimal)),
                        status=QuoteStatus.ACTIVE,
                        source_market_id=str(market.get("id") or "") or None,
                    )
                )
            except (TypeError, ValueError) as exc:
                outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)


def _line_for(
    selection: Selection,
    market_kind: Market,
    market_line: float | None,
    *,
    selection_name: str = "",
) -> float | None:
    if market_kind is Market.MONEYLINE:
        return None
    named_line = _named_line(selection_name)
    if market_kind is Market.TOTAL:
        return market_line if market_line is not None else named_line
    if named_line is not None:
        return named_line
    if market_line is None:
        return None
    if selection is Selection.AWAY:
        return abs(market_line)
    if selection is Selection.HOME:
        return -abs(market_line)
    return market_line


def _named_line(name: str) -> float | None:
    """Read the signed handicap appended to current Hard Rock selection names."""
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", name.strip())
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - regex accepts only float syntax
        return None


def _selection_for(
    sel: Mapping[str, Any],
    market_kind: Market,
    fixture: _Fixture,
    outcome: ParseOutcome,
    source: str,
) -> Selection | None:
    name = str(sel.get("name") or "").strip()
    sel_type = str(sel.get("type") or "").upper()
    if market_kind is Market.TOTAL:
        if sel_type == "OVER" or name.casefold().startswith("over"):
            return Selection.OVER
        if sel_type == "UNDER" or name.casefold().startswith("under"):
            return Selection.UNDER
        outcome.skipped["unknown_total_side"] += 1
        return None

    if name:
        participant_name = re.sub(r"\s+[+-]?\d+(?:\.\d+)?\s*$", "", name)
        participant = canonical_participant(participant_name, fixture.competition)
        if participant is not None:
            # Compare to book_home_key / oriented sides so tennis orient() cannot
            # flip Amelco A/B against the participant identity.
            if participant.key == fixture.home.key:
                return Selection.HOME
            if participant.key == fixture.away.key:
                return Selection.AWAY

    if name.casefold() in {"draw", "tie", "x"}:
        return Selection.DRAW

    # Amelco A/B only when the name did not resolve.  A = first name in the event
    # title, B = second — a statement about position and nothing else, which is
    # why this maps through ``title_first_key`` rather than through the book's
    # home side.  The two agree only under the ``"Away @ Home"`` template; under
    # ``"Home vs Away"`` they are opposites, and orient() swaps them again for
    # tennis, where the book's ordering is not ours.
    if sel_type in {"A", "B"}:
        target = (
            fixture.title_first_key if sel_type == "A" else fixture.title_second_key
        )
        if target == fixture.home.key:
            return Selection.HOME
        if target == fixture.away.key:
            return Selection.AWAY

    outcome.reject(
        source,
        "unresolved_selection",
        f"event {fixture.event_id}: {name!r} type={sel_type!r}",
        event_id=fixture.event_id,
    )
    return None


__all__ = [
    "HardRockAdapter",
    "SOURCE_KEY",
    "parse_hardrock",
]
