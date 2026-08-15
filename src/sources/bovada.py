"""Bovada game markets, from the public coupon feed its own site reads.

``www.bovada.lv/services/sports/event/coupon/events/A/description/{path}`` with
``marketFilterId=def`` returns one league's whole slate **with prices**, and with
the default filter it returns exactly the three game markets and nothing else.
One request per league, no per-event follow-up: the cheapest sportsbook in the
pipeline per market collected.

Offshore rather than US-licensed, which is why it answers at all — every
US-licensed book left is behind Akamai, Cloudflare or a CloudFront WAF (see
``docs/evidence/venues.md``, "Original California blocks").

Almost everything here is a structured field, which is unusual and welcome:

* ``competitors[].home`` states which side is home, so no event name is parsed to
  find out.  The other books' name conventions had to be verified against this
  one.
* ``outcomes[].type`` is ``H``/``A``/``D``/``O``/``U``, so a selection is read
  rather than inferred from a runner's spelling.
* ``price.handicap`` is signed from that runner's own perspective, which is
  exactly the orientation :class:`~src.schema.Quote` wants.

Field traps this parser exists to get right
-------------------------------------------

**The market name alone does not say which window it settles on.**  A tennis
event carries "Moneyline" at period "Match" *and* at period "Live Match", and the
second is an in-play price on the same screen.  The table is keyed on
``(sport, market name, period name)``, so a live market cannot be read as a
pregame one.

**"Total" in tennis counts games.**  So do "Game Spread" and "Set Spread" — a
different scoring unit from anything else here quotes, so tennis collects the
match winner alone, as it does for every other source.

**Soccer's window is called "Regulation Time".**  That is 90 minutes plus
stoppage, which is what :attr:`~src.vocab.Period.FULL_GAME` means for soccer;
cup extra time is a different contract and is named differently.

**A doubleheader is marked in prose and numbered by the clock.**  ``notes`` says
"Double Header Game #1", but the ordinal that matters is derived from start times
across every source by :func:`src.events.reconcile_event_keys`, so the note is
deliberately not read — a per-source ordinal is the mis-join this pipeline
already had once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.normalize import (
    MIN_DECIMAL_ODDS,
    american_to_decimal,
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import (
    canonical_participant,
    is_pairing,
    is_statistic,
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

SOURCE_KEY = "bovada"
DEFAULT_BASE_URL = "https://www.bovada.lv/services/sports/event/coupon/events/A/description"

#: The default market filter, which returns the three game markets and nothing
#: else.  Asking for everything would return props and futures that are all
#: counted out of scope anyway, at several times the payload size.
MARKET_FILTER = "def"

_OPEN = "O"

#: Bovada rate-limits an unpaced caller: seven league requests at the shared
#: default drew ``429`` on five of them, which reads downstream as five leagues
#: with no fixtures.  One request per second is what the whole configured set
#: answers cleanly at, and at fourteen leagues that is still fourteen seconds.
HOST_INTERVAL = 1.0

#: Bovada writes even money as a word.  Not a corrupt field — a real price, in a
#: spelling ``int()`` cannot read, and left unhandled it silently discarded the
#: feed's own American value in favour of a derived one.
_EVEN_MONEY = "EVEN"


@dataclass(frozen=True)
class LeaguePath:
    """One Bovada coupon path and the canonical league it carries."""

    path: str
    sport: Sport
    league: str


#: Every path this adapter collects.  Adding one here is enough to make it
#: selectable; nothing else in the module is keyed on the path.
LEAGUE_PATHS: tuple[LeaguePath, ...] = (
    LeaguePath("baseball/mlb", Sport.BASEBALL, "MLB"),
    LeaguePath("basketball/wnba", Sport.BASKETBALL, "WNBA"),
    LeaguePath("basketball/nba", Sport.BASKETBALL, "NBA"),
    LeaguePath("hockey/nhl", Sport.HOCKEY, "NHL"),
    LeaguePath("football/nfl", Sport.FOOTBALL, "NFL"),
    LeaguePath("tennis/atp", Sport.TENNIS, "ATP"),
    LeaguePath("tennis/wta", Sport.TENNIS, "WTA"),
    LeaguePath("soccer/europe/england/premier-league", Sport.SOCCER, "EPL"),
    LeaguePath("soccer/north-america/united-states/mls", Sport.SOCCER, "MLS"),
    LeaguePath("soccer/europe/spain/la-liga", Sport.SOCCER, "LA_LIGA"),
    LeaguePath("soccer/europe/italy/serie-a", Sport.SOCCER, "SERIE_A"),
    LeaguePath("soccer/europe/germany/1-bundesliga", Sport.SOCCER, "BUNDESLIGA"),
    LeaguePath("soccer/europe/france/ligue-1", Sport.SOCCER, "LIGUE_1"),
    LeaguePath("soccer/south-america/brazil/brasileirao-serie-a", Sport.SOCCER, "SOCCER_OTHER"),
)

PATH_BY_LEAGUE: dict[str, list[LeaguePath]] = {}
for _entry in LEAGUE_PATHS:
    PATH_BY_LEAGUE.setdefault(_entry.league, []).append(_entry)

PATHS: dict[str, LeaguePath] = {entry.path: entry for entry in LEAGUE_PATHS}

DEFAULT_LEAGUES: tuple[str, ...] = tuple(dict.fromkeys(entry.league for entry in LEAGUE_PATHS))


@dataclass(frozen=True)
class MarketRule:
    market: Market
    period: Period


#: Exact ``(sport, market name, period name)`` -> what it settles as.
#:
#: The period name is in the key and not an afterthought.  A tennis event carries
#: "Moneyline" at both "Match" and "Live Match", and the second is an in-play
#: price sitting beside the pregame one in the same payload; keyed on the market
#: name alone, half the tennis rows would be in-play prices wearing a pregame
#: label.
MARKET_RULES: dict[tuple[Sport, str, str], MarketRule] = {
    (Sport.BASEBALL, "Moneyline", "Game"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASEBALL, "Runline", "Game"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.BASEBALL, "Total", "Game"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.BASKETBALL, "Moneyline", "Game"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASKETBALL, "Point Spread", "Game"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.BASKETBALL, "Total", "Game"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.FOOTBALL, "Moneyline", "Game"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.FOOTBALL, "Point Spread", "Game"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.FOOTBALL, "Total", "Game"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    # Hockey: the full-game window at Bovada includes overtime and the shootout,
    # which is what FULL_GAME means for hockey here.
    (Sport.HOCKEY, "Moneyline", "Game"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.HOCKEY, "Puck Line", "Game"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.HOCKEY, "Total", "Game"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    # Soccer: "Regulation Time" is 90 minutes plus stoppage.
    (Sport.SOCCER, "3-Way Moneyline", "Regulation Time"): MarketRule(
        Market.MONEYLINE, Period.FULL_GAME
    ),
    (Sport.SOCCER, "Goal Spread", "Regulation Time"): MarketRule(
        Market.SPREAD, Period.FULL_GAME
    ),
    (Sport.SOCCER, "Total", "Regulation Time"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.TENNIS, "Moneyline", "Match"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
}

#: Deliberately not collected, with the reason each is a different contract.
OUT_OF_SCOPE: dict[tuple[Sport, str, str], str] = {
    (Sport.TENNIS, "Game Spread", "Match"): "alternate_scoring_unit_games",
    (Sport.TENNIS, "Set Spread", "Match"): "alternate_scoring_unit_sets",
    (Sport.TENNIS, "Total", "Match"): "alternate_scoring_unit_games",
    (Sport.SOCCER, "Draw No Bet", "Regulation Time"): "draw_voids_market",
    (Sport.SOCCER, "Double Chance", "Regulation Time"): "combined_outcome_market",
    (Sport.SOCCER, "Both Teams To Score", "Regulation Time"): "yes_no_market",
}

#: Period-name fragments that mark a window out of scope whatever the market is.
#: Checked before the tables, because an in-play price must never be read as a
#: pregame one however familiar the market name looks.
OUT_OF_SCOPE_PERIODS: tuple[tuple[str, str], ...] = (
    ("live", "live_market"),
    ("1st half", "half_only_market"),
    ("2nd half", "half_only_market"),
    ("half", "half_only_market"),
    ("quarter", "quarter_only_market"),
    ("period", "period_only_market"),
    ("inning", "inning_only_market"),
    ("set", "set_only_market"),
)

#: ``outcomes[].type`` -> canonical selection.  Structured, so no runner name is
#: parsed to find out which side a price is on.
OUTCOME_TYPES: dict[str, Selection] = {
    "H": Selection.HOME,
    "A": Selection.AWAY,
    "D": Selection.DRAW,
    "O": Selection.OVER,
    "U": Selection.UNDER,
}

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    sport: frozenset(
        rule.market
        for (mapped, _, _), rule in MARKET_RULES.items()
        if mapped is sport and rule.period is Period.FULL_GAME
    )
    for sport in Sport
}


def coupon_endpoint(path: str) -> str:
    """Stable label for one league's coupon response."""
    return f"coupon:{path}"


class BovadaAdapter:
    """Collects Bovada's pregame game markets for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        paths: list[str] = []
        for key in leagues:
            found = PATH_BY_LEAGUE.get(key)
            if found is None:
                raise KeyError(
                    f"Bovada has no coupon path for league {key!r}; known: "
                    f"{sorted(PATH_BY_LEAGUE)}"
                )
            for entry in found:
                if entry.path not in paths:
                    paths.append(entry.path)
        if not paths:
            raise ValueError("BovadaAdapter needs at least one league to collect")
        self._paths: tuple[str, ...] = tuple(paths)
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
        seen: dict[str, None] = {}
        for path in self._paths:
            seen.setdefault(PATHS[path].league, None)
        return tuple(seen)

    @property
    def paths(self) -> tuple[str, ...]:
        return self._paths

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Moneyline, spread and total everywhere except tennis.

        *tier* changes nothing: one coupon request per league carries the whole
        market set, so there is no per-event follow-up to defer.
        """
        del tier
        return capabilities_from(
            {PATHS[path].league: MARKETS_BY_SPORT[PATHS[path].sport] for path in self._paths},
            self.leagues,
        )

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for path in self._paths:
            tally.requested(path)
            try:
                raw = self._http.get(
                    f"{self.base_url}/{path}",
                    endpoint=coupon_endpoint(path),
                    params={"marketFilterId": MARKET_FILTER, "lang": "en"},
                )
            except SourceError as exc:
                # A league Bovada is not currently carrying answers 404, which is
                # an off day rather than a failure of the book.
                log.info("%s: %s unavailable: %s", self._source_key, path, exc)
                tally.failed(path, exc)
                continue
            count = _event_count(raw)
            # Kept even when it holds nothing, for the same reason: an empty
            # coupon is what a renamed field looks like, and the failure that
            # follows carries no payload of its own.
            raws.append(raw)
            tally.produced(path, count)
        tally.require_something(what="pregame event")
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_bovada(raws)

    def close(self) -> None:
        self._http.close()


def _event_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    if not isinstance(payload, list):
        return 0
    return sum(len(group.get("events") or []) for group in payload if isinstance(group, dict))


# ── parsing (pure) ───────────────────────────────────────────────────────────


#: ``book_home_key`` on the fixtures below is the competitor **Bovada** flagged
#: ``home: true`` — not always ``home``.  Where a league has no real home side —
#: tennis — :func:`src.events.orient` imposes its own ordering by participant key,
#: and it disagrees with the book's arbitrary ordering about half the time.  An
#: ``outcomes[].type`` of ``H`` means *Bovada's* home side, so translating it needs
#: that field and not ``home``.
#:
#: Getting it wrong is not a missing row, it is a price attached to the wrong
#: player: two books' prices for one match end up mirrored, both legs of a
#: "position" back the same competitor, and the report shows a guaranteed profit on
#: two perfectly ordinary prices.  Caught by
#: ``test_books_sharing_an_event_agree_on_the_favourite``, which is exactly the
#: fault it was written for after the same thing happened to Pinnacle.


def parse_bovada(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Bovada coupon responses into normalized rows.

    Pure: no network, no clock, no filesystem.
    """
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

    fixtures: dict[str, Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], Fixture]] = []
    for raw in latest_per_endpoint(raws):
        entry = PATHS.get(_path_of(raw.endpoint))
        if entry is None:
            raise FormatChangeError(
                f"{source}: stored response for unknown coupon path {raw.endpoint!r}"
            )
        payload = raw.json()
        if isinstance(payload, dict) and not payload:
            # An empty scope arrives as ``{}`` where a slate is an array — the
            # NHL coupon in the off-season, live.  ``fetch_raw``'s own
            # ``_event_count`` reads that as zero events without complaint;
            # raising here instead threw away every *other* scope's rows with
            # it, which cost 843 proven-parseable quotes across 166 events on
            # four of the ten stored runs.  A body that is malformed rather
            # than empty still raises below.
            outcome.skipped[f"empty_coupon_body:{_path_of(raw.endpoint)}"] += 1
            continue
        if not isinstance(payload, list):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: expected a JSON array of coupon groups"
            )
        for group in payload:
            if not isinstance(group, dict):
                outcome.skipped["group_not_an_object"] += 1
                continue
            for event in group.get("events") or []:
                if not isinstance(event, dict):
                    outcome.skipped["event_not_an_object"] += 1
                    continue
                event_id = str(event.get("id") or "")
                if not event_id:
                    outcome.reject(
                        source, "missing_event_id", f"event without an id in {raw.endpoint}"
                    )
                    continue
                if event_id in fixtures:
                    outcome.skipped["duplicate_event"] += 1
                    continue
                fixture = _accept_event(event, entry, source, raw.fetched_at, outcome)
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
        for display_group in event.get("displayGroups") or []:
            if not isinstance(display_group, dict):
                outcome.skipped["display_group_not_an_object"] += 1
                continue
            for market in display_group.get("markets") or []:
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


def _path_of(endpoint: str) -> str:
    kind, _, path = endpoint.partition(":")
    return path if kind == "coupon" else ""


def _accept_event(
    event: Mapping[str, Any],
    entry: LeaguePath,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> Fixture | None:
    event_id = str(event.get("id"))
    competition = league_registry.league(entry.league)

    if event.get("live"):
        outcome.skipped["event_live"] += 1
        return None
    if str(event.get("type") or "GAMEEVENT") != "GAMEEVENT":
        outcome.skipped[f"event_type:{event.get('type')}"] += 1
        return None

    sides = _competitors(event)
    if sides is None:
        # A futures container names no two competitors, or names more than two.
        outcome.skipped["non_game_event"] += 1
        return None
    home_name, away_name = sides
    if any(is_pairing(part, competition.sport) for part in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    # "Away Total Runs @ Home Total Runs" is a container for an aggregate stat,
    # not a fixture between two competitors — out of scope rather than broken.
    if any(is_statistic(part) for part in (home_name, away_name)):
        outcome.skipped["statistic_not_a_fixture"] += 1
        return None

    home = canonical_participant(home_name, competition)
    away = canonical_participant(away_name, competition)
    if home is None or away is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} ({event.get('description')!r}) in {competition.key}: "
            f"{away_name!r} -> {away.key if away else None}, "
            f"{home_name!r} -> {home.key if home else None}",
            event_id=event_id,
        )
        return None

    commence_time = parse_epoch_time(event.get("startTime"), unit="ms")
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} has unreadable startTime {event.get('startTime')!r}",
            event_id=event_id,
        )
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    # ``competitors[].home`` is a stated fact, so it is handed to orient rather
    # than derived from the description's word order.
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


def _competitors(event: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(home name, away name)`` when exactly one of each is stated."""
    home: list[str] = []
    away: list[str] = []
    for competitor in event.get("competitors") or []:
        if not isinstance(competitor, dict):
            continue
        name = str(competitor.get("name") or "").strip()
        if not name:
            continue
        (home if competitor.get("home") else away).append(name)
    if len(home) != 1 or len(away) != 1:
        return None
    return home[0], away[0]


def _resolve_market(sport: Sport, name: str, period: str) -> MarketRule | str:
    lowered_period = period.lower()
    for fragment, reason in OUT_OF_SCOPE_PERIODS:
        if fragment in lowered_period:
            return reason
    rule = MARKET_RULES.get((sport, name, period))
    if rule is not None:
        return rule
    named = OUT_OF_SCOPE.get((sport, name, period))
    if named is not None:
        return named
    return f"unmapped_market:{sport.value}:{name}"


def _parse_market(
    *,
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    name = str(market.get("description") or "")
    period = str((market.get("period") or {}).get("description") or "")
    resolved = _resolve_market(fixture.sport, name, period)
    if isinstance(resolved, str):
        outcome.skipped[resolved] += 1
        return
    rule = resolved

    market_id = str(market.get("id") or "")
    if not market_id:
        outcome.reject(
            source,
            "missing_market_id",
            f"{name!r} on event {fixture.event_id} has no id, so its rows could not be "
            "grouped into one market",
            event_id=fixture.event_id,
        )
        return

    market_open = str(market.get("status") or _OPEN) == _OPEN
    for entry in market.get("outcomes") or []:
        if not isinstance(entry, dict):
            outcome.skipped["outcome_not_an_object"] += 1
            continue
        quote = _build_quote(
            entry=entry,
            raw=raw,
            source=source,
            fixture=fixture,
            event_key=event_key,
            rule=rule,
            market_id=market_id,
            market_name=name,
            market_open=market_open,
            outcome=outcome,
        )
        if quote is not None:
            outcome.quotes.append(quote)


def _build_quote(
    *,
    entry: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    rule: MarketRule,
    market_id: str,
    market_name: str,
    market_open: bool,
    outcome: ParseOutcome,
) -> Quote | None:
    selection = OUTCOME_TYPES.get(str(entry.get("type") or ""))
    if selection in (Selection.HOME, Selection.AWAY):
        # ``H`` is the competitor *Bovada* flagged ``home: true``, which is our home
        # side only where the league has one — see ``Fixture.our_side``.
        selection = fixture.our_side(selection)
    if selection is None:
        outcome.reject(
            source,
            "unknown_selection",
            f"outcome type {entry.get('type')!r} on {market_name!r} for event "
            f"{fixture.event_id}",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None
    if selection is Selection.DRAW and not draw_is_priced(fixture.sport, rule.period):
        outcome.reject(
            source,
            "draw_not_priced",
            f"a draw on {fixture.sport.value}/{rule.period.value} for event "
            f"{fixture.event_id}, where a level result is not a settlement outcome",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None

    price = entry.get("price") or {}
    decimal_raw = price.get("decimal")
    if decimal_raw is None:
        # A suspended outcome legitimately carries no price.
        outcome.skipped["outcome_without_price"] += 1
        return None
    try:
        decimal_odds = float(decimal_raw)
    except (TypeError, ValueError):
        outcome.reject(
            source,
            "unreadable_price",
            f"{market_name} outcome on event {fixture.event_id} has an unparseable "
            f"decimal {decimal_raw!r}",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None
    if decimal_odds < MIN_DECIMAL_ODDS:
        # "Risk two hundred to win one" is a placeholder, not a quote.
        outcome.skipped["price_below_plausible_minimum"] += 1
        return None
    if not is_plausible_decimal_odds(decimal_odds):
        outcome.reject(
            source,
            "implausible_odds",
            f"{decimal_odds} on {market_name!r} for event {fixture.event_id} — no book "
            "publishes that price",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None

    line: float | None = None
    if rule.market in MARKETS_REQUIRING_LINE:
        handicap = price.get("handicap")
        if handicap is None or str(handicap).strip() == "":
            outcome.reject(
                source,
                "market_without_line",
                f"{market_name} outcome on event {fixture.event_id} carries no handicap",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            return None
        try:
            # Signed from this runner's own perspective already, which is the
            # orientation the schema wants.  ``+ 0.0`` normalizes -0.0.
            line = float(handicap) + 0.0
        except (TypeError, ValueError):
            outcome.reject(
                source,
                "unparseable_line",
                f"{market_name} outcome on event {fixture.event_id} has handicap "
                f"{handicap!r}",
                event_id=fixture.event_id,
                market_id=market_id,
            )
            return None

        # An Asian **split** line arrives as two fields, and reading only the
        # first states a different bet.
        #
        # Bovada writes a quarter line by putting the two halves the stake is
        # divided between in ``handicap`` and ``handicap2``: Everton at
        # ``0.0``/``-0.5`` is -0.25, not a draw-no-bet.  The pair is always
        # exactly half a point apart — 54 of 54 occurrences in the fixtures, 114
        # of 114 live — so the real line is their midpoint.
        #
        # Reading the lower half alone was the single most expensive defect in
        # this pipeline: **40 of 46 reported positions** on a live slate had a
        # Bovada leg, and every one of them was built on a mis-stated line.
        # Re-priced against Pinnacle's correctly-published quarter line, each
        # inverted — ``SOCCER-getafe@SOCCER-alaves`` was reported at +8.68% and
        # is really -4.34%.  The 0.0 case is the worst of them, because a
        # quarter line read as zero is not a near-miss but a different contract:
        # a push where there is none.
        #
        # ``src.arb`` already models quarter lines and their half-pushes; it
        # never fired here because the rows arrived labelled as whole or half
        # lines.  The other two sources that meet split lines refuse them
        # outright (``asian_split_line_splits_the_stake`` at Matchbook,
        # ``asian_quarter_line_splits_the_stake`` at Kambi); stating the line
        # correctly is better, and is what Pinnacle already does.
        second = price.get("handicap2")
        if second is not None and str(second).strip() != "":
            try:
                other = float(second) + 0.0
            except (TypeError, ValueError):
                outcome.reject(
                    source,
                    "unparseable_line",
                    f"{market_name} outcome on event {fixture.event_id} has handicap2 "
                    f"{second!r}",
                    event_id=fixture.event_id,
                    market_id=market_id,
                )
                return None
            if abs(abs(other - line) - 0.5) > 1e-9:
                # Not the split-line shape this is written for.  Refused rather
                # than guessed at: whatever the second number means, betting on
                # the first one alone is what produced the defect above.
                outcome.reject(
                    source,
                    "unrecognised_second_handicap",
                    f"{market_name} outcome on event {fixture.event_id} states "
                    f"handicap {line:g} and handicap2 {other:g}, which are not the "
                    "half-point pair of a split line",
                    event_id=fixture.event_id,
                    market_id=market_id,
                )
                return None
            line = (line + other) / 2.0

    outcome_open = str(entry.get("status") or _OPEN) == _OPEN
    try:
        return priced_quote(
            fixture,
            source=source,
            raw=raw,
            event_key=event_key,
            market=rule.market,
            period=rule.period,
            selection=selection,
            line=line,
            # The default market filter returns one market per kind, so there is
            # no alternate ladder here to distinguish.
            is_alternate=False,
            decimal_odds=decimal_odds,
            american_odds=_american(price, decimal_odds, outcome),
            implied_probability=implied_probability(decimal_odds),
            source_market_id=market_id,
            source_selection_id=str(entry.get("id")) if entry.get("id") is not None else None,
            status=QuoteStatus.ACTIVE if (market_open and outcome_open) else QuoteStatus.SUSPENDED,
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            source,
            "invalid_quote",
            f"{rule.market.value}/{selection.value} on event {fixture.event_id}: {exc}",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None


def _american(price: Mapping[str, Any], decimal_odds: float, outcome: ParseOutcome) -> int:
    """The feed's own American price when it agrees with the decimal one.

    Both formats are published, and the row is required to carry both, so taking
    each from the feed keeps the cross-format check downstream an actual check
    rather than a tautology about our own arithmetic.  A disagreement wider than
    honest rounding means one of the two fields is corrupt, and then the derived
    value is used so the row still carries a price at all.
    """
    derived = decimal_to_american(decimal_odds)
    stated = price.get("american")
    if stated is None:
        return derived
    text = str(stated).strip()
    # "EVEN" is +100 — and is then held to the same agreement check as every
    # other stated price, rather than returned before it.  All 15 occurrences in
    # the capture carry decimal 2.00 and pass, so this changes nothing today; it
    # was the one path that could publish a stated price nothing had compared
    # against the decimal one.
    text = "100" if text.upper() == _EVEN_MONEY else text
    try:
        value = int(text.replace("+", ""))
        payout = american_to_decimal(value) - 1.0
    except (TypeError, ValueError):
        outcome.repaired["feed_american_odds_unreadable"] += 1
        return derived
    if abs(payout - (decimal_odds - 1.0)) / (decimal_odds - 1.0) > 0.01:
        outcome.repaired["feed_american_odds_disagreed_with_decimal"] += 1
        return derived
    return value
