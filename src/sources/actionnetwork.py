"""Action Network multi-book scoreboard, from the public web JSON.

The current site uses ``api.actionnetwork.com/web/v2/scoreboard/{sport}``, which
is the default here; v1 captures remain supported for deterministic replay. No
authentication is involved. One response carries several books Action Network
aggregates; this adapter is parameterized by ``book_id`` so many registered
sources share one parser and filter to one counterparty.

**The two versions publish different book catalogues, and neither is a superset
of the other.**  Measured on 2026-08-08 by asking both the same ids in the same
minute:

* Modern per-state ids answer on **v2**.  ``bookIds=74,122,246,255,280,1534,
  1906,2791,3547,4623`` — Pennsylvania's whole set — comes back keyed by all ten
  on the v2 MLB board.  On v1 that board carried *none* of them and answered with
  an unrelated set instead of an error, which is what a wrong base URL looks
  like: a 200 and plausible numbers for somebody else's licence.  Neither
  endpoint is uniform across leagues — v1 returned 74 and 122 on WNBA and NFL,
  and v2 returned nine of ten on WNBA and seven on NFL — but a catalogue that
  honours two of ten ids on two leagues is not one you can collect a state from.
* The offshore shelf answers on **v1 only**.  Asking ``21,35,2495`` in the same
  minute, v1 carried Bovada on NFL and Bovada with 1xBet on soccer; v2 carried
  neither on any of five leagues.  :data:`LEGACY_V1_BASE_URL` exists for exactly
  those two sources and for no other reason.
* Fliff (``2292``), Circa (``78``), SuperBook (``14``) and SugarHouse (``708``)
  answer on **neither**.

On v2 ``bookIds`` selects rather than expands: naming an id alone is enough — 79,
123, 74, 4623 and 246 each came back when asked for by themselves — and naming
ids that do not exist narrows the payload to the defaults (15 consensus, 30
opener) rather than adding to them.  So the v1-era workaround where bet365 only
appeared if Caesars was named is not needed on v2.  It is **not** true that every
named id always comes back: a book that is not pricing that league is simply
absent, which is why ``parse`` filters by id rather than trusting the request.
``fetch_book_ids`` still names the set while parse filters to this tenant's
``book_id``.

**Both endpoints need their settlement windows asked for, in different ways, and
getting it wrong is silent.**  On v1, sending ``period=game`` matches the site's
moneyline view and strips baseball ``firstfiveinnings`` / ``firstinning`` rows,
so it is never sent.  On v2 the default is the opposite: full-game only, unless
:data:`REQUESTED_PERIODS` names the windows.  Measured on one MLB slate, v2 went
from ``{event: 154}`` to ``{event: 154, firstfiveinnings: 126, firstinning: 96}``
when asked.  Every committed v2 fixture predates that discovery and carries
full-game rows only.

Field traps this parser exists to get right
-------------------------------------------

**``type`` is the settlement window, not a market name.**  ``game`` is full
game; ``live`` is in-play on the same fixture; ``firstfiveinnings`` /
``firstinning`` are baseball period markets.  Reading every odds object as
full-game would file live prices as pregame.

**American prices and lines share field names that look interchangeable.**
``spread_home`` is the handicap; ``spread_home_line`` is the American price on
that side.  ``total`` is the line; ``over`` / ``under`` are the prices.  Taking
the wrong field invents a line that is really a price.

**Home is a stated team id.**  ``home_team_id`` / ``away_team_id`` pick the
sides out of ``teams[]``; the array order is not reliable.
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
    MIN_DECIMAL_ODDS,
    american_to_decimal,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import (
    canonical_participant,
    competition_marker,
    is_pairing,
    is_statistic,
    with_marker,
)
from src.raw_store import RawResponse
from src.schema import (
    Market,
    Period,
    QuoteStatus,
    Selection,
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
    latest_per_endpoint,
    parse_iso_time,
    priced_quote,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "an_draftkings"

#: The endpoint every state-licensed republisher must use, and therefore the
#: default: a descriptor that forgets to choose gets the one that knows modern
#: per-state book ids.  It used to be the other way round, and the cost was that
#: nine of Pennsylvania's ten republished feeds returned somebody else's books
#: while reporting healthy.
DEFAULT_BASE_URL = "https://api.actionnetwork.com/web/v2/scoreboard"

#: The older endpoint, kept because it is the **only** one carrying the offshore
#: shelf (see the module docstring).  Two descriptors set it deliberately; it is
#: not a fallback, and nothing else may adopt it without a measurement showing
#: its books are absent from v2.
LEGACY_V1_BASE_URL = "https://api.actionnetwork.com/web/v1/scoreboard"

#: The settlement windows to ask for, on every request.
#:
#: **v2 answers with full-game markets only unless this is sent.**  On one MLB
#: slate the market groups went from ``{event: 154}`` to ``{event: 154,
#: firstfiveinnings: 126, firstinning: 96}`` when asked.  Measured across the
#: committed v1 fixtures, period rows are 35-57% of the parsed rows of every
#: tenant that produces any, and 40-65% of its baseball rows (``an_onexbet`` is
#: the one live tenant carrying none), so a v2 request without this drops between
#: a third and two-thirds of the board while every source still reports healthy —
#: the same shape of loss as asking the wrong book id, and just as quiet.
#:
#: Sent on v1 too, where it changes nothing: v1 returns every window by default
#: and ignores the parameter (measured 2026-08-08 — identical market types with
#: and without).  One request shape for both endpoints, because a conditional on
#: the base URL is a second place for this to rot.
#:
#: The spelling is exact.  ``periods`` plural is the parameter; singular
#: ``period`` is silently ignored, ``marketTypes`` is a 400, and a list with
#: spaces after the commas is accepted and answered with full-game only — the
#: worst of the four, since it looks like it worked.
REQUESTED_PERIODS = "event,firstfiveinnings,firstinning"

#: Action Network answers freely but a burst of many tenants × six sports is
#: enough to look rude; half a second keeps a full pass under a minute.
HOST_INTERVAL = 0.5

#: Matches the odds-board browser request.  Without these the scoreboard is more
#: likely to answer with a thinner book set.
AN_HEADERS: dict[str, str] = {
    "Referer": "https://www.actionnetwork.com/",
    "Origin": "https://www.actionnetwork.com",
}

_GAME_TYPE = "game"
_LIVE_TYPE = "live"

_BASEBALL_PERIOD_TYPES: dict[str, Period] = {
    "firstfiveinnings": Period.FIRST_5_INNINGS,
    "firstinning": Period.FIRST_1_INNING,
}

_DONE_STATUSES = frozenset({
    "complete",
    "completed",
    "closed",
    "final",
    "inprogress",
    "in_progress",
    "live",
    "suspended",
    "postponed",
    "cancelled",
    "canceled",
    "weatherdelay",
    "delayed",
})


@dataclass(frozen=True)
class LeaguePath:
    """One scoreboard path and the canonical league it carries.

    Soccer is the exception: the path is shared across competitions, and each
    game's ``league_name`` is mapped separately (or falls back to the catch-all).
    """

    path: str
    sport: Sport
    league: str | None


LEAGUE_PATHS: tuple[LeaguePath, ...] = (
    LeaguePath("mlb", Sport.BASEBALL, "MLB"),
    LeaguePath("wnba", Sport.BASKETBALL, "WNBA"),
    LeaguePath("nba", Sport.BASKETBALL, "NBA"),
    LeaguePath("nfl", Sport.FOOTBALL, "NFL"),
    LeaguePath("nhl", Sport.HOCKEY, "NHL"),
    LeaguePath("soccer", Sport.SOCCER, None),
)

PATH_BY_LEAGUE: dict[str, list[LeaguePath]] = {}
for _entry in LEAGUE_PATHS:
    if _entry.league is not None:
        PATH_BY_LEAGUE.setdefault(_entry.league, []).append(_entry)
    else:
        for _league in (
            "EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1", "SOCCER_OTHER",
        ):
            PATH_BY_LEAGUE.setdefault(_league, []).append(_entry)

PATHS: dict[str, LeaguePath] = {entry.path: entry for entry in LEAGUE_PATHS}

DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB", "WNBA", "NBA", "NFL", "NHL",
    "EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1", "SOCCER_OTHER",
)

SOCCER_LEAGUE_BY_NAME: dict[str, str] = {
    "epl": "EPL",
    "premier-league": "EPL",
    "eng.1": "EPL",
    "mls": "MLS",
    "usa.1": "MLS",
    "la-liga": "LA_LIGA",
    "laliga": "LA_LIGA",
    "spa.1": "LA_LIGA",
    "serie-a": "SERIE_A",
    "seriea": "SERIE_A",
    "ita.1": "SERIE_A",
    "bundesliga": "BUNDESLIGA",
    "ger.1": "BUNDESLIGA",
    "ligue-1": "LIGUE_1",
    "ligue1": "LIGUE_1",
    "fra.1": "LIGUE_1",
}

SOCCER_FALLBACK_LEAGUE = "SOCCER_OTHER"

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.SOCCER: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
}


def scoreboard_endpoint(path: str, book_id: int) -> str:
    """Stable label for one book's scoreboard response on *path*.

    The book id is in the label because the same JSON carries every book, and
    parse filters by the id encoded here — the same reason Kambi puts the
    operator token in its endpoint labels.
    """
    return f"scoreboard:{book_id}:{path}"


class ActionNetworkAdapter:
    """Collects one Action Network book's pregame game markets."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        book_id: int,
        fetch_book_ids: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        paths: list[str] = []
        for key in leagues:
            found = PATH_BY_LEAGUE.get(key)
            if found is None:
                raise KeyError(
                    f"Action Network has no scoreboard path for league {key!r}; known: "
                    f"{sorted(PATH_BY_LEAGUE)}"
                )
            for entry in found:
                if entry.path not in paths:
                    paths.append(entry.path)
        if not paths:
            raise ValueError("ActionNetworkAdapter needs at least one league to collect")
        self._paths: tuple[str, ...] = tuple(paths)
        self._leagues: tuple[str, ...] = tuple(dict.fromkeys(leagues))
        self._source_key = source_key
        self.book_id = int(book_id)
        # ``None`` omits the query param.  Asking for some book ids (71, 75)
        # removes them from the payload; Caesars/Bet365 and the offshore shelf
        # need an explicit ask.
        self.fetch_book_ids = fetch_book_ids
        self.base_url = base_url.rstrip("/")
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=HOST_INTERVAL,
            headers=AN_HEADERS,
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    @property
    def paths(self) -> tuple[str, ...]:
        return self._paths

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Moneyline, spread and total at the full-game window.

        *tier* changes nothing: one scoreboard request per path carries the
        whole market set, so there is no per-event follow-up to defer.
        """
        del tier
        by_league: dict[str, frozenset[Market]] = {}
        for path in self._paths:
            entry = PATHS[path]
            markets = MARKETS_BY_SPORT[entry.sport]
            if entry.league is not None:
                by_league[entry.league] = markets
            else:
                for key in (
                    "EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1",
                    "SOCCER_OTHER",
                ):
                    by_league[key] = markets
        return capabilities_from(by_league, self.leagues)

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        params: dict[str, str] = {"periods": REQUESTED_PERIODS}
        if self.fetch_book_ids:
            params["bookIds"] = self.fetch_book_ids
        for path in self._paths:
            tally.requested(path)
            try:
                raw = self._http.get(
                    f"{self.base_url}/{path}",
                    endpoint=scoreboard_endpoint(path, self.book_id),
                    params=params,
                )
            except SourceError as exc:
                log.info("%s: %s unavailable: %s", self._source_key, path, exc)
                tally.failed(path, exc)
                continue
            count = _game_count(raw)
            raws.append(raw)
            tally.produced(path, count)
        tally.require_something(what="pregame event")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_actionnetwork(raws)

    def close(self) -> None:
        self._http.close()


def _game_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    games = payload.get("games")
    return len(games) if isinstance(games, list) else 0


def parse_actionnetwork(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Action Network scoreboard responses into normalized rows.

    Pure: no network, no clock, no filesystem.  ``source`` and ``book_id`` both
    come off the envelopes (the latter from the endpoint label).
    """
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

    ordered = latest_per_endpoint(raws)
    _require_one_book(ordered, source)

    fixtures: dict[str, Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], Fixture, int, str]] = []
    for raw in ordered:
        book_id, path = _book_and_path(raw.endpoint)
        entry = PATHS.get(path)
        if entry is None or book_id is None:
            raise FormatChangeError(
                f"{source}: stored response for unknown scoreboard endpoint "
                f"{raw.endpoint!r}"
            )
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: expected a JSON object scoreboard"
            )
        games = payload.get("games")
        if not isinstance(games, list):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: scoreboard has no 'games' array"
            )
        for game in games:
            if not isinstance(game, dict):
                outcome.skipped["game_not_an_object"] += 1
                continue
            event_id = str(game.get("id") or "")
            if not event_id:
                outcome.reject(
                    source, "missing_event_id", f"game without an id in {raw.endpoint}"
                )
                continue
            fixture_key = f"{path}:{event_id}"
            if fixture_key in fixtures:
                outcome.skipped["duplicate_event"] += 1
                continue
            fixture = _accept_game(game, entry, source, raw.fetched_at, outcome)
            if fixture is None:
                continue
            fixtures[fixture_key] = fixture
            work.append((raw, game, fixture, book_id, fixture_key))

    if not fixtures:
        return outcome

    event_keys = resolve_doubleheaders(
        {key: (f.base_key, f.commence_time) for key, f in fixtures.items()}
    )

    for raw, game, fixture, book_id, fixture_key in work:
        markets = game.get("markets")
        if isinstance(markets, dict):
            _parse_v2_markets(
                markets=markets,
                book_id=book_id,
                raw=raw,
                source=source,
                fixture=fixture,
                event_key=event_keys[fixture_key],
                outcome=outcome,
            )
            continue
        for odds in game.get("odds") or []:
            if not isinstance(odds, dict):
                outcome.skipped["odds_not_an_object"] += 1
                continue
            if odds.get("book_id") != book_id:
                outcome.skipped["odds_for_other_book"] += 1
                continue
            _parse_odds(
                odds=odds,
                raw=raw,
                source=source,
                fixture=fixture,
                event_key=event_keys[fixture_key],
                outcome=outcome,
            )

    drop_duplicate_selections(source, outcome)
    return outcome


def _parse_v2_markets(
    *,
    markets: Mapping[str, Any],
    book_id: int,
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    """Decode the current v2 scoreboard while v1 captures keep replaying.

    V2 groups rows as ``book -> period -> market -> outcomes`` instead of the
    v1 flat ``odds`` list.  The endpoint still carries several books, so the
    registered source is selected from the book id embedded in the envelope's
    endpoint label exactly as it was for v1.
    """
    selected = markets.get(str(book_id)) or markets.get(book_id)
    if not isinstance(selected, dict):
        outcome.skipped["odds_for_other_book"] += _v2_row_count(markets)
        return

    outcome.skipped["odds_for_other_book"] += max(
        0, _v2_row_count(markets) - _v2_row_count(selected)
    )
    for period_name, market_map in selected.items():
        period = _v2_period(str(period_name), fixture.sport)
        if not isinstance(period, Period):
            outcome.skipped[period] += _v2_row_count(market_map)
            continue
        if not isinstance(market_map, dict):
            outcome.skipped["market_group_not_an_object"] += 1
            continue
        for market_name, rows in market_map.items():
            if not isinstance(rows, list):
                outcome.skipped["market_rows_not_an_array"] += 1
                continue
            usable = [row for row in rows if isinstance(row, dict)]
            outcome.skipped["odds_not_an_object"] += len(rows) - len(usable)
            if market_name not in {"moneyline", "spread", "total"}:
                outcome.skipped[f"market_out_of_scope:{market_name}"] += len(usable)
                continue
            _emit_v2_market(
                rows=usable,
                market_name=str(market_name),
                period=period,
                raw=raw,
                source=source,
                fixture=fixture,
                event_key=event_key,
                outcome=outcome,
            )


def _v2_row_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return sum(_v2_row_count(child) for child in value.values())
    return 0


def _v2_period(value: str, sport: Sport) -> Period | str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"event", "game"}:
        return Period.FULL_GAME
    if normalized in {"first_5_innings", "firstfiveinnings"}:
        return (
            Period.FIRST_5_INNINGS
            if sport is Sport.BASEBALL
            else f"period_out_of_scope:{normalized}"
        )
    if normalized in {"first_inning", "firstinning"}:
        return (
            Period.FIRST_1_INNING
            if sport is Sport.BASEBALL
            else f"period_out_of_scope:{normalized}"
        )
    return f"period_out_of_scope:{normalized or 'empty'}"


def _emit_v2_market(
    *,
    rows: Sequence[Mapping[str, Any]],
    market_name: str,
    period: Period,
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    by_side = {
        str(row.get("side") or "").strip().lower(): row
        for row in rows
        if str(row.get("side") or "").strip()
    }
    first = rows[0] if rows else {}
    market_id = str(first.get("market_id") or f"{fixture.event_id}:{market_name}")
    status = {
        side: 0 if str(row.get("line_status") or "normal").lower() == "normal" else 1
        for side, row in by_side.items()
    }

    if market_name == "moneyline":
        if fixture.sport is Sport.SOCCER and "draw" not in by_side:
            outcome.skipped["draw_voids_market"] += 1
            return
        _emit(
            raw, source, fixture, event_key, outcome,
            Market.MONEYLINE, period, market_id,
            tuple(
                (selection, by_side.get(side, {}).get("odds"), side, None)
                for side, selection in (
                    ("home", Selection.HOME),
                    ("away", Selection.AWAY),
                    ("draw", Selection.DRAW),
                )
            ),
            status,
        )
        return

    if market_name == "spread":
        sides = []
        for side, selection in (("home", Selection.HOME), ("away", Selection.AWAY)):
            row = by_side.get(side)
            if row is None:
                continue
            try:
                line = float(row.get("value")) + 0.0
            except (TypeError, ValueError):
                outcome.reject(
                    source, "unparseable_line",
                    f"spread/{side} on event {fixture.event_id} has "
                    f"value={row.get('value')!r}",
                    event_id=fixture.event_id,
                )
                continue
            sides.append((selection, row.get("odds"), side, line))
        if len(sides) == 2:
            _emit(
                raw, source, fixture, event_key, outcome,
                Market.SPREAD, period, market_id, tuple(sides), status,
            )
        return

    sides = []
    for side, selection in (("over", Selection.OVER), ("under", Selection.UNDER)):
        row = by_side.get(side)
        if row is None:
            continue
        try:
            line = float(row.get("value")) + 0.0
        except (TypeError, ValueError):
            outcome.reject(
                source, "unparseable_line",
                f"total/{side} on event {fixture.event_id} has "
                f"value={row.get('value')!r}",
                event_id=fixture.event_id,
            )
            continue
        sides.append((selection, row.get("odds"), side, line))
    if len(sides) == 2:
        _emit(
            raw, source, fixture, event_key, outcome,
            Market.TOTAL, period, market_id, tuple(sides), status,
        )


def _require_one_book(raws: Sequence[RawResponse], source: str) -> None:
    """Refuse a store that mixes two states' book ids under one source key.

    One adapter instance carries one :attr:`~ActionNetworkAdapter.book_id`, and
    every envelope a pass writes is labelled with it, so two ids under one
    source key can only mean an **out-of-state capture surviving beside the
    current one**.  The same brand is a different id per licence —
    ``an_caesars`` is 123 in New Jersey and 1906 in Pennsylvania — and the two
    labels differ, so :func:`latest_per_endpoint` keeps both however old one is.

    Parsing them together has no good outcome.  De-duplication below is per
    ``path:event_id``, so for a game both captures list the **lexically smaller
    endpoint label** wins irrespective of ``fetched_at``: 123 beats 1906, and a
    Pennsylvania run publishes New Jersey's prices as Pennsylvania's, which is
    the one thing state-locality exists to prevent.  Putting the book id in that
    key instead only moves the collision downstream to ``dedup_key``, where
    storage's UNIQUE constraint makes
    :func:`~src.sources._common.drop_duplicate_selections` reject whichever half
    of the board came second.

    Neither half is this state's price, so there is nothing to choose between
    and no merge worth writing.  Fail, naming both ids, so the stale capture is
    cleared rather than published.  Envelopes whose label does not decode are
    left to the caller, which raises with the label in hand.
    """
    labels: dict[int, str] = {}
    for raw in raws:
        book_id, path = _book_and_path(raw.endpoint)
        if book_id is None or not path:
            continue
        labels.setdefault(book_id, raw.endpoint)
    if len(labels) > 1:
        named = ", ".join(f"{book} ({labels[book]})" for book in sorted(labels))
        raise FormatChangeError(
            f"{source}: stored responses carry {len(labels)} book ids — {named}. "
            "One pass writes one book id, so these are two licences' captures in "
            "one store; clear the out-of-state one instead of parsing it as this "
            "state's price"
        )


def _book_and_path(endpoint: str) -> tuple[int | None, str]:
    kind, _, rest = endpoint.partition(":")
    if kind != "scoreboard" or not rest:
        return None, ""
    book_text, _, path = rest.partition(":")
    if not book_text or not path:
        return None, ""
    try:
        return int(book_text), path
    except ValueError:
        return None, ""


def _accept_game(
    game: Mapping[str, Any],
    entry: LeaguePath,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> Fixture | None:
    event_id = str(game.get("id"))
    status = str(game.get("status") or "").strip().lower()
    if status in _DONE_STATUSES:
        outcome.skipped[f"event_status:{status or 'empty'}"] += 1
        return None

    competition = _competition_for(game, entry, outcome)
    if competition is None:
        return None

    sides = _teams(game)
    if sides is None:
        outcome.skipped["non_game_event"] += 1
        return None
    home_name, away_name = sides
    marker = competition_marker(str(game.get("league_name") or ""), competition.sport)
    home_name = with_marker(home_name, marker) or home_name
    away_name = with_marker(away_name, marker) or away_name
    if any(is_pairing(part, competition.sport) for part in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None
    if any(is_statistic(part) for part in (home_name, away_name)):
        outcome.skipped["statistic_not_a_fixture"] += 1
        return None

    home = canonical_participant(home_name, competition)
    if home is None:
        # Abbreviations often resolve when the full name does not.
        for team in game.get("teams") or []:
            if isinstance(team, dict) and team.get("id") == game.get("home_team_id"):
                home = canonical_participant(str(team.get("abbr") or ""), competition)
                break
    away = canonical_participant(away_name, competition)
    if away is None:
        for team in game.get("teams") or []:
            if isinstance(team, dict) and team.get("id") == game.get("away_team_id"):
                away = canonical_participant(str(team.get("abbr") or ""), competition)
                break
    if home is None or away is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} in {competition.key}: "
            f"{away_name!r} -> {away.key if away else None}, "
            f"{home_name!r} -> {home.key if home else None}",
            event_id=event_id,
        )
        return None

    commence_time = parse_iso_time(game.get("start_time"))
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} has unreadable start_time {game.get('start_time')!r}",
            event_id=event_id,
        )
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    away_side, home_side = orient(
        away, home, competition, home=home if competition.has_home_away else None
    )
    return Fixture(
        event_id=event_id,
        sport=entry.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=home.key,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _competition_for(
    game: Mapping[str, Any],
    entry: LeaguePath,
    outcome: ParseOutcome,
) -> League | None:
    if entry.league is not None:
        return league_registry.league(entry.league)
    name = str(game.get("league_name") or "").strip().lower()
    if not name:
        outcome.skipped["league_unmapped"] += 1
        return None
    key = SOCCER_LEAGUE_BY_NAME.get(name, SOCCER_FALLBACK_LEAGUE)
    return league_registry.league(key)


def _teams(game: Mapping[str, Any]) -> tuple[str, str] | None:
    home_id = game.get("home_team_id")
    away_id = game.get("away_team_id")
    if home_id is None or away_id is None:
        return None
    by_id: dict[Any, Mapping[str, Any]] = {}
    for team in game.get("teams") or []:
        if isinstance(team, dict) and team.get("id") is not None:
            by_id[team["id"]] = team
    home = by_id.get(home_id)
    away = by_id.get(away_id)
    if home is None or away is None:
        return None
    home_name = _team_name(home)
    away_name = _team_name(away)
    if not home_name or not away_name:
        return None
    return home_name, away_name


def _team_name(team: Mapping[str, Any]) -> str:
    for key in ("full_name", "display_name", "abbr"):
        value = str(team.get(key) or "").strip()
        if value:
            return value
    return ""


def _period_for(odds_type: str, sport: Sport) -> Period | str:
    if odds_type == _GAME_TYPE:
        return Period.FULL_GAME
    if odds_type == _LIVE_TYPE:
        return "live_market"
    if odds_type in _BASEBALL_PERIOD_TYPES:
        if sport is not Sport.BASEBALL:
            return f"period_out_of_scope:{odds_type}"
        return _BASEBALL_PERIOD_TYPES[odds_type]
    return f"period_out_of_scope:{odds_type}"


def _parse_odds(
    *,
    odds: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    odds_type = str(odds.get("type") or "").strip().lower()
    period = _period_for(odds_type, fixture.sport)
    # Period is a StrEnum, so ``isinstance(..., str)`` is true for a real
    # period too — check the enum first or every full-game price is skipped.
    if not isinstance(period, Period):
        outcome.skipped[period] += 1
        return

    line_status = odds.get("line_status") if isinstance(odds.get("line_status"), dict) else {}

    ml_home, ml_away, draw = odds.get("ml_home"), odds.get("ml_away"), odds.get("draw")
    if ml_home is not None or ml_away is not None or draw is not None:
        require_draw = fixture.sport is Sport.SOCCER and period is Period.FULL_GAME
        if require_draw and draw is None:
            if ml_home is not None or ml_away is not None:
                outcome.skipped["draw_voids_market"] += 1
        else:
            _emit(
                raw, source, fixture, event_key, outcome,
                Market.MONEYLINE, period, f"{fixture.event_id}:{odds_type}:moneyline",
                (
                    (Selection.HOME, ml_home, "ml_home", None),
                    (Selection.AWAY, ml_away, "ml_away", None),
                    (Selection.DRAW, draw, "draw", None),
                ),
                line_status,
            )

    spread_home, spread_away = odds.get("spread_home"), odds.get("spread_away")
    if spread_home is not None and spread_away is not None:
        try:
            home_line = float(spread_home) + 0.0
            away_line = float(spread_away) + 0.0
        except (TypeError, ValueError):
            outcome.reject(
                source,
                "unparseable_line",
                f"spread on event {fixture.event_id} has "
                f"spread_home={spread_home!r} spread_away={spread_away!r}",
                event_id=fixture.event_id,
            )
        else:
            _emit(
                raw, source, fixture, event_key, outcome,
                Market.SPREAD, period, f"{fixture.event_id}:{odds_type}:spread",
                (
                    (Selection.HOME, odds.get("spread_home_line"), "spread_home", home_line),
                    (Selection.AWAY, odds.get("spread_away_line"), "spread_away", away_line),
                ),
                line_status,
            )

    total = odds.get("total")
    if total is not None and str(total).strip() != "":
        try:
            total_line = float(total) + 0.0
        except (TypeError, ValueError):
            outcome.reject(
                source,
                "unparseable_line",
                f"total on event {fixture.event_id} has total={total!r}",
                event_id=fixture.event_id,
            )
        else:
            _emit(
                raw, source, fixture, event_key, outcome,
                Market.TOTAL, period, f"{fixture.event_id}:{odds_type}:total",
                (
                    (Selection.OVER, odds.get("over"), "over", total_line),
                    (Selection.UNDER, odds.get("under"), "under", total_line),
                ),
                line_status,
            )


def _emit(
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
    market: Market,
    period: Period,
    market_id: str,
    sides: Sequence[tuple[Selection, Any, str, float | None]],
    line_status: Mapping[str, Any],
) -> None:
    for selection, american, status_key, side_line in sides:
        if american is None or str(american).strip() == "":
            if selection is not Selection.DRAW:
                outcome.skipped["outcome_without_price"] += 1
            continue
        if selection is Selection.DRAW and not draw_is_priced(fixture.sport, period):
            outcome.skipped["draw_not_priced_for_sport"] += 1
            continue
        try:
            american_odds = int(str(american).strip().replace("+", ""))
            decimal_odds = american_to_decimal(american_odds)
        except (TypeError, ValueError):
            outcome.reject(
                source,
                "unreadable_price",
                f"{market.value}/{selection.value} on event {fixture.event_id} "
                f"has american {american!r}",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            continue
        if decimal_odds < MIN_DECIMAL_ODDS:
            outcome.skipped["price_below_plausible_minimum"] += 1
            continue
        if not is_plausible_decimal_odds(decimal_odds):
            outcome.reject(
                source,
                "implausible_odds",
                f"{decimal_odds} on {market.value}/{selection.value} for event "
                f"{fixture.event_id}",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            continue
        selection = _orient_selection(selection, fixture)
        open_line = line_status.get(status_key) in (0, None)
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
                    line=side_line,
                    is_alternate=False,
                    decimal_odds=decimal_odds,
                    american_odds=american_odds,
                    implied_probability=implied_probability(decimal_odds),
                    source_market_id=market_id,
                    source_selection_id=f"{market_id}:{selection.value}",
                    status=QuoteStatus.ACTIVE if open_line else QuoteStatus.SUSPENDED,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(
                source,
                "invalid_quote",
                f"{market.value}/{selection.value} on event {fixture.event_id}: {exc}",
                event_id=fixture.event_id,
                market_id=market_id,
            )


def _orient_selection(selection: Selection, fixture: Fixture) -> Selection:
    if selection not in (Selection.HOME, Selection.AWAY):
        return selection
    return fixture.our_side(selection)
