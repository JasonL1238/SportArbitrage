"""Matchbook: the first source here that is an **exchange** rather than a book.

``www.matchbook.com/edge/rest`` is the unauthenticated API the public site reads
from.  One ``events`` call per sport returns the whole slate *with prices*, so
the moneyline, every total and every handicap for a league arrive together —
which makes it the cheapest source in the pipeline per market collected.

    (``apiclient.matchbook.com`` is the documented host and answers 530 from
    here; ``www.matchbook.com`` serves the same paths.  Recorded in
    ``docs/SOURCE_FEASIBILITY.md`` so it is not rediscovered.)

An exchange price is not a sportsbook price, and three differences matter enough
to be structural rather than noted:

**Every price has a side, and only one of them is yours.**  A runner carries both
``back`` and ``lay`` offers.  To *take* a price you take the available **back**
side; mapping a lay price as though it were a back price inflates every margin,
and on a two-way market the lay side looks like a better version of the same bet.
Only back prices become rows.  A lay-only runner — real, and common on a thin
market — is a counted skip, never converted.

**Laying is not backing the other side.**  On a two-way market laying the home
side is economically close to backing the away side, but the stake and the
liability differ: a lay risks liability rather than stake.  Synthesising a
converted row would put a number in the ``decimal_odds`` column that nobody can
place at, so the conversion is simply not done.

**The money behind a price is published, and it binds.**  ``available-amount`` at
the best back price is the largest stake that can actually be matched there.  A
top-of-book price with $40 behind it is not an arbitrage at a $500 stake, so it
goes into :attr:`~src.schema.Quote.limit_amount` and caps the position in
:mod:`src.arb`.  Kalshi, SX Bet and Pinnacle
also state one on every row they publish, so this is a fact four of the ten
venues supply rather than the two this note used to claim.

And the charge: Matchbook takes a percentage of **net winnings**, so the quoted
3.00 pays 2.96.  That lives in :mod:`src.commission`, because the same gap
appears on four of the new sources and the arbitrage arithmetic has to use the
price that is actually paid.

Field traps this parser exists to get right
-------------------------------------------

**The market *name* carries the scoring window, not the type.**  ``market-type``
is ``"total"`` for "Total", "1st Half Total", "Home Team Total Goals" and "Away
Team Total Goals" alike, and ``"handicap"`` for both "Handicap" and "1st Half
Handicap".  Keying on the type alone files a first-half total as a full-game
total — a different contract at a similar-looking number, which is exactly the
shape of a phantom arbitrage.  The table is keyed on ``(Sport, name)`` and
matched exactly, for the same reason Kambi's is.

**Tennis handicaps and totals are counted in games.**  ``market-type: total`` on a
tennis event is total *games*, not sets and not points, and no other source here
quotes it — so tennis collects the match winner alone, as it does everywhere else
in this pipeline.

**Soccer's moneyline is ``one_x_two``, and it is a three-runner market.**  Its
third runner is literally named ``"Draw"``.  Reading it as two-way would drop a
third of the probability and make the remaining two look like free money.

**"open" includes in-play.**  ``states=open`` returns matches already under way,
flagged by ``in-running-flag``.  An in-play price is not the same product as a
pregame one, so those are counted out of scope.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
from src.normalize import decimal_to_american, implied_probability, is_plausible_decimal_odds
from src.participants import (
    with_marker,
    Participant,
    canonical_participant,
    competition_marker,
    is_pairing,
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
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    latest_capture,
    latest_per_endpoint,
    parse_iso_time,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, SourceError, require_mapping

log = logging.getLogger(__name__)

SOURCE_KEY = "matchbook"
DEFAULT_BASE_URL = "https://www.matchbook.com/edge/rest"

#: Prices are asked for in decimal, back-and-lay, in one currency.  Depth 1 is
#: the top of book, which is the only level a position is ever taken at here:
#: a deeper level is a *worse* price, and using it would report an edge the
#: position cannot be filled at.
ODDS_TYPE = "DECIMAL"
EXCHANGE_TYPE = "back-lay"
DEFAULT_CURRENCY = "USD"
PRICE_DEPTH = 1

#: Events per request.  200 is honoured; the busiest sport (soccer) has ~140
#: open events, so one page usually covers a sport and the loop below exists for
#: the day it does not.
DEFAULT_PER_PAGE = 200

#: Stop after this many pages per sport.  A bound rather than a limit anyone
#: expects to hit: what it prevents is a paging bug turning into an unbounded
#: crawl of somebody else's API.
MAX_PAGES_PER_SPORT = 5

_BACK = "back"
_OPEN = "open"

# ── declarative tables ───────────────────────────────────────────────────────

#: Matchbook ``sport-id`` per canonical sport.  From ``/lookups/sports``.
SPORT_IDS: dict[Sport, int] = {
    Sport.BASEBALL: 3,
    Sport.BASKETBALL: 4,
    Sport.HOCKEY: 6,
    Sport.FOOTBALL: 1,
    Sport.SOCCER: 15,
    Sport.TENNIS: 9,
}

#: Navigation ``SPORT`` node name -> canonical sport.  The navigation tree is
#: what turns a competition id on an event into a competition *name*, and the
#: name is the only stable handle: Matchbook mints a fresh competition id per
#: tennis tournament, exactly as Pinnacle does.
SPORT_BY_NAV_NAME: dict[str, Sport] = {
    "Baseball": Sport.BASEBALL,
    "Basketball": Sport.BASKETBALL,
    "Ice Hockey": Sport.HOCKEY,
    "American Football": Sport.FOOTBALL,
    "Soccer": Sport.SOCCER,
    "Tennis": Sport.TENNIS,
}

#: Competition name -> canonical league, per sport, matched **exactly**.
#: Exactly, because Matchbook also lists "Spain La Liga 2" and "Brazil Serie B",
#: and a substring rule would file both as their senior competition.
LEAGUE_BY_COMPETITION: dict[Sport, dict[str, str]] = {
    Sport.BASEBALL: {"Major League Baseball": "MLB"},
    Sport.BASKETBALL: {"WNBA": "WNBA", "NBA": "NBA"},
    Sport.HOCKEY: {"NHL": "NHL"},
    Sport.FOOTBALL: {"NFL": "NFL"},
    Sport.SOCCER: {
        "England Premier League": "EPL",
        "English Premier League": "EPL",
        "US Major League Soccer": "MLS",
        "Spain La Liga": "LA_LIGA",
        "Italy Serie A": "SERIE_A",
        "Germany Bundesliga": "BUNDESLIGA",
        "France Ligue 1": "LIGUE_1",
    },
}

#: Unrecognised competitions land on a catch-all rather than being dropped:
#: league is not part of event identity, so the cost is coverage legibility and
#: the alternative is losing a fixture two other books also price.
CATCH_ALL: dict[Sport, str] = {
    Sport.SOCCER: "SOCCER_OTHER",
    Sport.TENNIS: "TENNIS_OTHER",
    Sport.HOCKEY: "HOCKEY_OTHER",
}

#: Tennis tour, read as a **tour** and a **tier** rather than as one string.
#:
#: Matched anywhere in the name, because that is not where these venues put the
#: word: Matchbook writes ``"ATP Vancouver Challenger"`` and SX Bet writes
#: ``"Vancouver Challenger ATP"``.  A single ordered list of markers cannot
#: express that: testing ``"challenger"`` before ``"wta"`` filed every *women's*
#: Challenger — ``"Vancouver Challenger WTA"``, 252 markets on one capture — as
#: ``ATP_CHALLENGER``, the men's tour, and produced a fresh cross-source
#: disagreement with Matchbook, which calls the same event ``WTA``.
#:
#: So the tour is decided first and the tier only narrows it.  There is no
#: ``WTA_CHALLENGER`` key, so a women's Challenger stays ``WTA``: the same
#: governing body and a lower tier is a far smaller error than the wrong tour,
#: and it agrees with what the other sources call it.
TENNIS_TOURS: tuple[tuple[str, str], ...] = (
    ("itf", "ITF"),
    ("wta", "WTA"),
    ("atp", "ATP"),
)

#: The tier marker, which only refines :data:`TENNIS_TOURS`' ATP answer.
TENNIS_SECOND_TIER = "challenger"


def _tennis_league(label: str) -> str | None:
    """The tour a tennis competition name names, or ``None``."""
    lowered = label.strip().casefold()
    for marker, tour in TENNIS_TOURS:
        if marker in lowered:
            if tour == "ATP" and TENNIS_SECOND_TIER in lowered:
                return "ATP_CHALLENGER"
            return tour
    # A tier marker with no tour named at all is a men's Challenger by
    # convention; the women's tour always names itself.
    return "ATP_CHALLENGER" if TENNIS_SECOND_TIER in lowered else None



@dataclass(frozen=True)
class MarketRule:
    market: Market
    period: Period


#: Exact ``(Sport, market name)`` -> what it settles as.
#:
#: Keyed on the **name** and not on ``market-type``, which is the trap: the type
#: is ``"total"`` for the game total, the first-half total and both team totals,
#: and ``"handicap"`` for the game and first-half handicaps.  A first-half total
#: filed as a full-game total is a different contract at a similar number, and
#: pairing one against another book's game total looks like a large clean edge.
MARKET_RULES: dict[tuple[Sport, str], MarketRule] = {
    (Sport.BASEBALL, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASEBALL, "Handicap"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.BASEBALL, "Total"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.BASKETBALL, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASKETBALL, "Handicap"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.BASKETBALL, "Total"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.HOCKEY, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.HOCKEY, "Handicap"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.HOCKEY, "Total"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.FOOTBALL, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.FOOTBALL, "Handicap"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.FOOTBALL, "Total"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    # Soccer's headline market is three-way and named differently.
    (Sport.SOCCER, "Match Odds"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.SOCCER, "Handicap"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.SOCCER, "Total"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    # Tennis: the match winner and nothing else — see OUT_OF_SCOPE.
    (Sport.TENNIS, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
}

#: Markets deliberately not collected, and why each is a different contract
#: rather than a variant of one that is kept.  Listed by name so the traps stay
#: visible in the skip counters instead of vanishing into a generic bucket.
OUT_OF_SCOPE: dict[tuple[Sport, str], str] = {
    (Sport.TENNIS, "Handicap"): "alternate_scoring_unit_games",
    (Sport.TENNIS, "Total"): "alternate_scoring_unit_games",
    (Sport.TENNIS, "Set Betting"): "correct_score_market",
    (Sport.SOCCER, "Home Team Total Goals"): "team_total_no_other_source_quotes_it",
    (Sport.SOCCER, "Away Team Total Goals"): "team_total_no_other_source_quotes_it",
    (Sport.SOCCER, "Both Teams To Score"): "yes_no_market",
    (Sport.SOCCER, "Correct Score"): "correct_score_market",
    (Sport.SOCCER, "Double Chance"): "combined_outcome_market",
    (Sport.SOCCER, "Draw No Bet"): "draw_voids_market",
    (Sport.SOCCER, "Half Time/Full Time"): "combined_outcome_market",
    (Sport.SOCCER, "Match Result and Both Teams To Score"): "combined_outcome_market",
    (Sport.SOCCER, "Match Result and Total Goals"): "combined_outcome_market",
    (Sport.SOCCER, "Odd or Even Total"): "odd_even_market",
    (Sport.SOCCER, "To Qualify"): "qualification_market",
    (Sport.SOCCER, "Home Team To Win To Nil"): "yes_no_market",
    (Sport.SOCCER, "Away Team To Win To Nil"): "yes_no_market",
}

#: Name fragments identifying an out-of-scope contract when no exact rule
#: matched.  Applied **only after** the exact tables, so a collected name can
#: never be reinterpreted by one of these.
OUT_OF_SCOPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("1st half", "half_only_market"),
    ("2nd half", "half_only_market"),
    ("half time", "half_only_market"),
    ("half-time", "half_only_market"),
    ("1st period", "period_only_market"),
    ("1st quarter", "period_only_market"),
    ("1st set", "period_only_market"),
    ("winner", "futures"),
    ("to be relegated", "futures"),
    ("finish", "futures"),
)

#: ``"OVER 8.5"`` / ``"UNDER 8.5"`` — the runner name on a total.
_TOTAL_RUNNER = re.compile(r"^(?P<side>OVER|UNDER)\s+(?P<line>[-+]?\d+(?:\.\d+)?)$", re.IGNORECASE)

#: ``"Cleveland Guardians -1.5"``.  The sign is required, which is what keeps a
#: club whose name ends in digits ("Schalke 04") from donating its number.
_HANDICAP_RUNNER = re.compile(r"^(?P<name>.+?)\s+(?P<line>[-+]\d+(?:\.\d+)?)$")

#: ``"Malaysia (-3.0/3.5)"`` and ``"OVER (2.0/2.5)"`` — an **Asian split line**,
#: which is a quarter line written as the two whole/half lines it splits between.
#:
#: A different contract, not a variant of the one above: the stake is divided
#: across two neighbouring lines, so the market half-pushes on the whole number
#: and there is no single ``line`` on a row that can represent it.  Counted out
#: of scope rather than rejected — the market is real and Matchbook is right to
#: offer it, this pipeline simply does not model it, exactly as it does not model
#: Kambi's ``Asian Handicap``.
#:
#: Left as a rejection it produced 666 of them on one slate, which would have
#: marked the source unhealthy every run for correctly declining to guess.
_SPLIT_LINE_RUNNER = re.compile(
    r"\(\s*[-+]?\d+(?:\.\d+)?\s*/\s*[-+]?\d+(?:\.\d+)?\s*\)\s*$"
)

#: The draw runner on ``one_x_two``.
_DRAW_RUNNER = "draw"

#: ``" at "`` on the US sports ("Away at Home"), ``" vs "`` on soccer and tennis
#: ("Home vs Away").  Both verified against another book's own home flag: Bovada
#: publishes ``competitors[].home`` and agrees with this reading on every shared
#: fixture.  Validation's ``home_away_disagreement`` is the standing check.
_SEPARATORS: tuple[tuple[str, bool], ...] = (
    (" at ", False),  # away first
    (" vs ", True),  # home first
)

#: ``"(G1)"`` on the second game of a doubleheader.  Kept out of the name before
#: resolution for the open-roster sports, where a parenthetical is *not* dropped
#: (a soccer "(W)" is the only thing separating a women's side from the men's).
_GAME_MARKER = re.compile(r"\s*\((?:G|Game\s*)\d+\)\s*$", re.IGNORECASE)

DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB",
    "WNBA",
    "NBA",
    "NHL",
    "HOCKEY_OTHER",
    "NFL",
    "ATP",
    "WTA",
    "ATP_CHALLENGER",
    "ITF",
    "TENNIS_OTHER",
    "EPL",
    "MLS",
    "LA_LIGA",
    "SERIE_A",
    "BUNDESLIGA",
    "LIGUE_1",
    "SOCCER_OTHER",
)

#: Markets claimed per sport, for coverage reporting.
MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    sport: frozenset(
        rule.market
        for (mapped, _), rule in MARKET_RULES.items()
        if mapped is sport and rule.period is Period.FULL_GAME
    )
    for sport in Sport
}

_NAVIGATION_ENDPOINT = "navigation"


def events_endpoint(sport: Sport, page: int) -> str:
    """Stable label for one page of one sport's slate."""
    return f"events:{sport.value}:{page:02d}"


# ── the adapter ──────────────────────────────────────────────────────────────


class MatchbookAdapter:
    """Collects Matchbook's pregame exchange prices for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        currency: str = DEFAULT_CURRENCY,
        per_page: int = DEFAULT_PER_PAGE,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        keys: list[str] = []
        for key in leagues:
            league_registry.league(key)  # fail at construction, not mid-run
            if key not in keys:
                keys.append(key)
        if not keys:
            raise ValueError("MatchbookAdapter needs at least one league to collect")
        self._leagues: tuple[str, ...] = tuple(keys)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.currency = currency
        self.per_page = per_page
        self._http = SourceClient(source_key, timeout=timeout, client=client)

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    @property
    def sports(self) -> tuple[Sport, ...]:
        wanted = {league_registry.league(key).sport for key in self._leagues}
        return tuple(sport for sport in Sport if sport in wanted and sport in SPORT_IDS)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Moneyline, handicap and total everywhere except tennis.

        Tennis claims the match winner alone: Matchbook's tennis handicap and
        total are counted in *games*, which is a different scoring unit from
        anything another source here quotes.

        *tier* changes nothing — one ``events`` call per sport carries the whole
        market set, which is what makes this the cheapest source per market.
        """
        del tier
        return capabilities_from(
            {
                key: MARKETS_BY_SPORT[league_registry.league(key).sport]
                for key in self._leagues
            },
            self._leagues,
        )

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """The navigation tree once, then each configured sport's slate.

        The navigation call is what turns a competition id on an event into a
        competition *name*, and the name is the only stable handle Matchbook
        offers: it mints a fresh competition id per tennis tournament.  It is
        captured like any other response, so ``parse`` stays a pure function of
        stored bytes rather than needing a lookup performed at fetch time.
        """
        del tier  # no per-event follow-up exists for this source
        raws: list[RawResponse] = [self._get_navigation()]
        tally = self.last_fetch = ScopeTally(self._source_key)
        for sport in self.sports:
            tally.requested(sport.value)
            # ``into=raws`` rather than a returned list: a scope that fails
            # part-way through keeps the pages it already fetched.  They were
            # paid for out of somebody else's bandwidth, they are valid, and
            # discarding them turned "page 2 of 3 timed out" into "this whole
            # sport returned nothing" — 4 of 6 requests thrown away on a run
            # that reported OK.
            before = len(raws)
            try:
                self._fetch_sport(sport, into=raws)
            except SourceError as exc:
                log.warning("%s: %s failed: %s", self._source_key, sport.value, exc)
                tally.failed(sport.value, exc)
                # Counted as failed, not as produced.  The pages it managed to
                # fetch are kept — that is the point of ``into=`` — but a scope
                # that raised has not produced a slate, and letting its partial
                # pages mark it "produced" would satisfy ``require_something``
                # and turn a source that lost every sport into a healthy one.
                continue
            pages = raws[before:]
            tally.produced(sport.value, sum(_event_count(page) for page in pages))
        tally.require_something(what="open event")
        return raws

    def _get_navigation(self) -> RawResponse:
        return self._http.get(f"{self.base_url}/navigation", endpoint=_NAVIGATION_ENDPOINT)

    def _fetch_sport(
        self, sport: Sport, *, into: list[RawResponse] | None = None
    ) -> list[RawResponse]:
        """Every page of this scope, appended to *into* as they arrive.

        Appending as it goes rather than returning at the end is what lets a
        failure part-way through keep the pages already fetched; see the caller.
        """
        pages: list[RawResponse] = [] if into is None else into
        offset = 0
        for page in range(1, MAX_PAGES_PER_SPORT + 1):
            raw = self._http.get(
                f"{self.base_url}/events",
                endpoint=events_endpoint(sport, page),
                params={
                    "sport-ids": str(SPORT_IDS[sport]),
                    "states": "open",
                    "per-page": str(self.per_page),
                    "offset": str(offset),
                    "odds-type": ODDS_TYPE,
                    "exchange-type": EXCHANGE_TYPE,
                    "include-prices": "true",
                    "price-depth": str(PRICE_DEPTH),
                    "currency": self.currency,
                },
            )
            payload = require_mapping(
                raw.json(), source=self._source_key, endpoint=raw.endpoint
            )
            events = payload.get("events") or []
            # Kept even when it holds nothing: an empty first page is what a
            # renamed field looks like, and the EmptyResponseError that follows
            # carries no payload of its own, so the failure would otherwise
            # arrive with nothing to read.
            pages.append(raw)
            if not events:
                # An empty first page is a real off day for the sport; an empty
                # later page is the end of the list.  Neither is a failure.
                break
            offset += len(events)
            total = payload.get("total")
            if not isinstance(total, int) or offset >= total:
                break
            if page == MAX_PAGES_PER_SPORT:
                log.warning(
                    "%s: stopped at %d pages for %s with %d of %d events collected",
                    self._source_key, MAX_PAGES_PER_SPORT, sport.value, offset, total,
                )
        return pages

    # ── parse ────────────────────────────────────────────────────────────────

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_matchbook(raws)

    def close(self) -> None:
        self._http.close()


def _event_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    return len(payload.get("events") or []) if isinstance(payload, dict) else 0


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
    marker: str | None = None
    """The competition's identity marker, carried so the **priced side** is
    resolved the same way the fixture was.

    Applied to ``home``/``away`` when the fixture was built and nowhere else, it
    broke exactly the case it exists for: where a venue marks the competition
    and not the teams, the fixture keys became ``...w`` while every
    competitor-named price still resolved to the unmarked slug, matched neither
    side, and was **rejected** — one such fixture marks the whole source
    unhealthy."""


def parse_matchbook(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Matchbook responses into normalized rows.

    Pure: no network, no clock, no filesystem.  Everything time-related comes
    from ``raw.fetched_at``, and the ``source`` each row carries comes from the
    envelope.
    """
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    selected = latest_per_endpoint(latest_capture(raws))

    navigation = next(
        (raw for raw in selected if raw.endpoint == _NAVIGATION_ENDPOINT), None
    )
    if navigation is None:
        raise FormatChangeError(
            f"{source}: no navigation response, so no competition id can be named — "
            "every fixture's league would have to be guessed"
        )
    competitions = _competition_index(navigation, source)
    outcome.skipped["navigation_response"] += 1

    fixtures: dict[str, _Fixture] = {}
    priced: list[tuple[RawResponse, Mapping[str, Any], _Fixture]] = []
    for raw in selected:
        if raw.endpoint == _NAVIGATION_ENDPOINT:
            continue
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: expected a JSON object in an events response"
            )
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                outcome.skipped["event_not_an_object"] += 1
                continue
            event_id = str(event.get("id") or "")
            if not event_id:
                outcome.reject(source, "missing_event_id", f"event without an id in {raw.endpoint}")
                continue
            if event_id in fixtures:
                outcome.skipped["duplicate_event"] += 1
                continue
            fixture = _accept_event(event, competitions, source, raw.fetched_at, outcome)
            if fixture is None:
                continue
            fixtures[event_id] = fixture
            priced.append((raw, event, fixture))

    if not fixtures:
        return outcome

    event_keys = resolve_doubleheaders(
        {event_id: (f.base_key, f.commence_time) for event_id, f in fixtures.items()}
    )

    for raw, event, fixture in priced:
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


def _competition_index(
    navigation: RawResponse, source: str
) -> dict[str, tuple[Sport, str]]:
    """``competition id -> (sport, competition name)`` from the navigation tree.

    Matchbook mints a fresh competition id per tennis tournament, exactly as
    Pinnacle does, so ids cannot be enumerated ahead of time and the *name* is
    the only stable handle.  An event carries only ids, which is why this tree is
    fetched and captured rather than a static table being kept here.
    """
    payload = navigation.json()
    if not isinstance(payload, list):
        raise FormatChangeError(
            f"{source}:{navigation.endpoint}: expected a JSON array of navigation trees"
        )
    index: dict[str, tuple[Sport, str]] = {}

    def walk(nodes: Iterable[Any], sport: Sport | None) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = str(node.get("type") or "")
            name = str(node.get("name") or "")
            here = sport
            if kind == "SPORT":
                here = SPORT_BY_NAV_NAME.get(name)
            elif kind == "COMPETITION" and here is not None and node.get("id") is not None:
                index.setdefault(str(node["id"]), (here, name))
            walk(node.get("meta-tags") or [], here)

    walk(payload, None)
    if not index:
        raise FormatChangeError(
            f"{source}:{navigation.endpoint}: navigation tree named no competitions"
        )
    return index


def _classify(
    event: Mapping[str, Any], competitions: Mapping[str, tuple[Sport, str]]
) -> tuple[Sport, str, str] | None:
    """``(sport, league key, competition name)`` for one event, or ``None``."""
    for category in event.get("category-id") or []:
        found = competitions.get(str(category))
        if found is None:
            continue
        sport, name = found
        league = _league_for(sport, name)
        if league is not None:
            return sport, league, name
    return None


def _league_for(sport: Sport, competition_name: str) -> str | None:
    named = LEAGUE_BY_COMPETITION.get(sport, {}).get(competition_name.strip())
    if named is not None:
        return named
    if sport is Sport.TENNIS:
        found = _tennis_league(competition_name)
        if found is not None:
            return found
    return CATCH_ALL.get(sport)


def _split_name(name: str) -> tuple[str, str, bool] | None:
    """``(first, second, home_first)`` for a two-sided event name."""
    for separator, home_first in _SEPARATORS:
        if name.count(separator) == 1:
            first, second = name.split(separator, 1)
            return first.strip(), second.strip(), home_first
    return None


def _accept_event(
    event: Mapping[str, Any],
    competitions: Mapping[str, tuple[Sport, str]],
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> _Fixture | None:
    event_id = str(event.get("id"))
    name = str(event.get("name") or "")

    if event.get("in-running-flag"):
        # "open" includes matches already under way.  An in-play price moves on
        # the run of play and is not the same product as a pregame one.
        outcome.skipped["event_in_running"] += 1
        return None
    if str(event.get("status") or "") != _OPEN:
        outcome.skipped[f"event_status:{event.get('status')}"] += 1
        return None

    classified = _classify(event, competitions)
    if classified is None:
        outcome.skipped["competition_unmapped"] += 1
        return None
    sport, league_key, competition_name = classified
    if not league_registry.is_known(league_key):
        outcome.skipped[f"league_not_registered:{league_key}"] += 1
        return None
    competition = league_registry.league(league_key)

    split = _split_name(name)
    if split is None:
        # An outright container: "Super Bowl LXI Winner" names no two sides.
        outcome.skipped["non_game_event"] += 1
        return None
    first_raw, second_raw, home_first = split

    if any(is_pairing(part, competition.sport) for part in (first_raw, second_raw)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    resolvable = [_strip_game_marker(part, competition) for part in (first_raw, second_raw)]
    # A women's, reserve or youth competition whose *name* carries the marker
    # while the team names do not.  ``_classify`` has always returned the
    # competition name and it was discarded here.
    marker = competition_marker(competition_name, competition.sport)
    resolvable = [with_marker(part, marker) for part in resolvable]
    first = canonical_participant(resolvable[0], competition)
    second = canonical_participant(resolvable[1], competition)
    if first is None or second is None or first.key == second.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} ({name!r}) in {competition.key}: "
            f"{resolvable[0]!r} -> {first.key if first else None}, "
            f"{resolvable[1]!r} -> {second.key if second else None}",
            event_id=event_id,
        )
        return None

    commence_time = parse_iso_time(event.get("start"))
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} ({name!r}) has unparseable start {event.get('start')!r}",
            event_id=event_id,
        )
        return None
    if commence_time <= captured_at:
        # Pregame only, judged against the moment the bytes were captured so the
        # decision replays identically rather than depending on a clock.
        outcome.skipped["event_already_started"] += 1
        return None

    book_home = (first if home_first else second) if competition.has_home_away else None
    away, home = orient(first, second, competition, home=book_home)
    return _Fixture(
        event_id=event_id,
        sport=sport,
        competition=competition,
        home=home,
        away=away,
        commence_time=commence_time,
        base_key=build_event_key(away.key, home.key, commence_time, competition),
        marker=marker,
    )


def _strip_game_marker(name: str, competition: League) -> str:
    """Remove a ``(G1)`` doubleheader marker before resolution.

    Only for open-roster competitions, where parentheticals are deliberately
    *kept*: a soccer "(W)" is the only thing separating a women's side from the
    men's, so the resolver does not drop them and this marker would otherwise
    become part of the club's identity.  A closed roster drops parentheticals
    itself, so nothing needs doing there.
    """
    if competition.roster is not None:
        return name
    return _GAME_MARKER.sub("", name).strip()


def _resolve_market(sport: Sport, name: str) -> MarketRule | str:
    """The rule for a market name, or the reason it is out of scope."""
    if not name:
        return "market_without_name"
    rule = MARKET_RULES.get((sport, name))
    if rule is not None:
        return rule
    named = OUT_OF_SCOPE.get((sport, name))
    if named is not None:
        return named
    lowered = name.lower()
    for fragment, reason in OUT_OF_SCOPE_MARKERS:
        if fragment in lowered:
            return reason
    return f"unmapped_market:{sport.value}"


def _parse_market(
    *,
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    name = str(market.get("name") or "")
    resolved = _resolve_market(fixture.sport, name)
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

    market_open = str(market.get("status") or "") == _OPEN and not market.get("in-running-flag")
    for runner in market.get("runners") or []:
        if not isinstance(runner, dict):
            outcome.skipped["runner_not_an_object"] += 1
            continue
        quote = _build_quote(
            runner=runner,
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


def _best_back(
    runner: Mapping[str, Any],
    *,
    currency: str = DEFAULT_CURRENCY,
    outcome_counts: Counter[str] | None = None,
) -> tuple[float, float | None] | None:
    """``(decimal odds, available amount)`` at the best takeable back price.

    Only ``back``.  A lay price is the other half of the book and taking it is a
    different bet with a different risk — laying stakes your liability, not your
    stake — so mapping one into ``decimal_odds`` would put a number in the column
    that nobody can place at.  ``None`` means this runner has no back offer,
    which is ordinary on a thin market and is counted rather than converted.
    """
    outcome_counts = Counter() if outcome_counts is None else outcome_counts
    unreadable: list[str] = []
    foreign: list[str] = []
    best: tuple[float, float | None] | None = None
    for price in runner.get("prices") or []:
        if not isinstance(price, dict) or str(price.get("side") or "") != _BACK:
            continue
        # ``decimal-odds``, not ``odds``.
        #
        # ``odds`` means whatever the sibling ``odds-type`` says, and that field
        # was never read.  Both are on all 17,057 captured prices and agree
        # exactly today, because ``odds-type`` is ``DECIMAL`` on every one of
        # them — but a request that came back in another format would be read as
        # decimal in silence, and the plausibility band cannot catch it.  The
        # dangerous direction is AMERICAN: a true decimal 4.00 arrives as ``+300``
        # and is recorded as decimal **300.0**, an implied probability of 0.33%,
        # which is instant phantom arbitrage on every underdog.  3,552 of the
        # 5,480 positive-American values in the capture would land inside the
        # band.
        #
        # ``decimal-odds`` is unconditional and needs no interpretation, so
        # reading it removes the dependency rather than checking it.
        raw_odds = price.get("decimal-odds")
        if raw_odds is None:
            # Falling back to ``odds`` is only safe when the venue says it is
            # decimal.  Ungated, the fallback reinstated the exact hazard the
            # comment above describes: an AMERICAN ``+300`` reads as decimal
            # 300.0, which passes the plausibility band at an implied 0.33%.
            # ``odds-type`` is on all 16,780 captured prices.
            if str(price.get("odds-type") or "") != ODDS_TYPE:
                unreadable.append(str(price.get("odds-type") or "missing"))
                continue
            raw_odds = price.get("odds")
        try:
            odds = float(raw_odds)
        except (TypeError, ValueError):
            continue
        # ``available-amount`` becomes ``limit_amount``, which ``src.arb`` takes a
        # ``min()`` of across venues as one bankroll cap — so it has to be in the
        # currency every other venue states.  The request asks for one and the
        # response states which it got; a size in an unfamiliar currency is
        # dropped rather than compared against dollars.
        #
        # Compared against the currency this instance *asked* for, not a module
        # constant: an adapter built with ``currency="GBP"`` would otherwise
        # discard every limit it collected.  And counted, because dropping a size
        # silently removes the cap that would have bound — ``src.arb`` reads a
        # missing one as "this leg states none" and sizes off the other legs.
        # A price whose size cannot be compared is not emitted at all.
        #
        # Dropping the size to ``None`` looked conservative and is not: ``None``
        # means *unknown*, and ``src.arb`` reports a cap only when some leg
        # states one — so on a two-leg position with one exchange leg, dropping
        # it removes the cap **and** the note explaining it, and a position
        # backed by a $40 order book is presented as unbounded.  Refusing the
        # price removes something that cannot be sized instead of a size that
        # would have bound.
        amount = price.get("available-amount")
        stated = str(price.get("currency") or currency)
        if stated != currency:
            foreign.append(stated)
            continue
        try:
            size = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            size = None
        if best is None or odds > best[0]:
            best = (odds, size)
    # Reported once per runner rather than once per price, and as a bounded key:
    # the venue's own strings must not become an unbounded namespace in the
    # ``skipped`` table.
    if unreadable:
        outcome_counts["price_in_an_unreadable_odds_type"] += len(unreadable)
    if foreign:
        outcome_counts["price_in_another_currency"] += len(foreign)
    return best


def _runner_selection(
    runner_name: str, rule: MarketRule, fixture: _Fixture
) -> tuple[Selection, float | None] | str:
    """``(selection, line)`` for a runner, or a reason it could not be read."""
    text = runner_name.strip()
    if _SPLIT_LINE_RUNNER.search(text):
        return "!asian_split_line_splits_the_stake"
    if rule.market is Market.TOTAL:
        match = _TOTAL_RUNNER.match(text)
        if match is None:
            return "unparseable_total_runner"
        side = Selection.OVER if match.group("side").lower() == "over" else Selection.UNDER
        return side, float(match.group("line"))

    line: float | None = None
    if rule.market in MARKETS_REQUIRING_LINE:
        match = _HANDICAP_RUNNER.match(text)
        if match is None:
            return "unparseable_handicap_runner"
        text, line = match.group("name").strip(), float(match.group("line"))

    if text.lower() == _DRAW_RUNNER:
        return Selection.DRAW, line

    # Through the fixture's own competition marker, for the same reason Kambi
    # does: the marker was applied when the fixture was built and not here, so a
    # reserve or women's competition rejected every priced runner.
    who = canonical_participant(
        with_marker(_strip_game_marker(text, fixture.competition), fixture.marker),
        fixture.competition,
    )
    if who is None:
        return "unresolved_runner_participant"
    if who.key == fixture.home.key:
        return Selection.HOME, line
    if who.key == fixture.away.key:
        return Selection.AWAY, line
    return "runner_not_a_participant_of_the_event"


def _build_quote(
    *,
    runner: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: _Fixture,
    event_key: str,
    rule: MarketRule,
    market_id: str,
    market_name: str,
    market_open: bool,
    outcome: ParseOutcome,
) -> Quote | None:
    runner_name = str(runner.get("name") or "")
    resolved = _runner_selection(runner_name, rule, fixture)
    if isinstance(resolved, str):
        # A leading "!" marks a deliberate omission rather than a failure: the
        # market is real and this pipeline does not model it.  The two are graded
        # differently on purpose — a rejection means the source offered something
        # in scope that could not be represented, and marks the source unhealthy.
        if resolved.startswith("!"):
            outcome.skipped[resolved[1:]] += 1
            return None
        outcome.reject(
            source,
            resolved,
            f"{market_name} runner {runner_name!r} on event {fixture.event_id} "
            f"({fixture.away.name} at {fixture.home.name})",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None
    selection, line = resolved

    if selection is Selection.DRAW and not draw_is_priced(fixture.sport, rule.period):
        outcome.reject(
            source,
            "draw_not_priced",
            f"a draw runner on {fixture.sport.value}/{rule.period.value} for event "
            f"{fixture.event_id}, where a level result is not a settlement outcome",
            event_id=fixture.event_id,
            market_id=market_id,
        )
        return None

    # The currency this response was *asked* for, off the envelope rather than
    # off the adapter, so ``parse`` stays a pure function of the bytes and an
    # instance built for another currency is read correctly on replay.
    requested = str((raw.request_params or {}).get("currency") or DEFAULT_CURRENCY)
    best = _best_back(runner, currency=requested, outcome_counts=outcome.skipped)
    if best is None:
        # No back offer at all: the runner exists and nobody is laying it.  Real
        # and common on a thin exchange market.
        outcome.skipped["runner_without_a_back_price"] += 1
        return None
    decimal_odds, available = best

    # An extreme is not corruption on an order-driven venue.  A resting
    # offer at 99.99% or at a tenth of a percent is a real order somebody
    # placed, unlike a sportsbook's posted price where a number outside the
    # band is a units error.  Counted out of scope rather than rejected: a
    # rejection marks the whole source unhealthy for a market working
    # normally, which is what took Smarkets down on its first live run.
    if not is_plausible_decimal_odds(decimal_odds):
        outcome.skipped["price_outside_the_plausible_band"] += 1
        return None

    # A stated size of zero is "nothing is available here", which is not the same
    # fact as "no size is stated" and must not be recorded as it: the schema
    # treats None as unknown.
    #
    # So the price is dropped rather than published with an unknown size.  It
    # used to be published — the comment above already said this was wrong and
    # the line below it did it anyway, because :class:`src.schema.Quote` refuses
    # a non-positive ``limit_amount`` and ``None`` was the only value left.  The
    # effect was a leg that can match nothing reading as a leg of unknown size,
    # which is what ``max_total_stake`` treats as "no constraint from here": a
    # position resting on it was reported uncapped.  The sibling case a few
    # lines up — a size stated in a currency that cannot be compared — is
    # handled the same way, and for the same stated reason: "dropping the size
    # to None looked conservative and is not."
    if available is not None and available <= 0:
        outcome.skipped["available_amount_not_positive"] += 1
        return None
    limit_amount = available

    runner_open = str(runner.get("status") or _OPEN) == _OPEN
    try:
        return Quote(
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
            # ``+ 0.0`` normalizes -0.0 so a pick'em handicap and its mirror
            # format identically in ``dedup_key``.
            line=None if line is None else line + 0.0,
            # Matchbook publishes several totals and handicaps per fixture with
            # no "main line" flag, and ``dedup_key`` already separates them by
            # line, so none is marked alternate: inventing a primary would be a
            # guess, and the flag exists to separate two rows at the *same*
            # number, which cannot happen here.
            is_alternate=False,
            decimal_odds=decimal_odds,
            american_odds=decimal_to_american(decimal_odds),
            implied_probability=implied_probability(decimal_odds),
            source_market_id=market_id,
            source_selection_id=str(runner.get("id")) if runner.get("id") is not None else None,
            limit_amount=limit_amount,
            status=QuoteStatus.ACTIVE if (market_open and runner_open) else QuoteStatus.SUSPENDED,
            last_change_at=parse_iso_time(runner.get("last-price-update-time")),
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
