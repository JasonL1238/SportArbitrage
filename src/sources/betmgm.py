"""BetMGM / Entain CDS pregame markets, from the public fixtures feed.

``www.{state}.betmgm.com/cds-api/bettingoffer/fixtures`` is the same JSON the
public sportsbook page renders from.  No authentication is involved:
``x-bwin-accessid`` is a static application key baked into the public site (the
same class of credential as FanDuel's ``_ak``), not a user session.

Reachability is host- and identity-sensitive.  From California the Illinois and
West Virginia hosts answer; New Jersey may refuse the Illinois access id.  The
collector defaults to Chrome TLS impersonation — see ``docs/evidence/venues.md``,
"Previously blocked under plain ``httpx``".

Two payload shapes carry prices on the same fixture:

* ``optionMarkets[].options[].price`` — soccer, and baseball's headline lines.
* ``games[].results`` — basketball, hockey, football, tennis, and baseball's
  alternate packing of the same lines.

Both are read.  A market present in both is de-duplicated by
:func:`~src.sources._common.drop_duplicate_selections`.

Home/away is a stated field when Entain's V2 soccer payload sets
``properties.type`` to ``HomeTeam`` / ``AwayTeam``.  US-sport fixtures name the
event ``Away at Home`` and list the two teams in that order; the name is used
only to *identify* which participant is home, never as a free-text market.
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
from src.leagues import League
from src.normalize import (
    MIN_DECIMAL_ODDS,
    american_to_decimal,
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import (
    canonical_participant,
    competition_marker,
    is_futures,
    is_pairing,
    is_statistic,
    with_marker,
)
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
    Fixture,
    ScopeTally,
    SourceClient,
    TENNIS_TOURS_BY_GENDER,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_per_endpoint,
    priced_quote,
    tennis_tour,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "betmgm"
DEFAULT_BASE_URL = "https://www.il.betmgm.com"
DEFAULT_ACCESS_ID = "ZTg4YWEwMTgtZTlhYy00MWRkLWIzYWYtZjMzODI5ZDE0Mjc5"
DEFAULT_SUBDIVISION = "US-Illinois"

#: Entain's edge is happier when paced; an unpaced burst drew 301/403 after a
#: few dozen requests during the feasibility re-probe.
HOST_INTERVAL = 0.75

PAGE_SIZE = 50

_AT = re.compile(r"\s+at\s+", re.IGNORECASE)
_DASH = re.compile(r"\s+-\s+")
_LINE_IN_NAME = re.compile(
    r"(?P<prefix>over|under)?\s*(?P<line>[+-]?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SportScope:
    """One CDS ``sportIds`` request and the leagues it may carry."""

    sport_id: int
    sport: Sport
    leagues: tuple[str, ...]
    #: Competition ids kept when set; others are counted out of scope.
    competition_ids: frozenset[int] | None = None


#: One request per sport covers every league of that sport.  Competition ids pin
#: the US / top-flight competitions this pipeline collects; everything else on
#: the same sport feed (KBO, Big 3, club friendlies without an NHL id, …) is
#: skipped with a reason rather than coerced into a registered league.
SPORT_SCOPES: tuple[SportScope, ...] = (
    SportScope(23, Sport.BASEBALL, ("MLB",), frozenset({75})),
    SportScope(7, Sport.BASKETBALL, ("WNBA", "NBA"), frozenset({402})),  # NBA id TBD offseason
    SportScope(12, Sport.HOCKEY, ("NHL",), frozenset({34})),
    SportScope(11, Sport.FOOTBALL, ("NFL",), frozenset({35})),
    SportScope(5, Sport.TENNIS, ("ATP", "WTA", "ATP_CHALLENGER", "ITF")),
    SportScope(
        4,
        Sport.SOCCER,
        ("EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1", "SOCCER_OTHER"),
        None,  # no id gate: every competition is kept, routed or caught by SOCCER_OTHER
    ),
)

SCOPE_BY_SPORT_ID: dict[int, SportScope] = {s.sport_id: s for s in SPORT_SCOPES}

#: Competition id -> canonical league for non-tennis sports.
#:
#: Soccer is matched on **id, never on a name substring**.  The 2026-08-14
#: Illinois capture carries 109 soccer competitions on one feed, and among them
#: "LaLiga" sits beside "LaLiga 2", "Serie A" beside "Serie B" *and* "Brasileiro
#: Serie A", "Bundesliga" beside "2. Bundesliga", and "Ligue 1" beside "Ligue 2".
#: The substring table this replaced filed every one of those on the senior key,
#: and also swept up five unrelated competitions called "Premier League" — Welsh,
#: Maltese, Tanzanian, Armenian and Canadian — so the run reported 29 EPL
#: fixtures where there were 10.  Worse, it read "Frauen-Bundesliga" as
#: BUNDESLIGA, putting a women's fixture on the men's key.
#:
#: 102829 is the fix to a silent gap rather than a collision: BetMGM writes the
#: Spanish top flight as one word, "LaLiga", so the old ``"la liga"`` marker
#: never fired and ``LA_LIGA`` — declared in ``SPORT_SCOPES`` — was unreachable
#: from this feed.
#:
#: The cost of dropping the name fallback is that a tenant renumbering an id
#: degrades that competition to ``SOCCER_OTHER``.  That is a labelling loss which
#: ``league_disagreement`` reports loudly, where the substring rule's failure was
#: a wrong league nothing objected to.
COMPETITION_LEAGUE: dict[int, str] = {
    75: "MLB",
    402: "WNBA",
    34: "NHL",
    35: "NFL",
    102841: "EPL",  # England - Premier League
    104417: "MLS",
    102829: "LA_LIGA",  # "LaLiga"; 102830 is LaLiga 2 and stays SOCCER_OTHER
    102846: "SERIE_A",  # Italy; 102838 is Brasileiro Serie A and is not this
    102842: "BUNDESLIGA",  # 102845 is 2. Bundesliga
    102843: "LIGUE_1",  # 102376 is Ligue 2
}

#: Every soccer competition not routed above lands here rather than being
#: dropped.  BetMGM was the only registered adapter with neither a catch-all nor
#: a second-tier guard: of the 712 soccer fixtures it returned on 2026-08-14 it
#: discarded 599 as ``competition_out_of_scope``, 333 of which were fixtures
#: other books in the same run had priced.  League is deliberately not part of
#: event identity (``docs/INPUT_CONTRACT.md``), so the catch-all costs coverage
#: legibility and nothing else — see ``src/leagues.py``'s charter for the key.
SOCCER_CATCH_ALL = "SOCCER_OTHER"

#: Tennis competition-name markers, order-sensitive: women's / ITF before ATP so
#: a "WTA Challenger" cannot land as ATP_CHALLENGER.
#: What an unrecognised tennis competition is filed under.  A venue's policy,
#: not a fact about tennis, which is why the shared scan reports ``None`` and
#: this is applied here.
TENNIS_FALLBACK_LEAGUE = "ITF"


@dataclass(frozen=True)
class MarketRule:
    market: Market
    period: Period


#: Exact market names as BetMGM publishes them on ``optionMarkets`` / ``games``.
MARKET_RULES: dict[tuple[Sport, str], MarketRule] = {
    (Sport.BASEBALL, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASEBALL, "Run Line Spread"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.BASEBALL, "Totals"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.BASKETBALL, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASKETBALL, "Spread"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.BASKETBALL, "Totals"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.FOOTBALL, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.FOOTBALL, "Spread"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.FOOTBALL, "Totals"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.HOCKEY, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.HOCKEY, "Spread"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.HOCKEY, "Totals (including OT and shootouts)"): MarketRule(
        Market.TOTAL, Period.FULL_GAME
    ),
    (Sport.HOCKEY, "3-way result: Regular time"): MarketRule(
        Market.MONEYLINE, Period.REGULATION
    ),
    (Sport.TENNIS, "Match winner"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.SOCCER, "Match result"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.SOCCER, "Total goals"): MarketRule(Market.TOTAL, Period.FULL_GAME),
}

OUT_OF_SCOPE: dict[tuple[Sport, str], str] = {
    (Sport.BASKETBALL, "1st half moneyline"): "half_only_market",
    (Sport.BASKETBALL, "1st half spread"): "half_only_market",
    (Sport.BASKETBALL, "1st half totals"): "half_only_market",
    (Sport.FOOTBALL, "1st half moneyline"): "half_only_market",
    (Sport.FOOTBALL, "1st half spread"): "half_only_market",
    (Sport.FOOTBALL, "1st half totals"): "half_only_market",
    (Sport.TENNIS, "Set 1 winner"): "set_only_market",
    (Sport.TENNIS, "Set 2 winner"): "set_only_market",
    (Sport.TENNIS, "Total games: Match"): "alternate_scoring_unit_games",
    (Sport.TENNIS, "Player to win the most games in the match (player spread)"): (
        "alternate_scoring_unit_games"
    ),
    (Sport.SOCCER, "Double chance"): "combined_outcome_market",
    (Sport.SOCCER, "Both teams to score"): "yes_no_market",
    (Sport.SOCCER, "First team to score"): "first_to_score_market",
    (Sport.SOCCER, "To qualify"): "outright_or_series",
    (Sport.SOCCER, "Match result: Early payout"): "promo_market",
}

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    sport: frozenset(rule.market for (mapped, _), rule in MARKET_RULES.items() if mapped is sport)
    for sport in Sport
}


def fixtures_endpoint(sport_id: int, page: int) -> str:
    return f"fixtures:sport-{sport_id}:page-{page:02d}"


class BetMgmAdapter:
    """Collects BetMGM Illinois pregame game markets for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        access_id: str = DEFAULT_ACCESS_ID,
        subdivision: str = DEFAULT_SUBDIVISION,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        proxy_state: str | None = None,
    ) -> None:
        wanted = tuple(leagues) if leagues is not None else tuple(
            league for scope in SPORT_SCOPES for league in scope.leagues
        )
        scopes: list[SportScope] = []
        for scope in SPORT_SCOPES:
            kept = tuple(league for league in scope.leagues if league in wanted)
            if not kept:
                continue
            scopes.append(
                SportScope(
                    scope.sport_id,
                    scope.sport,
                    kept,
                    scope.competition_ids,
                )
            )
        if not scopes:
            raise ValueError(
                f"BetMgmAdapter has no sport scope for leagues {list(wanted)!r}; "
                f"known: {sorted({league for s in SPORT_SCOPES for league in s.leagues})}"
            )
        self._scopes: tuple[SportScope, ...] = tuple(scopes)
        self._wanted = frozenset(wanted)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.access_id = access_id
        self.subdivision = subdivision
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
        for scope in self._scopes:
            for league in scope.leagues:
                if league in self._wanted:
                    seen.setdefault(league, None)
        return tuple(seen)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {league: MARKETS_BY_SPORT[scope.sport] for scope in self._scopes for league in scope.leagues},
            self.leagues,
        )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for scope in self._scopes:
            label = f"sport:{scope.sport_id}"
            tally.requested(label)
            try:
                pages = self._fetch_sport(scope)
            except SourceError as exc:
                log.info("%s: sport %s unavailable: %s", self._source_key, scope.sport_id, exc)
                tally.failed(label, exc)
                continue
            raws.extend(pages)
            count = sum(_fixture_count(raw) for raw in pages)
            tally.produced(label, count)
        tally.require_something(what="pregame fixture")
        return raws

    def _fetch_sport(self, scope: SportScope) -> list[RawResponse]:
        pages: list[RawResponse] = []
        skip = 0
        page = 1
        while True:
            params = {
                "x-bwin-accessid": self.access_id,
                "lang": "en-us",
                "country": "US",
                "userCountry": "US",
                "subdivision": self.subdivision,
                "fixtureTypes": "Standard",
                "state": "Latest",
                "offerMapping": "Filtered",
                "offerCategories": "Gridable",
                "fixtureCategories": "Gridable",
                "sortBy": "Tags",
                "take": str(PAGE_SIZE),
                "skip": str(skip),
                "sportIds": str(scope.sport_id),
            }
            raw = self._http.get(
                f"{self.base_url}/cds-api/bettingoffer/fixtures",
                endpoint=fixtures_endpoint(scope.sport_id, page),
                params=params,
                headers={
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/",
                },
            )
            pages.append(raw)
            payload = raw.json()
            fixtures = payload.get("fixtures") if isinstance(payload, dict) else None
            if not isinstance(fixtures, list) or not fixtures:
                break
            total = int(payload.get("totalCount") or 0)
            skip += len(fixtures)
            page += 1
            if skip >= total or len(fixtures) < PAGE_SIZE:
                break
        return pages

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        # Parse is a pure function of the stored bytes.  League narrowing belongs
        # to fetch (and to the collector's post-filter), never to parse — otherwise
        # two differently-configured instances disagree over one capture.
        return parse_betmgm(raws)

    def close(self) -> None:
        self._http.close()


def _fixture_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    fixtures = payload.get("fixtures")
    return len(fixtures) if isinstance(fixtures, list) else 0


@dataclass(frozen=True)
class _Fixture(Fixture):
    """The shared fixture plus the one thing BetMGM's payload needs remembered."""

    participant_ids: Mapping[int, str]
    """BetMGM ``participantId`` -> canonical participant key."""

    marker: str | None = None
    """``"w"`` / ``"u19"`` / … when the *competition* name carries the
    distinction and the team names do not.  Kept on the fixture so an outcome
    label resolves through the same marked names the keys were built from."""

    away_listed_first: bool = False
    """Whether the book listed the away side first, which is what its
    ``sourceName`` "1"/"2" counts from.  See :func:`_sides`."""


def parse_betmgm(
    raws: Sequence[RawResponse],
    *,
    wanted_leagues: frozenset[str] | None = None,
) -> ParseOutcome:
    """Turn captured BetMGM fixtures responses into normalized rows.

    Pure: no network, no clock, no filesystem.  *wanted_leagues* is retained for
    offline narrowing in tests; collection always passes ``None``.
    """
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    wanted = wanted_leagues

    fixtures: dict[str, _Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], _Fixture]] = []
    for raw in latest_per_endpoint(raws):
        sport_id = _sport_id_of(raw.endpoint)
        scope = SCOPE_BY_SPORT_ID.get(sport_id)
        if scope is None:
            raise FormatChangeError(
                f"{source}: stored response for unknown sport endpoint {raw.endpoint!r}"
            )
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: expected a JSON object with fixtures"
            )
        for event in payload.get("fixtures") or []:
            if not isinstance(event, dict):
                outcome.skipped["fixture_not_an_object"] += 1
                continue
            event_id = str(event.get("id") or "")
            if not event_id:
                outcome.reject(
                    source, "missing_event_id", f"fixture without an id in {raw.endpoint}"
                )
                continue
            if event_id in fixtures:
                outcome.skipped["duplicate_event"] += 1
                continue
            fixture = _accept_event(event, scope, source, raw.fetched_at, outcome, wanted)
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
        seen_market_ids: set[str] = set()
        for om in event.get("optionMarkets") or []:
            if not isinstance(om, dict):
                outcome.skipped["option_market_not_an_object"] += 1
                continue
            _parse_option_market(
                market=om,
                raw=raw,
                source=source,
                fixture=fixture,
                event_key=event_keys[fixture.event_id],
                outcome=outcome,
                seen_market_ids=seen_market_ids,
            )
        for game in event.get("games") or []:
            if not isinstance(game, dict):
                outcome.skipped["game_market_not_an_object"] += 1
                continue
            _parse_game_market(
                market=game,
                raw=raw,
                source=source,
                fixture=fixture,
                event_key=event_keys[fixture.event_id],
                outcome=outcome,
                seen_market_ids=seen_market_ids,
            )

    drop_duplicate_selections(source, outcome)
    return outcome


def _sport_id_of(endpoint: str) -> int:
    # fixtures:sport-23:page-01
    try:
        _, sport_token, _ = endpoint.split(":", 2)
        return int(sport_token.removeprefix("sport-"))
    except (ValueError, AttributeError) as exc:
        raise FormatChangeError(f"unparseable BetMGM endpoint label {endpoint!r}") from exc


def _accept_event(
    event: Mapping[str, Any],
    scope: SportScope,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
    wanted: frozenset[str] | None,
) -> _Fixture | None:
    event_id = str(event.get("id"))
    if event.get("stage") == "Live":
        outcome.skipped["event_live"] += 1
        return None
    if event.get("isVirtual"):
        outcome.skipped["virtual_event"] += 1
        return None

    competition = _competition(event, scope, outcome)
    if competition is None:
        return None
    if wanted is not None and competition.key not in wanted:
        outcome.skipped["league_not_requested"] += 1
        return None

    sides = _sides(event, competition, outcome)
    if sides is None:
        return None
    home_name, away_name, book_home_name, id_map, away_listed_first = sides

    # Unrecognised soccer competitions now share one catch-all league, so the
    # venue's own name for the competition is read by nothing else — and this
    # feed carries Frauen-Bundesliga, u19 and reserve tiers beside their senior
    # sides.  Without the marker the two produce a byte-identical event key and
    # are priced as one market.
    marker = competition_marker(_competition_name(event), competition.sport)
    home_name = with_marker(home_name, marker) or home_name
    away_name = with_marker(away_name, marker) or away_name
    book_home_name = with_marker(book_home_name, marker) or book_home_name
    id_map = {pid: with_marker(name, marker) or name for pid, name in id_map.items()}

    if any(is_pairing(part, competition.sport) for part in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None
    if any(is_statistic(part) for part in (home_name, away_name)):
        outcome.skipped["statistic_not_a_fixture"] += 1
        return None
    if any(is_futures(part) for part in (home_name, away_name)):
        # An outright listed as a fixture — "Patro Eisden Maasmechelen - RSC
        # Anderlecht Futures" on the Belgian second tier.  Out of scope by the
        # contract, so counted rather than rejected.
        outcome.skipped["futures_not_a_fixture"] += 1
        return None

    home = canonical_participant(home_name, competition)
    away = canonical_participant(away_name, competition)
    book_home = canonical_participant(book_home_name, competition)
    if home is None or away is None or book_home is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} ({_name(event)!r}) in {competition.key}: "
            f"{away_name!r}/{home_name!r}",
            event_id=event_id,
        )
        return None

    commence_raw = event.get("startDate")
    commence_time = _parse_iso(commence_raw)
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} has unreadable startDate {commence_raw!r}",
            event_id=event_id,
        )
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    away_side, home_side = orient(
        away, home, competition, home=home if competition.has_home_away else None
    )
    # Map feed participant ids onto the *oriented* sides.  Comparing against the
    # pre-orient home/away and then writing home_side/away_side keys swapped the
    # tennis moneyline whenever title order disagreed with alphabetical orient
    # (Jodar listed first, Musetti alphabetically "home" → Jodar's price landed
    # on Musetti).
    participant_ids: dict[int, str] = {}
    for pid, name in id_map.items():
        who = canonical_participant(name, competition)
        if who is None:
            continue
        if who.key == home_side.key:
            participant_ids[pid] = home_side.key
        elif who.key == away_side.key:
            participant_ids[pid] = away_side.key

    return _Fixture(
        event_id=event_id,
        sport=scope.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=book_home.key if competition.has_home_away else home_side.key,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
        participant_ids=participant_ids,
        marker=marker,
        away_listed_first=away_listed_first,
    )


def _competition(
    event: Mapping[str, Any],
    scope: SportScope,
    outcome: ParseOutcome,
) -> League | None:
    raw = event.get("competition") or {}
    if not isinstance(raw, dict):
        outcome.skipped["competition_missing"] += 1
        return None
    try:
        competition_id = int(raw.get("id"))
    except (TypeError, ValueError):
        competition_id = None
    name = _name(raw) or ""

    if scope.sport is Sport.TENNIS:
        if "doubles" in name.lower():
            outcome.skipped["doubles_or_team_pairing"] += 1
            return None
        league_key = _tennis_league(name)
        return league_registry.league(league_key)

    if scope.sport is Sport.SOCCER:
        league_key = COMPETITION_LEAGUE.get(competition_id or -1) or SOCCER_CATCH_ALL
        return league_registry.league(league_key)

    if scope.competition_ids is not None and competition_id not in scope.competition_ids:
        outcome.skipped["competition_out_of_scope"] += 1
        return None
    league_key = COMPETITION_LEAGUE.get(competition_id or -1)
    if league_key is None:
        outcome.skipped["unknown_competition"] += 1
        return None
    return league_registry.league(league_key)


def _competition_name(event: Mapping[str, Any]) -> str:
    """The venue's own name for the competition, for the marker to read.

    Once an unrecognised competition lands on one catch-all league, nothing
    downstream reads this name — so a women's, reserve or youth competition
    whose *name* carries the distinction while the team names do not would
    produce a fixture byte-identical to the men's fixture of the same two clubs
    at another book.
    """
    raw = event.get("competition")
    return _name(raw) or "" if isinstance(raw, dict) else ""


def _tennis_league(name: str) -> str:
    return tennis_tour(name, markers=TENNIS_TOURS_BY_GENDER) or TENNIS_FALLBACK_LEAGUE


def _sides(
    event: Mapping[str, Any],
    competition: League,
    outcome: ParseOutcome,
) -> tuple[str, str, str, dict[int, str], bool] | None:
    """Return ``(home_name, away_name, book_home_name, id->name, away_listed_first)``.

    The last element is what BetMGM's ``sourceName`` "1"/"2" counts from, and it
    is not the same across sports.  On the US convention the title reads "Away at
    Home" and "1" is the away side; on soccer's 1X2 the payload states the roles
    and "1" is the home side.  Measured on the 2026-08-14 Illinois capture, over
    every Match result option whose label matched a side exactly: soccer
    ``sourceName`` 1 -> home 688 times and 2 -> away 694 times, with no
    counterexample.  The rule was applied unconditionally in the US direction, so
    a soccer option whose label did *not* match — "FC Dynamo Kiev" priced on a
    fixture whose participant is spelled "FC Dynamo Kyiv" — landed the away price
    on the home side.  It surfaced as a duplicate key rather than a wrong price
    only because the home option had already claimed that key first.
    """
    teams: list[tuple[int, str, str | None]] = []
    for part in event.get("participants") or []:
        if not isinstance(part, dict):
            continue
        props = part.get("properties") or {}
        if isinstance(props, dict) and props.get("type") == "Player":
            continue
        name = _name(part)
        if not name:
            continue
        try:
            pid = int(part.get("participantId"))
        except (TypeError, ValueError):
            continue
        side_type = props.get("type") if isinstance(props, dict) else None
        teams.append((pid, name, side_type if isinstance(side_type, str) else None))

    # Prefer stated HomeTeam/AwayTeam.
    home_stated = [t for t in teams if t[2] == "HomeTeam"]
    away_stated = [t for t in teams if t[2] == "AwayTeam"]
    if len(home_stated) == 1 and len(away_stated) == 1:
        home_name = home_stated[0][1]
        away_name = away_stated[0][1]
        id_map = {home_stated[0][0]: home_name, away_stated[0][0]: away_name}
        # Roles are stated, so there is no title order to read; soccer is the
        # only sport that reaches this branch and its "1" is the home side.
        return home_name, away_name, home_name, id_map, False

    # Strip typed leftovers; keep untyped team rows.
    plain = [(pid, name) for pid, name, side in teams if side in (None, "")]
    if len(plain) < 2:
        # Fall back to any non-player pair.
        plain = [(pid, name) for pid, name, _ in teams]
    if len(plain) != 2:
        outcome.skipped["non_game_event"] += 1
        return None

    event_name = _name(event) or ""
    # "Away at Home" — US sports convention on this feed.
    if _AT.search(event_name):
        left, right = _AT.split(event_name, maxsplit=1)
        away_name = _match_side(left.strip(), plain)
        home_name = _match_side(right.split("(")[0].strip(), plain)
        if away_name and home_name and away_name != home_name:
            id_map = {pid: name for pid, name in plain}
            return home_name, away_name, home_name, id_map, True

    # "Home - Away" / "A - B" (soccer V1 title order is home-first; tennis has no home).
    if _DASH.search(event_name):
        left, right = _DASH.split(event_name, maxsplit=1)
        first = _match_side(left.strip(), plain) or plain[0][1]
        second = _match_side(right.strip(), plain) or plain[1][1]
        id_map = {pid: name for pid, name in plain}
        return first, second, first, id_map, False

    # Last resort: participant list order, first=away, second=home for US sports.
    id_map = {pid: name for pid, name in plain}
    if competition.has_home_away:
        away_name, home_name = plain[0][1], plain[1][1]
        return home_name, away_name, home_name, id_map, True
    return plain[0][1], plain[1][1], plain[0][1], id_map, False


def _match_side(fragment: str, plain: Sequence[tuple[int, str]]) -> str | None:
    if not fragment:
        return None
    lowered = fragment.lower()
    for _, name in plain:
        if name.lower() == lowered or name.lower() in lowered or lowered in name.lower():
            return name
    # Short nicknames: "Rangers" vs "Texas Rangers"
    for _, name in plain:
        short = name.split()[-1].lower()
        if short == lowered or short in lowered.split():
            return name
    return None


def _resolve_market(sport: Sport, name: str) -> MarketRule | str:
    rule = MARKET_RULES.get((sport, name))
    if rule is not None:
        return rule
    named = OUT_OF_SCOPE.get((sport, name))
    if named is not None:
        return named
    if name.startswith("1st ") or "quarter" in name.lower() or "half" in name.lower():
        return "period_only_market"
    if name.startswith("Match result and") or name.startswith("Game "):
        return "combined_or_prop_market"
    return f"unmapped_market:{sport.value}:{name}"


def _parse_option_market(
    *,
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
    seen_market_ids: set[str],
) -> None:
    if str(market.get("status") or "") != "Visible":
        outcome.skipped["market_not_visible"] += 1
        return
    name = _name(market) or ""
    resolved = _resolve_market(fixture.sport, name)
    if isinstance(resolved, str):
        outcome.skipped[resolved] += 1
        return
    market_id = str(market.get("id") or "")
    if not market_id:
        outcome.reject(
            source,
            "missing_market_id",
            f"{name!r} on event {fixture.event_id} has no id",
            event_id=fixture.event_id,
        )
        return
    if market_id in seen_market_ids:
        outcome.skipped["duplicate_market_id"] += 1
        return
    seen_market_ids.add(market_id)

    line_hint = _parameter_line(market)
    for option in market.get("options") or []:
        if not isinstance(option, dict):
            outcome.skipped["option_not_an_object"] += 1
            continue
        if str(option.get("status") or "") != "Visible":
            outcome.skipped["outcome_not_visible"] += 1
            continue
        price = option.get("price") or {}
        if not isinstance(price, dict):
            outcome.skipped["outcome_without_price"] += 1
            continue
        quote = _build_quote(
            label=_name(option) or "",
            decimal_raw=price.get("odds"),
            american_raw=price.get("americanOdds"),
            participant_id=option.get("participantId"),
            attr=option.get("attr"),
            totals_prefix=_option_totals_prefix(option),
            source_name=_name(option.get("sourceName") or {})
            if isinstance(option.get("sourceName"), dict)
            else option.get("sourceName"),
            raw=raw,
            source=source,
            fixture=fixture,
            event_key=event_key,
            rule=resolved,
            market_id=market_id,
            market_name=name,
            line_hint=line_hint,
            outcome=outcome,
        )
        if quote is not None:
            outcome.quotes.append(quote)


def _parse_game_market(
    *,
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
    seen_market_ids: set[str],
) -> None:
    if str(market.get("visibility") or "") != "Visible":
        outcome.skipped["market_not_visible"] += 1
        return
    name = _name(market) or ""
    resolved = _resolve_market(fixture.sport, name)
    if isinstance(resolved, str):
        outcome.skipped[resolved] += 1
        return
    market_id = str(market.get("id") or "")
    if not market_id:
        outcome.reject(
            source,
            "missing_market_id",
            f"{name!r} on event {fixture.event_id} has no id",
            event_id=fixture.event_id,
        )
        return
    if market_id in seen_market_ids:
        outcome.skipped["duplicate_market_id"] += 1
        return
    seen_market_ids.add(market_id)

    line_hint = _float_or_none(market.get("attr"))
    for result in market.get("results") or []:
        if not isinstance(result, dict):
            outcome.skipped["outcome_not_an_object"] += 1
            continue
        if str(result.get("visibility") or "") != "Visible":
            outcome.skipped["outcome_not_visible"] += 1
            continue
        quote = _build_quote(
            label=_name(result) or "",
            decimal_raw=result.get("odds"),
            american_raw=result.get("americanOdds"),
            participant_id=result.get("playerId") or result.get("participantId"),
            attr=result.get("attr"),
            totals_prefix=result.get("totalsPrefix"),
            source_name=_name(result.get("sourceName") or {})
            if isinstance(result.get("sourceName"), dict)
            else result.get("sourceName"),
            raw=raw,
            source=source,
            fixture=fixture,
            event_key=event_key,
            rule=resolved,
            market_id=market_id,
            market_name=name,
            line_hint=line_hint,
            outcome=outcome,
        )
        if quote is not None:
            outcome.quotes.append(quote)


def _build_quote(
    *,
    label: str,
    decimal_raw: Any,
    american_raw: Any,
    participant_id: Any,
    attr: Any,
    totals_prefix: Any,
    source_name: Any,
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    rule: MarketRule,
    market_id: str,
    market_name: str,
    line_hint: float | None,
    outcome: ParseOutcome,
) -> Quote | None:
    selection = _selection_for(
        source=source,
        fixture=fixture,
        rule=rule,
        label=label,
        participant_id=participant_id,
        totals_prefix=totals_prefix,
        source_name=source_name,
        outcome=outcome,
        market_id=market_id,
        market_name=market_name,
    )
    if selection is None:
        return None

    if decimal_raw is None:
        outcome.skipped["outcome_without_price"] += 1
        return None
    try:
        decimal_odds = float(decimal_raw)
    except (TypeError, ValueError):
        outcome.reject(
            source,
            "unreadable_price",
            f"odds {decimal_raw!r} on {market_name!r} for event {fixture.event_id}",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None
    if not is_plausible_decimal_odds(decimal_odds) or decimal_odds < MIN_DECIMAL_ODDS:
        outcome.skipped["implausible_price"] += 1
        return None

    line = _line_for(rule.market, attr, label, line_hint)
    if rule.market in MARKETS_REQUIRING_LINE and line is None:
        outcome.reject(
            source,
            "missing_line",
            f"{market_name!r} option {label!r} on event {fixture.event_id} has no line",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None

    try:
        american = _american_odds(american_raw, decimal_odds, outcome)
    except ValueError:
        outcome.skipped["implausible_price"] += 1
        return None

    return priced_quote(
        fixture,
        source=source,
        raw=raw,
        event_key=event_key,
        market=rule.market,
        period=rule.period,
        selection=selection,
        line=line,
        decimal_odds=decimal_odds,
        american_odds=american,
        implied_probability=implied_probability(decimal_odds),
        source_market_id=market_id,
        status=QuoteStatus.ACTIVE,
    )


def _selection_for(
    *,
    source: str,
    fixture: _Fixture,
    rule: MarketRule,
    label: str,
    participant_id: Any,
    totals_prefix: Any,
    source_name: Any,
    outcome: ParseOutcome,
    market_id: str,
    market_name: str,
) -> Selection | None:
    if rule.market is Market.TOTAL:
        prefix = str(totals_prefix or "").lower()
        lowered = label.lower()
        if prefix == "over" or lowered.startswith("over"):
            return Selection.OVER
        if prefix == "under" or lowered.startswith("under"):
            return Selection.UNDER
        outcome.reject(
            source,
            "unknown_selection",
            f"total option {label!r} on {market_name!r} for event {fixture.event_id}",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None

    if label.lower() in {"tie", "draw", "x"}:
        if not draw_is_priced(fixture.sport, rule.period):
            outcome.reject(
                source,
                "draw_not_priced",
                f"a draw on {fixture.sport.value}/{rule.period.value} for event "
                f"{fixture.event_id}",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            return None
        return Selection.DRAW

    priced_key: str | None = None
    try:
        pid = int(participant_id) if participant_id is not None else None
    except (TypeError, ValueError):
        pid = None
    if pid is not None:
        priced_key = fixture.participant_ids.get(pid)

    if priced_key is None:
        # Marked on both sides or neither.  The fixture's participant names carry
        # the competition marker; the outcome label does not, and ``_label_matches``
        # falls back to comparing the label against the name's *last* token — which
        # on a marked name is the marker itself, so a literal "W" option would have
        # matched the women's side of any fixture.
        marked = with_marker(label, fixture.marker) or label
        for key, participant in (
            (fixture.home.key, fixture.home),
            (fixture.away.key, fixture.away),
        ):
            if _label_matches(marked, participant.name):
                priced_key = key
                break

    if priced_key is None and fixture.competition.has_home_away and str(source_name) in {"1", "2"}:
        # "1" is whichever side the book listed first — the away side on an
        # "Away at Home" US title, the home side on soccer's 1X2.  Reading it as
        # away unconditionally put the away price on the home key for any soccer
        # option whose label did not match, which is measured in ``_sides``.
        first_is_away = str(source_name) == "1"
        if not fixture.away_listed_first:
            first_is_away = not first_is_away
        priced_key = fixture.away.key if first_is_away else fixture.home.key

    if priced_key is None:
        outcome.reject(
            source,
            "unknown_selection",
            f"option {label!r} on {market_name!r} for event {fixture.event_id}",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None

    return Selection.HOME if priced_key == fixture.home.key else Selection.AWAY


def _american_odds(american_raw: Any, decimal_odds: float, outcome: ParseOutcome) -> int:
    """Prefer the feed's American price when it agrees with decimal; else derive.

    Entain sometimes rounds the two formats independently far enough that a
    strict cross-check fails.  Getting this wrong in the American direction
    (keeping a disagreeing feed value) fails the source contract; deriving from
    decimal only loses the feed's preferred display form.
    """
    derived = decimal_to_american(decimal_odds)
    if american_raw is None:
        return derived
    try:
        value = int(american_raw)
    except (TypeError, ValueError):
        outcome.repaired["feed_american_odds_unreadable"] += 1
        return derived
    if -100 < value < 100:
        outcome.repaired["feed_american_odds_not_a_price"] += 1
        return derived
    try:
        payout = american_to_decimal(value) - 1.0
    except ValueError:
        outcome.repaired["feed_american_odds_unreadable"] += 1
        return derived
    expected = decimal_odds - 1.0
    if expected <= 0 or abs(payout - expected) / expected > 0.01:
        outcome.repaired["feed_american_odds_disagreed_with_decimal"] += 1
        return derived
    return value


def _label_matches(label: str, name: str) -> bool:
    cleaned = _LINE_IN_NAME.sub("", label).strip().lower()
    lowered = name.lower()
    if not cleaned:
        return False
    if cleaned == lowered or cleaned in lowered or lowered in cleaned:
        return True
    return cleaned == name.split()[-1].lower()


def _line_for(
    market: Market,
    attr: Any,
    label: str,
    line_hint: float | None,
) -> float | None:
    if market is Market.MONEYLINE:
        return None
    if market is Market.SPREAD:
        value = _float_or_none(attr)
        if value is not None:
            return value
        match = _LINE_IN_NAME.search(label)
        return float(match.group("line")) if match else None
    if market is Market.TOTAL:
        if line_hint is not None:
            return line_hint
        value = _float_or_none(attr)
        if value is not None:
            return value
        match = _LINE_IN_NAME.search(label)
        return float(match.group("line")) if match else None
    return None


def _parameter_line(market: Mapping[str, Any]) -> float | None:
    for param in market.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        if param.get("key") == "DecimalValue":
            return _float_or_none(param.get("value"))
    return None


def _option_totals_prefix(option: Mapping[str, Any]) -> str | None:
    params = option.get("parameters") or {}
    if isinstance(params, dict):
        types = params.get("optionTypes") or []
        if types:
            return str(types[0])
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _name(node: Any) -> str:
    """Read Entain's ``{value: ...}`` name object, or a parent that wraps one."""
    if isinstance(node, dict):
        if "value" in node and not isinstance(node.get("value"), dict):
            value = node.get("value")
            return str(value).strip() if value is not None else ""
        nested = node.get("name")
        if isinstance(nested, dict):
            value = nested.get("value")
            return str(value).strip() if value is not None else ""
        if isinstance(nested, str):
            return nested.strip()
    return str(node).strip() if node is not None else ""
