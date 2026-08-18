"""FanDuel pregame game markets for baseball, basketball, hockey, football,
tennis and soccer, from the public content pages.

``sbapi.{state}.sportsbook.fanduel.com`` serves the same JSON the public
sportsbook page renders from.  No authentication is involved: ``_ak`` is a
static application key baked into the public page, not a user credential, and no
account, session, or geolocation workaround is used.

Three request shapes cover everything collected here:

* ``content-managed-page?page=CUSTOM&customPageId={mlb|wnba|nba|nhl|nfl}`` — one
  US league per page.  All four sports expose the *same three* market types
  (``MONEY_LINE``, ``MATCH_HANDICAP_(2-WAY)``, ``TOTAL_POINTS_(OVER/UNDER)``),
  which is why :data:`US_GAME_MARKETS` is one table shared by four pages rather
  than four copies of it.
* ``content-managed-page?page=SPORT&eventTypeId={1|2}`` — the whole soccer or
  tennis slate in one response, spanning many competitions.  Soccer's headline
  market is ``WIN-DRAW-WIN`` (the 90-minute three-way moneyline); tennis's is
  ``MATCH_BETTING`` (match winner).
* ``event-page?eventId=…&tab=popular`` — per-fixture detail.  This is the only
  place FanDuel exposes soccer full-game totals and handicaps, and it does so as
  **fixed-line market types** (``OVER_UNDER_25`` = Over/Under 2.5 goals,
  ``HOME_TEAM_-1.5_GOALS``) rather than as a line on the runner.

Every page mixes real fixtures with futures containers — events named "NFL
Futures", "NHL Specials", "NBA Player Awards", "English Premier League",
"Men's US Open 2026".  Those are counted out of scope rather than coerced into
the game schema, which is what once produced market types like
``american_league_cy_young_2026`` and participants like ``MLB Futures``.

Two failure modes this module is deliberately structured against:

**Silent drops.**  There is no bare ``continue``.  Every input record that does
not become a row lands either in ``outcome.skipped`` (deliberately out of scope,
counted by reason) or in ``outcome.reject`` (in scope but unrepresentable, with a
stable machine-readable reason).  The previous version dropped every market
belonging to a futures container with no counter at all — 70 of the MLB page's
118 markets — so "we collected everything" was an assumption rather than a
measurement.

**Duplicated replays.**  :func:`parse_fanduel` selects the *latest* response per
endpoint instead of parsing everything handed to it.  Replaying a directory that
holds two runs previously emitted every row twice, and because ``dedup_key`` is
enforced by a UNIQUE constraint, that aborted the insert of the entire run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

import httpx

from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League, is_known
from src.leagues import league as get_league
from src.normalize import MIN_DECIMAL_ODDS, decimal_to_american, implied_probability
from src.participants import (
    with_marker,
    Participant,
    canonical_participant,
    competition_marker,
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
)
from src.sources._common import (
    ScopeTally,
    SourceClient,
    TENNIS_TOURS_BY_GENDER,
    Tier,
    capabilities_from,
    envelope_source,
    latest_per_endpoint,
    parse_iso_time,
    tennis_tour,
)
from src.sources.base import ParseOutcome
from src.sources.guards import (
    CoverageCappedError,
    FormatChangeError,
    SourceError,
    require_keys,
    require_mapping,
    require_nonempty,
)

log = logging.getLogger(__name__)

SOURCE_KEY = "fanduel"
DEFAULT_STATE = "il"

#: Static application key present in the public page's own JavaScript.  Not a
#: credential: it identifies the web client, is identical for every visitor, and
#: no account is involved.
DEFAULT_APP_KEY = "FhMFpcPWXMeyZxOx"

CONTENT_PAGE_PATH = "content-managed-page"
EVENT_PAGE_PATH = "event-page"

#: The ``tab`` that carries soccer's full-game totals and handicaps.  Without it
#: the event page returns whichever tab FanDuel currently defaults to, which was
#: observed to be the first-half card — real markets, but the wrong period.
POPULAR_TAB = "popular"


# ── how a runner's selection and line are stated ─────────────────────────────


class LineFrom(StrEnum):
    """Where a market's line lives in the payload.

    FanDuel states it in two incompatible ways.  US markets put it on the runner
    as ``handicap``, already signed from that runner's own perspective.  Soccer
    totals and handicaps put it in the *market type name* and repeat it in the
    runner name (``"Chelsea -1.5 Goals"``), leaving ``handicap`` at ``0`` — so
    reading ``handicap`` there would silently produce a spread at line 0.
    """

    NONE = "none"
    HANDICAP = "handicap"
    RUNNER_NAME = "runner_name"


class SelectionFrom(StrEnum):
    """How a runner's canonical selection is determined.

    ``RESULT_TYPE`` is the easy case: ``result.type`` is ``HOME``/``AWAY``/
    ``DRAW``/``OVER``/``UNDER``.  Soccer's fixed-line markets leave ``result``
    empty, and tennis's ``result.type`` is a *positional* label with no meaning —
    there is no home player, and each book orders the two names as it likes — so
    both resolve the runner's own name to a participant instead and derive the
    side from identity.
    """

    RESULT_TYPE = "result_type"
    OVER_UNDER_NAME = "over_under_name"
    PARTICIPANT = "participant"


@dataclass(frozen=True)
class MarketSpec:
    """What one FanDuel ``marketType`` means in canonical terms."""

    market: Market
    period: Period
    line_from: LineFrom = LineFrom.NONE
    selection_from: SelectionFrom = SelectionFrom.RESULT_TYPE
    declared_line: float | None = None
    """Absolute line named by the market type itself (``OVER_UNDER_25`` → 2.5).

    Cross-checked against the line parsed from the runner, so a renamed market
    type or a reformatted runner name is a rejection rather than a price filed
    at the wrong number."""


#: The three market types every US league page exposes, identically.  Sharing one
#: table across baseball, basketball, hockey and football is not a shortcut — the
#: strings really are the same, and four copies would let them drift apart.
#: ``TOTAL_POINTS_(OVER/UNDER)`` counts runs in baseball and goals in hockey;
#: ``Market.TOTAL`` plus the row's ``sport`` already carries that distinction.
US_GAME_MARKETS: Mapping[str, MarketSpec] = {
    "MONEY_LINE": MarketSpec(Market.MONEYLINE, Period.FULL_GAME),
    "MATCH_HANDICAP_(2-WAY)": MarketSpec(
        Market.SPREAD, Period.FULL_GAME, LineFrom.HANDICAP
    ),
    "TOTAL_POINTS_(OVER/UNDER)": MarketSpec(
        Market.TOTAL, Period.FULL_GAME, LineFrom.HANDICAP
    ),
}

#: Tennis: match winner only.  Set handicaps, total games and set betting are
#: real markets but a different scope, counted out of scope rather than
#: half-supported.
TENNIS_MARKETS: Mapping[str, MarketSpec] = {
    "MATCH_BETTING": MarketSpec(
        Market.MONEYLINE, Period.FULL_GAME, selection_from=SelectionFrom.PARTICIPANT
    ),
}

#: Soccer, from the sport page.  ``WIN-DRAW-WIN`` settles on 90 minutes plus
#: stoppage, which is what :attr:`Period.FULL_GAME` means for soccer.  Cup
#: extra-time and "to qualify" products are a different contract and are counted
#: out of scope, never mapped here.
SOCCER_SPORT_PAGE_MARKETS: Mapping[str, MarketSpec] = {
    "WIN-DRAW-WIN": MarketSpec(Market.MONEYLINE, Period.FULL_GAME),
}

#: Soccer, from the per-event page.  Empty because every in-scope market there is
#: a *fixed-line* type resolved by pattern — see :func:`_soccer_fixed_line_spec`.
#: ``WIN-DRAW-WIN`` also appears on this page and is deliberately not collected
#: from it: the sport page already supplied it, and emitting it twice would
#: collide on ``dedup_key`` and abort the whole run's insert.
SOCCER_EVENT_PAGE_MARKETS: Mapping[str, MarketSpec] = {}

#: Market types a page must not collect because another page already did.
SOCCER_EVENT_PAGE_DUPLICATES: Mapping[str, str] = {
    "WIN-DRAW-WIN": "already_collected_from_sport_page",
}

#: ``OVER_UNDER_25`` → Over/Under 2.5 goals.  The digits carry one implied
#: decimal place, so ``05`` is 0.5 and ``105`` would be 10.5.
_SOCCER_TOTAL_TYPE = re.compile(r"^OVER_UNDER_(?P<digits>\d{2,3})$")

#: ``HOME_TEAM_-1.5_GOALS`` — the home side at -1.5.  The mirrored market
#: ``AWAY_TEAM_-1.5_GOALS`` is a *different* market at the opposite line, not the
#: other half of this one.
_SOCCER_HANDICAP_TYPE = re.compile(
    r"^(?:HOME|AWAY)_TEAM_(?P<line>[-+]?\d+(?:\.\d+)?)_GOALS$"
)

#: ``"Over 2.5 Goals"`` / ``"Under 2.5 Goals"``.
_TOTAL_RUNNER = re.compile(
    r"^(?P<side>Over|Under)\s+(?P<line>\d+(?:\.\d+)?)\s+Goals?$", re.IGNORECASE
)

#: ``"Chelsea -1.5 Goals"``.  The sign is required, which is what keeps a club
#: whose name ends in digits ("Schalke 04") from donating its number to the line.
_HANDICAP_RUNNER = re.compile(
    r"^(?P<name>.+?)\s+(?P<line>[-+]\d+(?:\.\d+)?)\s+Goals?$", re.IGNORECASE
)


def _soccer_fixed_line_spec(market_type: str) -> MarketSpec | None:
    """Resolve a soccer fixed-line market type, else ``None``."""
    total = _SOCCER_TOTAL_TYPE.match(market_type)
    if total is not None:
        return MarketSpec(
            Market.TOTAL,
            Period.FULL_GAME,
            LineFrom.RUNNER_NAME,
            SelectionFrom.OVER_UNDER_NAME,
            declared_line=int(total.group("digits")) / 10,
        )
    handicap = _SOCCER_HANDICAP_TYPE.match(market_type)
    if handicap is not None:
        return MarketSpec(
            Market.SPREAD,
            Period.FULL_GAME,
            LineFrom.RUNNER_NAME,
            SelectionFrom.PARTICIPANT,
            declared_line=abs(float(handicap.group("line"))),
        )
    return None


#: ``result.type`` -> canonical selection.
RESULT_TYPES: Mapping[str, Selection] = {
    "HOME": Selection.HOME,
    "AWAY": Selection.AWAY,
    "OVER": Selection.OVER,
    "UNDER": Selection.UNDER,
    "DRAW": Selection.DRAW,
}

_ALTERNATE_PREFIX = "ALTERNATE_"


# ── out of scope, by name ────────────────────────────────────────────────────

#: Market types seen on *real fixtures* that are deliberately not collected, with
#: the reason recorded.  Listing them is what makes "out of scope" a decision
#: rather than a silence: each one increments a named counter.
OUT_OF_SCOPE_MARKETS: Mapping[str, str] = {
    # Futures and outrights that ride along on a fixture's own event.
    "OUTRIGHT_BETTING": "futures",
    "MULTIPLE_TROPHIES": "futures",
    "TOP_GOALSCORER": "futures",
    "TO_BE_RELEGATED": "futures",
    "AVOID_RELEGATION": "futures",
    "TOP_5_FINISH": "futures",
    "TOP_6_FINISH": "futures",
    "CONFERENCE_WINNER": "futures",
    "CONFERENCE_WINNER_REG": "futures",
    "BETTING_WITHOUT": "futures",
    "STRAIGHT_FORECAST": "futures",
    # A different settlement window: these include cup extra time and penalties,
    # so they are not the 90-minute contract FULL_GAME means for soccer.
    "TO_QUALIFY_FOR_THE_NEXT_ROUND": "different_settlement_window",
    # Real full-game markets whose shape is not one of the four canonical ones.
    "DOUBLE_CHANCE": "combined_outcome",
    "DRAW_NO_BET": "voids_on_draw",
    "BOTH_TEAMS_TO_SCORE": "goal_prop",
    "TEAM_TO_SCORE_THE_FIRST_GOAL": "goal_prop",
    "FULL_TIME_RESULT_-_2_UP": "bonus_settlement_rule",
    "WINNING_MARGIN": "margin_prop",
    "METHOD_OF_VICTORY": "exotic",
    "NUMBER_OF_TEAM_GOALS": "team_prop",
    "RESULT_&_BOTH_TO_SCORE": "combined_outcome",
    "A_GOAL_SCORED_IN_BOTH_HALVES": "goal_prop",
}

#: Families of out-of-scope market types.  Patterns rather than literals because
#: FanDuel adds prop types continuously and each addition would otherwise arrive
#: as a rejection per fixture — 127 soccer fixtures make that a flood that hides
#: the one rejection that matters.  Checked only *after* the in-scope tables, so
#: a pattern can never shadow a market that is collected.
OUT_OF_SCOPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"HALF|QUARTER|INNING|PERIOD|MINUTES?\b|\b\d+_?MIN"), "period_out_of_scope"),
    (re.compile(r"EXTRA_TIME|PENALT"), "different_settlement_window"),
    (
        re.compile(
            # ``TO_RECORD`` is FanDuel's prefix for a player statistical
            # threshold — ``TO_RECORD_6+_REBOUNDS_WNBA`` and its 8+ sibling
            # rejected the 2026-08-08 Pennsylvania run, which is what a market
            # type neither collected nor declared is supposed to do.  Named as a
            # family rather than as the two seen, because the threshold and the
            # statistic both vary and listing instances invites the same run
            # failing next week on ``TO_RECORD_10+_POINTS``.
            r"GOAL_?SCORER|TO_SCORE|TO_RECORD|HAT-TRICK|CORRECT_SCORE|MULTIGOL"
            r"|RACE_TO|CORNER|CARD|BOOKING|PLAYER|ASSIST|SHOT|POINTS_SGP"
            r"|TOUCHDOWN|ODD_OR_EVEN|CLEAN_SHEET|BOTH_TO_SCORE"
        ),
        "prop",
    ),
    (
        re.compile(
            r"FUTURES|WINNER|CHAMPION|AWARD|SPECIAL|SEED|PLAYOFF|DIVISION"
            r"|CONFERENCE|RELEGAT|TROPHY|MVP|DRAFT|SEASON|CY_YOUNG|ROOKIE"
            r"|COACH_OF_THE_YEAR|EXACT_RESULT|EXACT_FINISH|STAGE_OF_ELIMINATION"
        ),
        "futures",
    ),
    (re.compile(r"TEAM_TOTAL|X\+_|_X\+|_SGP$|_SPP$|_SB$|_SGP_"), "team_or_season_prop"),
)


def classify_out_of_scope(market_type: str) -> str | None:
    """Reason this market type is deliberately not collected, or ``None``."""
    literal = OUT_OF_SCOPE_MARKETS.get(market_type)
    if literal is not None:
        return literal
    for pattern, reason in OUT_OF_SCOPE_PATTERNS:
        if pattern.search(market_type):
            return reason
    return None


# ── league vocabularies ──────────────────────────────────────────────────────

#: Canonical league key -> the page key its fixtures arrive on.  This is the one
#: place a league is wired to a request; :data:`PAGES` describes the request.
#:
#: The mapping is deliberately many-to-one for tennis and soccer: FanDuel serves
#: every tennis tour, and every soccer competition, from a *single* response.
#: Requesting one page per league would fetch the same megabyte six times.
PAGE_BY_LEAGUE: Mapping[str, str] = {
    "MLB": "mlb",
    "NBA": "nba",
    "WNBA": "wnba",
    "NHL": "nhl",
    "NFL": "nfl",
    "ATP": "tennis",
    "WTA": "tennis",
    "ATP_CHALLENGER": "tennis",
    "ITF": "tennis",
    "EPL": "soccer",
    "MLS": "soccer",
    "LA_LIGA": "soccer",
    "SERIE_A": "soccer",
    "BUNDESLIGA": "soccer",
    "LIGUE_1": "soccer",
    "SOCCER_OTHER": "soccer",
}

#: Everything this adapter knows how to collect.  The NBA is included even though
#: it is in its offseason and returns futures containers only: a configured
#: league that produces nothing is visible as a gap in coverage reporting, while
#: an unconfigured one is indistinguishable from one that was never asked for.
DEFAULT_LEAGUES: tuple[str, ...] = tuple(PAGE_BY_LEAGUE)

#: FanDuel competition name -> canonical soccer league.  Matched **exactly**, so
#: "German Bundesliga 2" lands in the catch-all instead of being filed as the
#: Bundesliga.  Anything unlisted becomes ``SOCCER_OTHER``, which exists so a
#: fixture is never dropped merely because its competition is unrecognised.
SOCCER_LEAGUE_BY_COMPETITION: Mapping[str, str] = {
    "English Premier League": "EPL",
    "US MLS": "MLS",
    "US Major League Soccer": "MLS",
    "Spanish La Liga": "LA_LIGA",
    "Italian Serie A": "SERIE_A",
    "German Bundesliga": "BUNDESLIGA",
    "French Ligue 1": "LIGUE_1",
}

SOCCER_FALLBACK_LEAGUE = "SOCCER_OTHER"

#: Where a tennis competition goes when :func:`_common.tennis_tour` reads no tour
#: out of its name.  ITF is the safe default: it is the one tour registered for
#: both genders, and because league is **not** part of event identity (see
#: :mod:`src.leagues`) the choice affects coverage labelling only — never whether
#: two books' prices join.
TENNIS_FALLBACK_LEAGUE = "ITF"


def soccer_league_for(competition_name: str) -> str:
    return SOCCER_LEAGUE_BY_COMPETITION.get(
        competition_name.strip(), SOCCER_FALLBACK_LEAGUE
    )


def tennis_league_for(competition_name: str) -> str:
    found = tennis_tour(competition_name, markers=TENNIS_TOURS_BY_GENDER)
    if found is not None:
        return found
    log.debug(
        "fanduel: tennis competition %r matched no tour rule; filing under %s",
        competition_name,
        TENNIS_FALLBACK_LEAGUE,
    )
    return TENNIS_FALLBACK_LEAGUE


# ── pages ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PageSpec:
    """One FanDuel request shape, and how to read what comes back."""

    key: str
    """Short stable page identifier.  For a single-league page this *is* the
    lowercased league key, which is what makes the endpoint label identify the
    league."""

    sport: Sport
    path: str
    params: Mapping[str, str]
    separator: str
    """Event-name separator.  ``" @ "`` on the US pages (``"Away @ Home"``),
    ``" v "`` on the soccer and tennis sport pages (``"Home v Away"``)."""

    home_first: bool
    """Whether the name lists the home side first.  Verified live in both
    directions: FanDuel writes US games away-first and soccer games home-first,
    confirmed against ``result.type`` on the moneyline."""

    markets: Mapping[str, MarketSpec]
    fixed_league: str | None = None
    """Canonical league key when the page serves exactly one league."""

    fixed_line_markets: bool = False
    """Whether to try :func:`_soccer_fixed_line_spec` for unrecognised types."""

    duplicate_markets: Mapping[str, str] = field(default_factory=dict)
    reject_unresolved_participants: bool = True
    """Whether a two-sided event name whose sides do not resolve is a failure.

    True for closed-roster leagues, where an unresolvable club name means the
    roster is out of date and that must be loud.  Also true for the open-roster
    sports, where ``resolve_open`` only declines names carrying outright markers
    — which on a two-sided name would be a genuine surprise."""

    skip_names_with_slash: bool = False
    """Tennis doubles.  ``"R Galloway / E King v F Reynolds / J Watt"`` is a real
    market, but a *pair* is not an identity :mod:`src.participants` models, and
    the initialised surnames cannot be reconciled with another book's spelling of
    the same pair.  Collecting them would add rows that can never join and could
    collide with a singles slug, so they are counted out of scope instead."""


def _us_page(page_key: str, sport: Sport, league_key: str) -> PageSpec:
    """One of the four US league pages.  They differ only in three fields."""
    return PageSpec(
        key=page_key,
        sport=sport,
        path=CONTENT_PAGE_PATH,
        params={"page": "CUSTOM", "customPageId": page_key},
        separator=" @ ",
        home_first=False,
        markets=US_GAME_MARKETS,
        fixed_league=league_key,
    )


#: The one page whose fixtures get a second, per-event request.  Named rather
#: than spelled out at each use: the fetch loop, the endpoint router and the
#: detail hop's scope name all mean *this* page, and one of them was reading a
#: leaked loop variable that agreed with it only because "soccer" is last in
#: :data:`PAGES`.
SOCCER_PAGE_KEY = "soccer"

PAGES: Mapping[str, PageSpec] = {
    "mlb": _us_page("mlb", Sport.BASEBALL, "MLB"),
    "wnba": _us_page("wnba", Sport.BASKETBALL, "WNBA"),
    "nba": _us_page("nba", Sport.BASKETBALL, "NBA"),
    "nhl": _us_page("nhl", Sport.HOCKEY, "NHL"),
    "nfl": _us_page("nfl", Sport.FOOTBALL, "NFL"),
    "tennis": PageSpec(
        key="tennis",
        sport=Sport.TENNIS,
        path=CONTENT_PAGE_PATH,
        params={"page": "SPORT", "eventTypeId": "2"},
        separator=" v ",
        home_first=True,
        markets=TENNIS_MARKETS,
        skip_names_with_slash=True,
    ),
    "soccer": PageSpec(
        key="soccer",
        sport=Sport.SOCCER,
        path=CONTENT_PAGE_PATH,
        params={"page": "SPORT", "eventTypeId": "1"},
        separator=" v ",
        home_first=True,
        markets=SOCCER_SPORT_PAGE_MARKETS,
    ),
}

#: The per-event follow-up.  Modelled as its own page because it reads a
#: different market vocabulary from the same sport's slate page.
SOCCER_EVENT_PAGE = PageSpec(
    key="soccer",
    sport=Sport.SOCCER,
    path=EVENT_PAGE_PATH,
    params={"tab": POPULAR_TAB},
    separator=" v ",
    home_first=True,
    markets=SOCCER_EVENT_PAGE_MARKETS,
    fixed_line_markets=True,
    duplicate_markets=SOCCER_EVENT_PAGE_DUPLICATES,
)


# ── endpoint labels ──────────────────────────────────────────────────────────
#
# The label is not cosmetic: it becomes part of the stored filename, part of
# every row's ``raw_ref``, and the key ``parse`` groups on to pick the latest
# response.  So it must be derived from configuration, never from a clock, a
# counter, or an iteration order.

#: ``mlb`` -> ``content-managed-page-mlb``.
SPORT_PAGE_ENDPOINTS: Mapping[str, str] = {
    page_key: f"{CONTENT_PAGE_PATH}-{page_key}" for page_key in PAGES
}

_ENDPOINTS_TO_PAGE: Mapping[str, PageSpec] = {
    endpoint: PAGES[page_key] for page_key, endpoint in SPORT_PAGE_ENDPOINTS.items()
}

#: League key -> the dash-form used inside an event-page label.  Underscores are
#: replaced because :func:`src.raw_store._slug` collapses them in the filename
#: anyway, and a label that survives slugging unchanged is easier to trace back.
LEAGUE_LABELS: Mapping[str, str] = {
    key: key.lower().replace("_", "-") for key in PAGE_BY_LEAGUE
}

_LEAGUE_BY_LABEL: Mapping[str, str] = {label: key for key, label in LEAGUE_LABELS.items()}


def event_page_endpoint(league_key: str, event_id: str) -> str:
    """``("EPL", "35785196")`` -> ``"event-page-epl-35785196"``.

    Deterministic in both arguments, so the same fixture yields the same label
    on every run and the stored capture is diffable across days.
    """
    label = LEAGUE_LABELS.get(league_key)
    if label is None:
        raise KeyError(f"{league_key!r} is not a league this adapter collects")
    return f"{EVENT_PAGE_PATH}-{label}-{event_id}"


def page_for_endpoint(endpoint: str) -> PageSpec | None:
    """Which page spec reads the response stored under *endpoint*."""
    page = _ENDPOINTS_TO_PAGE.get(endpoint)
    if page is not None:
        return page
    prefix = f"{EVENT_PAGE_PATH}-"
    if not endpoint.startswith(prefix):
        return None
    league_label, _, event_id = endpoint[len(prefix) :].rpartition("-")
    if not event_id or league_label not in _LEAGUE_BY_LABEL:
        return None
    if PAGE_BY_LEAGUE[_LEAGUE_BY_LABEL[league_label]] != SOCCER_PAGE_KEY:
        return None
    return SOCCER_EVENT_PAGE


# ── the adapter ──────────────────────────────────────────────────────────────


class FanDuelAdapter:
    """Collects FanDuel pregame game markets for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        state: str = DEFAULT_STATE,
        app_key: str = DEFAULT_APP_KEY,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        soccer_detail: bool = True,
        soccer_detail_limit: int | None = None,
        proxy_state: str | None = None,
        *,
        source_key: str = SOURCE_KEY,
    ) -> None:
        unknown = [key for key in leagues if key not in PAGE_BY_LEAGUE]
        if unknown:
            raise ValueError(
                f"FanDuel cannot collect league(s) {unknown}; "
                f"known: {sorted(PAGE_BY_LEAGUE)}"
            )
        # Ordered by the registry, not by the caller, so two instances configured
        # with the same set request the same pages in the same order.
        self._leagues = tuple(key for key in PAGE_BY_LEAGUE if key in set(leagues))
        self._source_key = source_key
        self.state = state
        self.app_key = app_key
        self.soccer_detail = soccer_detail
        self.soccer_detail_limit = soccer_detail_limit
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, proxy_state=proxy_state
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Markets this instance claims, per league — and it depends on the tier.

        FanDuel is the source that makes tiering visible in the data rather than
        only in the request count.  Its soccer spreads and totals exist **only**
        on the per-event page, which is 126 of its 133 requests, so a ``core``
        pass genuinely collects soccer moneylines and nothing else.  Saying that
        out loud is what stops validation reporting ``core_market_absent`` — "a
        source label has probably changed" — for a market the run deliberately
        did not ask for.
        """
        claims: dict[str, frozenset[Market]] = {}
        for key in self._leagues:
            page = PAGES[PAGE_BY_LEAGUE[key]]
            markets = frozenset(spec.market for spec in page.markets.values())
            if page.sport is Sport.SOCCER and tier.includes_depth and self.soccer_detail:
                markets = markets | {Market.SPREAD, Market.TOTAL}
            claims[key] = markets
        return capabilities_from(claims, self._leagues)

    @property
    def page_keys(self) -> tuple[str, ...]:
        """The pages this instance requests, in request order."""
        ordered: list[str] = []
        for league_key in self._leagues:
            page_key = PAGE_BY_LEAGUE[league_key]
            if page_key not in ordered:
                ordered.append(page_key)
        return tuple(page_key for page_key in PAGES if page_key in ordered)

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Slate pages always; the per-event soccer hop only in the depth tier.

        This is the seam the request budget turns on.  A full pass measured 133
        requests, of which 126 were soccer event pages; a core pass is seven.
        The event page is the only place FanDuel exposes soccer totals and
        handicaps, so deferring it is a real reduction in what is collected —
        which is why :meth:`capabilities` reports the smaller claim under
        ``core`` rather than letting the absence look like a broken parser.

        **A failure part-way through does not discard what came before it.**  The
        loops below tolerate one page failing, because they otherwise did the
        opposite: a single refusal on soccer event page 100 propagated out of
        ``fetch_raw``, and the collector's ``except SourceError`` branch returns
        before anything is written to the raw store — so seven slate pages and
        ninety-nine detail pages already fetched were thrown away, unparsed and
        uncaptured, over one postponed fixture.  A detail page is worth even less
        than that: it is an *addition* to a slate that has already succeeded, so
        losing one is a counted skip rather than a failure at all.
        """
        raws: list[RawResponse] = []
        soccer_raw: RawResponse | None = None
        tally = self.last_fetch = ScopeTally(self._source_key)
        for page_key in self.page_keys:
            page = PAGES[page_key]
            tally.requested(page_key)
            try:
                raw = self._get(
                    endpoint=SPORT_PAGE_ENDPOINTS[page_key], path=page.path, params=page.params
                )
                self._require_slate(raw)
            except SourceError as exc:
                log.warning("%s: %s page failed: %s", self._source_key, page_key, exc)
                tally.failed(page_key, exc)
                continue
            raws.append(raw)
            tally.produced(page_key, 1)
            if page_key == SOCCER_PAGE_KEY:
                soccer_raw = raw
        tally.require_something(what="slate page")

        if soccer_raw is not None and self.soccer_detail and tier.includes_depth:
            # One scope for the whole detail hop, registered once, whatever
            # happens inside it.
            #
            # Each refusal used to register a scope of its own —
            # ``failed(f"{page_key}:event:{event_id}", …)`` — and ``failed``
            # registers what it is handed, so the denominator grew in lockstep
            # with the numerator: 12 refusals out of 126 optional hops read as
            # 12 of 18 scopes lost, which is 67%, which is an ERROR and
            # ``report.ok = False`` on a pass that collected 99% of what it
            # asked for.  Registering the hops individually instead would fix
            # the ratio and break something worse — 126 detail scopes beside
            # six slate pages means losing five of the six sports dilutes to 4%,
            # and the sports are where the rows are.  So the hop is one scope,
            # counted in the same unit as the pages beside it, and how much of
            # it was lost is said in the message rather than in the arithmetic.
            #
            # ``page_key`` was also a leaked loop variable here, correct only
            # because ``"soccer"`` happens to be last in ``PAGES``.
            detail_scope = f"{SOCCER_PAGE_KEY}:detail"
            targets = list(self._soccer_detail_targets(soccer_raw))
            # No explicit ``requested`` call: both branches below register the
            # scope, because ``failed`` and ``produced`` register what they are
            # handed, and one of them always runs when there are targets.
            refusals: list[SourceError] = []
            landed = len(raws)
            for league_key, event_id in targets:
                try:
                    raws.append(
                        self._get(
                            endpoint=event_page_endpoint(league_key, event_id),
                            path=EVENT_PAGE_PATH,
                            params={"eventId": event_id, **SOCCER_EVENT_PAGE.params},
                            require_markets=False,
                        )
                    )
                except FormatChangeError as exc:
                    # A fixture postponed between the slate call and this one
                    # answers without an ``events`` attachment.  Its detail is an
                    # addition to a slate that already succeeded, so it costs
                    # that fixture's handicaps and nothing else.
                    log.info(
                        "%s: soccer detail for event %s unavailable: %s",
                        self._source_key, event_id, exc,
                    )
                except SourceError as exc:
                    # Every *other* refusal is recorded.  This clause used to
                    # catch them all, and this hop is 126 of the 133 requests a
                    # full pass makes — so a 403 or a rate limit across the whole
                    # loop left ``ok=True``, ``failed_scopes`` empty and
                    # ``scopes_refused`` silent, while ``capabilities(FULL)``
                    # still declared spread and total.  The total loss then
                    # surfaced only as ``core_market_absent``, whose message
                    # blames a renamed source label and sends an operator after a
                    # parser change; the *partial* loss surfaced as two warnings
                    # the code documents as firing on every normal run, so half
                    # of FanDuel's soccer handicaps could vanish with no signal.
                    refusals.append(exc)
                    log.warning(
                        "%s: soccer detail for event %s refused: %s",
                        self._source_key, event_id, exc,
                    )
            if targets:
                got = len(raws) - landed
                if refusals and len(refusals) == len(targets):
                    # Every hop refused: the enrichment scope itself is gone.
                    tally.failed(
                        detail_scope,
                        CoverageCappedError(
                            f"{self._source_key}: all {len(targets)} soccer "
                            f"detail page(s) were refused, so those fixtures "
                            f"carry no handicap or total from this source "
                            f"(first: {refusals[0]})"
                        ),
                    )
                elif refusals and got:
                    # Some hops landed — the scope answered — and some were
                    # refused.  That is a truncated enrichment, not a refused
                    # one: filing it under ``failed`` put ``soccer:detail`` in
                    # the share-lost numerator and graded a run that kept 17 of
                    # 20 detail pages the same way as one that lost the whole
                    # hop.  ``produced`` still registers the landings so the
                    # denominator knows the scope was asked for.
                    tally.produced(detail_scope, got)
                    tally.truncated(
                        detail_scope,
                        CoverageCappedError(
                            f"{self._source_key}: {len(refusals)} of {len(targets)} "
                            f"soccer detail page(s) were refused, so those fixtures "
                            f"carry no handicap or total from this source "
                            f"(first: {refusals[0]})"
                        ),
                    )
                elif refusals:
                    # Some hops refused, the rest postponed/malformed — nothing
                    # landed.  ``refusals and not got`` used to call this
                    # "all N were refused" even when only one was; that put a
                    # mixed unavailable slate in the share-lost numerator.
                    tally.produced(detail_scope, 0)
                    tally.truncated(
                        detail_scope,
                        CoverageCappedError(
                            f"{self._source_key}: {len(refusals)} of {len(targets)} "
                            f"soccer detail page(s) were refused and the rest "
                            f"came back without events, so those fixtures carry "
                            f"no handicap or total from this source "
                            f"(first: {refusals[0]})"
                        ),
                    )
                elif not got:
                    # Every hop answered the postponed/malformed shape this loop
                    # swallows on purpose.  One fixture doing that costs that
                    # fixture's handicaps; *every* fixture doing it costs the
                    # whole enrichment with nothing on ``failed_scopes`` and
                    # nothing on ``truncated_scopes``, so the run looked clean
                    # while FanDuel soccer carried no totals at all.
                    tally.produced(detail_scope, 0)
                    tally.truncated(
                        detail_scope,
                        CoverageCappedError(
                            f"{self._source_key}: all {len(targets)} soccer "
                            f"detail page(s) came back without events "
                            f"(postponed or moved), so those fixtures carry no "
                            f"handicap or total from this source"
                        ),
                    )
                else:
                    # Counted in responses that actually landed, not in hops
                    # attempted.  Every hop answering with the postponed-fixture
                    # shape is swallowed on purpose and leaves ``refusals``
                    # empty, so counting targets marked the scope as having
                    # produced 20 items when nothing at all had arrived.
                    tally.produced(detail_scope, got)
        return raws

    def _get(
        self,
        *,
        endpoint: str,
        path: str,
        params: Mapping[str, str],
        require_markets: bool = True,
    ) -> RawResponse:
        url = f"https://sbapi.{self.state}.sportsbook.fanduel.com/api/{path}"
        query = {**params, "_ak": self.app_key}
        # Raises on blocked / rate limited / geo-restricted / CAPTCHA / login /
        # HTML / empty / non-JSON, after exhausting the retry budget for the
        # kinds that are worth asking about again.  ``_ak`` is a static public
        # application key, not a credential, but it is kept out of the stored
        # envelope all the same: a capture is a document that gets shared.
        raw = self._http.get(
            url,
            endpoint=endpoint,
            params=query,
            record_params={k: v for k, v in query.items() if k != "_ak"},
        )
        envelope = require_mapping(raw.json(), source=self.source_key, endpoint=raw.endpoint)
        require_keys(envelope, ("attachments",), source=self.source_key, endpoint=raw.endpoint)
        attachments = require_mapping(
            envelope["attachments"], source=self.source_key, endpoint=raw.endpoint
        )
        expected = ("events", "markets") if require_markets else ("events",)
        require_keys(attachments, expected, source=self.source_key, endpoint=raw.endpoint)
        return raw

    def _require_slate(self, raw: RawResponse) -> None:
        """A slate page with no events or no markets is a failure, not an off day.

        Even the NBA's offseason page carries futures containers and their
        markets, so an empty one means the page moved or the request was
        answered by something other than FanDuel.
        """
        attachments = raw.json()["attachments"]
        for what in ("events", "markets"):
            require_nonempty(
                attachments.get(what),
                source=self.source_key,
                endpoint=raw.endpoint,
                what=what,
            )

    def _soccer_detail_targets(self, soccer_raw: RawResponse) -> list[tuple[str, str]]:
        """Fixtures worth a per-event request, as ``(league_key, event_id)``.

        Only fixtures that already carry a three-way moneyline on the slate page:
        that is the cheapest available proof the event is a priced game rather
        than a competition-level futures container, and it is also the condition
        under which the event page's ``WIN-DRAW-WIN`` is a duplicate rather than
        the only copy.  Ordered by start time so a truncating limit keeps the
        near fixtures, which are the ones both other books also price.
        """
        attachments = soccer_raw.json().get("attachments") or {}
        events = attachments.get("events") or {}
        competitions = attachments.get("competitions") or {}
        priced = {
            str(market.get("eventId"))
            for market in (attachments.get("markets") or {}).values()
            if str(market.get("marketType") or "") in SOCCER_SPORT_PAGE_MARKETS
        }
        configured = set(self._leagues)
        targets: list[tuple[str, str, str]] = []
        for event_id, event in events.items():
            event_id = str(event_id)
            name = str(event.get("name") or "")
            if event_id not in priced or name.count(SOCCER_EVENT_PAGE.separator) != 1:
                continue
            competition = competitions.get(str(event.get("competitionId"))) or {}
            league_key = soccer_league_for(str(competition.get("name") or ""))
            if league_key not in configured:
                continue
            targets.append((str(event.get("openDate") or ""), league_key, event_id))
        targets.sort(key=lambda item: (item[0], item[2]))
        if self.soccer_detail_limit is not None:
            targets = targets[: self.soccer_detail_limit]
        return [(league_key, event_id) for _, league_key, event_id in targets]

    # ── parse ────────────────────────────────────────────────────────────────

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_fanduel(raws)

    def close(self) -> None:
        self._http.close()


# ── parsing (pure) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Game:
    """One accepted fixture."""

    event_id: str
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


def parse_fanduel(raws: RawResponse | Sequence[RawResponse]) -> ParseOutcome:
    """Parse captured FanDuel responses into normalized quotes.

    Pure: no network, no filesystem, no clock, **no configuration**, no surviving
    state.  Everything time-related comes from ``raw.fetched_at``, and the
    ``source`` every row carries comes from the envelope rather than from this
    module's constant — so a second FanDuel instance, if one is ever registered
    for another state, keeps its own identity through replay.

    It took a league list until recently, and that was a genuine violation of the
    purity this docstring claims.  ``replay_run`` builds an adapter with no
    arguments, so a run collected with ``--league EPL`` replayed against *every*
    league: the same soccer page parsed to 30 rows on collection and 381 on
    replay, reported as 351 "invented" rows.  Narrowing is the collector's job and
    happens after reconciliation (:func:`src.collector.in_scope`), where it
    applies identically to both sides of the comparison; here every fixture the
    captured bytes describe is normalized, exactly as Pinnacle's parser does.
    """
    if isinstance(raws, RawResponse):
        raws = [raws]
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

    games: dict[str, _Game] = {}
    #: Event ids that will not become games, and why — so a market belonging to
    #: one is counted rather than dropped.
    unresolved: dict[str, str] = {}
    #: ``(raw, page, market_id, market)`` in a deterministic order.
    work: list[tuple[RawResponse, PageSpec, str, Mapping[str, Any]]] = []

    for raw in latest_per_endpoint(raws):
        page = page_for_endpoint(raw.endpoint)
        if page is None:
            outcome.reject(
                source,
                "unknown_endpoint",
                f"no page spec reads endpoint {raw.endpoint!r}",
                endpoint=raw.endpoint,
            )
            continue
        try:
            attachments = raw.json().get("attachments") or {}
        except ValueError as exc:
            outcome.reject(
                source,
                "unparseable_body",
                f"{raw.endpoint}: {exc}",
                endpoint=raw.endpoint,
            )
            continue
        competitions = attachments.get("competitions") or {}
        for event_id, event in (attachments.get("events") or {}).items():
            event_id = str(event_id)
            if event_id not in games and event_id not in unresolved:
                game = _classify_event(
                    event_id=event_id,
                    source=source,
                    event=event,
                    page=page,
                    competitions=competitions,
                    outcome=outcome,
                    unresolved=unresolved,
                    captured_at=raw.fetched_at,
                )
                if game is not None:
                    games[event_id] = game
        for market_id, market in (attachments.get("markets") or {}).items():
            work.append((raw, page, str(market_id), market))

    event_keys = resolve_doubleheaders(
        {event_id: (game.base_key, game.commence_time) for event_id, game in games.items()}
    )

    for raw, page, market_id, market in work:
        _parse_market(
            raw=raw,
            source=source,
            page=page,
            market_id=market_id,
            market=market,
            games=games,
            unresolved=unresolved,
            event_keys=event_keys,
            outcome=outcome,
        )

    return outcome


def _classify_event(
    *,
    event_id: str,
    source: str,
    event: Mapping[str, Any],
    page: PageSpec,
    competitions: Mapping[str, Any],
    outcome: ParseOutcome,
    unresolved: dict[str, str],
    captured_at: datetime,
) -> _Game | None:
    """Accept a real two-competitor fixture, or record why not.

    Every path either returns a game or writes to *unresolved* with a reason, so
    the markets that belong to a rejected event can be counted instead of
    vanishing.
    """
    name = str(event.get("name") or "")
    league_key = page.fixed_league or _league_from_competition(page, event, competitions)

    if not is_known(league_key):
        unresolved[event_id] = "unknown_league"
        outcome.reject(
            source,
            "unknown_league",
            f"event {event_id} ({name!r}) mapped to unregistered league {league_key!r}",
            event_id=event_id,
        )
        return None
    competition = get_league(league_key)

    # A futures container is named for the competition or the award, not for two
    # competitors: "NFL Futures", "English Premier League", "Men's US Open 2026".
    # It therefore carries no separator, which is the cheapest reliable test and
    # the one that keeps "NHL Specials" out of the fixture list.
    if name.count(page.separator) != 1:
        unresolved[event_id] = "non_game"
        outcome.skipped["non_game_event"] += 1
        return None
    first_raw, second_raw = name.split(page.separator, 1)

    if page.skip_names_with_slash and ("/" in first_raw or "/" in second_raw):
        unresolved[event_id] = "doubles"
        outcome.skipped["tennis_doubles"] += 1
        return None

    # A women's, reserve or youth competition whose *name* carries the marker
    # while the team names do not.  This adapter maps every unrecognised soccer
    # competition into one catch-all league, so the competition name is then
    # read by nothing — and its own soccer page carries, by name, "English
    # Women's Championship", "German Frauen-Bundesliga", "Mexican Liga MX
    # Femenil", "Norwegian Toppserien Ladies", "Australian A-League - Women" and
    # "Friendlies Women's International".  Without this a women's fixture here
    # and the men's fixture of the same two clubs at another venue produce a
    # byte-identical event key and are priced as one market.
    #
    # The guard existed and was wired into Pinnacle alone; the recorded cost of
    # this exact shape there was an 11.2% "guaranteed" position ranked first in
    # the whole run.
    marker = competition_marker(
        str((competitions.get(str(event.get("competitionId"))) or {}).get("name") or ""),
        competition.sport,
    )
    first_raw = with_marker(first_raw, marker)
    second_raw = with_marker(second_raw, marker)

    first = canonical_participant(first_raw, competition)
    second = canonical_participant(second_raw, competition)
    if first is None or second is None or first.key == second.key:
        unresolved[event_id] = "unresolved_participants"
        detail = (
            f"event {event_id} ({name!r}) in {league_key}: "
            f"{first_raw!r} -> {first.key if first else None}, "
            f"{second_raw!r} -> {second.key if second else None}"
        )
        if page.reject_unresolved_participants:
            outcome.reject(source, "unresolved_participants", detail, event_id=event_id)
        else:
            outcome.skipped["unresolved_participants"] += 1
        return None

    commence_time = parse_iso_time(event.get("openDate"))
    if commence_time is None:
        unresolved[event_id] = "missing_commence_time"
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} ({name!r}) has unparseable openDate "
            f"{event.get('openDate')!r}",
            event_id=event_id,
        )
        return None

    # This collector's scope is **pregame**.  FanDuel's slate keeps an event
    # listed after it starts, and its prices then are in-play prices: they move on
    # the run of play, and they are not the same product as a pregame line even
    # though nothing on the row says so.  Comparing one against another book's
    # pregame price is a false comparison, so an event that has already started as
    # of the moment this payload was captured is out of scope.
    #
    # `captured_at` is `raw.fetched_at` — data carried in the stored envelope, not
    # a reading of the clock — so `parse` stays a pure function of its bytes and
    # replaying an old capture reproduces the same decision.
    if commence_time <= captured_at:
        unresolved[event_id] = "started"
        outcome.skipped["event_already_started"] += 1
        return None

    # Tennis has no home player, so the book's ordering carries no information and
    # `orient` imposes its own by participant key.  Everywhere else the book's
    # assignment is the fact and is handed over explicitly.
    book_home = (first if page.home_first else second) if competition.has_home_away else None
    away, home = orient(first, second, competition, home=book_home)

    return _Game(
        event_id=event_id,
        competition=competition,
        home=home,
        away=away,
        commence_time=commence_time,
        base_key=build_event_key(away.key, home.key, commence_time, competition),
        marker=marker,
    )


def _league_from_competition(
    page: PageSpec, event: Mapping[str, Any], competitions: Mapping[str, Any]
) -> str:
    competition = competitions.get(str(event.get("competitionId"))) or {}
    name = str(competition.get("name") or "")
    if page.sport is Sport.SOCCER:
        return soccer_league_for(name)
    if page.sport is Sport.TENNIS:
        return tennis_league_for(name)
    raise AssertionError(f"page {page.key} has no fixed league and no rules")


def _parse_market(
    *,
    raw: RawResponse,
    source: str,
    page: PageSpec,
    market_id: str,
    market: Mapping[str, Any],
    games: Mapping[str, _Game],
    unresolved: Mapping[str, str],
    event_keys: Mapping[str, str],
    outcome: ParseOutcome,
) -> None:
    event_id = str(market.get("eventId", ""))
    market_type = str(market.get("marketType") or "")
    game = games.get(event_id)
    if game is None:
        reason = unresolved.get(event_id)
        if reason is None:
            outcome.reject(
                source,
                "market_without_event",
                f"{market_type or 'unnamed market'} {market_id} references event "
                f"{event_id!r}, which the payload does not describe",
                market_id=market_id,
                event_id=event_id,
            )
        else:
            # The event was already counted or rejected once on its own; counting
            # its markets separately is what the previous version left out, and
            # it is the difference between "48 markets collected" and "48 of 118
            # collected, 70 belong to futures containers".
            outcome.skipped[f"market_on_{reason}_event"] += 1
        return

    resolved = resolve_market_type(market_type, page)
    if resolved is None:
        duplicate = page.duplicate_markets.get(market_type)
        if duplicate is not None:
            outcome.skipped[f"{duplicate}:{market_type}"] += 1
            return
        out_of_scope = classify_out_of_scope(market_type)
        if out_of_scope is not None:
            outcome.skipped[f"out_of_scope_market:{out_of_scope}"] += 1
            return
        outcome.reject(
            source,
            "unmapped_market_type",
            f"{market_type!r} on in-scope event {event_id} ({game.away.name} @ "
            f"{game.home.name}) is neither collected nor declared out of scope",
            market_id=market_id,
            event_id=event_id,
        )
        return
    spec, is_alternate = resolved

    runners = market.get("runners") or []
    if not runners:
        outcome.reject(
            source,
            "market_without_runners",
            f"{market_type} on event {event_id} has no runners",
            market_id=market_id,
            event_id=event_id,
        )
        return

    # The feed's own liveness flag, which nothing read.
    #
    # Pregame scope was decided entirely by ``commence_time <= captured_at`` — a
    # proxy for the question the payload answers directly.  On the captured
    # tennis slate the proxy and the flag agree on 186 of 187 fixtures and
    # disagree on one: market 717.178490412, *Pablo Perez Ramos v Markus
    # Molder*, ``openDate`` 99 seconds after the fetch and ``"inPlay": true``.
    # The proxy admits it, and once the row is written nothing else in the
    # payload says it is a live price.  972 of the captured markets carry the
    # flag and 46 of them are true, so it is neither rare nor optional.
    if market.get("inPlay") is True:
        outcome.skipped["market_in_play"] += 1
        return

    market_open = str(market.get("marketStatus") or "OPEN").upper() == "OPEN"
    for runner in runners:
        quote = _build_quote(
            raw=raw,
            source=source,
            page=page,
            runner=runner,
            market_id=market_id,
            market_type=market_type,
            spec=spec,
            is_alternate=is_alternate,
            market_open=market_open,
            game=game,
            event_key=event_keys[game.event_id],
            outcome=outcome,
        )
        if quote is not None:
            outcome.quotes.append(quote)


def resolve_market_type(
    market_type: str, page: PageSpec
) -> tuple[MarketSpec, bool] | None:
    """``marketType`` -> ``(spec, is_alternate)``, or ``None`` if not collected.

    ``ALTERNATE_`` variants are the same contract at a non-headline line, so they
    map to the same market and set the flag.  The flag is not decoration:
    ``ALTERNATE_MONEY_LINE`` differs from ``MONEY_LINE`` in no other field, so
    without it the two rows collide on ``dedup_key`` and the run's insert aborts.
    """
    is_alternate = market_type.startswith(_ALTERNATE_PREFIX)
    base = market_type[len(_ALTERNATE_PREFIX) :] if is_alternate else market_type
    spec = page.markets.get(base)
    if spec is None and page.fixed_line_markets:
        spec = _soccer_fixed_line_spec(base)
    if spec is None:
        return None
    return spec, is_alternate


def _build_quote(
    *,
    raw: RawResponse,
    source: str,
    page: PageSpec,
    runner: Mapping[str, Any],
    market_id: str,
    market_type: str,
    spec: MarketSpec,
    is_alternate: bool,
    market_open: bool,
    game: _Game,
    event_key: str,
    outcome: ParseOutcome,
) -> Quote | None:
    runner_name = str(runner.get("runnerName") or "")
    selection, line, problem = _resolve_runner(runner=runner, spec=spec, game=game)
    if problem is not None:
        outcome.reject(
            source,
            problem,
            f"{market_type} runner {runner_name!r} on event {game.event_id} "
            f"({game.away.name} @ {game.home.name})",
            market_id=market_id,
            event_id=game.event_id,
        )
        return None

    odds_block = runner.get("winRunnerOdds") or {}
    decimal_raw = ((odds_block.get("trueOdds") or {}).get("decimalOdds") or {}).get(
        "decimalOdds"
    )
    if decimal_raw is None:
        # A suspended runner legitimately carries no price.
        outcome.skipped["runner_without_price"] += 1
        return None

    # A price below 1.01 decimal is "risk two hundred to win one" — no book takes
    # that bet, so the value is a placeholder rather than a quote.  Observed live
    # on a suspended FanDuel runner at 1.005.  Emitting it would put a number
    # outside `src.normalize`'s declared domain into the store, where every later
    # reader treats it as a real price: it implies a 99.5% probability, which
    # drags any overround sum it lands in above 1.0 on its own and can mask a
    # genuinely mispaired market.
    try:
        if float(decimal_raw) < MIN_DECIMAL_ODDS:
            outcome.skipped["price_below_plausible_minimum"] += 1
            return None
    except (TypeError, ValueError):
        outcome.reject(
            source,
            "unreadable_price",
            f"{market_type} runner {runner_name!r} on event {game.event_id} "
            f"has an unparseable decimalOdds {decimal_raw!r}",
            market_id=market_id,
            event_id=game.event_id,
        )
        return None

    # The feed publishes both formats.  Taking each from the feed rather than
    # deriving one from the other keeps the downstream consistency check an
    # actual check instead of a tautology about our own arithmetic.
    american_raw = (odds_block.get("americanDisplayOdds") or {}).get("americanOdds")
    runner_active = str(runner.get("runnerStatus") or "ACTIVE").upper() == "ACTIVE"

    try:
        decimal_odds = float(decimal_raw)
        american_odds = (
            int(american_raw)
            if american_raw is not None and abs(int(american_raw)) >= 100
            else decimal_to_american(decimal_odds)
        )
        return Quote(
            source=source,
            observed_at=raw.fetched_at,
            raw_ref=raw.ref,
            sport=page.sport,
            league=game.competition.key,
            event_key=event_key,
            source_event_id=game.event_id,
            home_participant=game.home.key,
            away_participant=game.away.key,
            home_team=game.home.name,
            away_team=game.away.name,
            commence_time=game.commence_time,
            market=spec.market,
            period=spec.period,
            selection=selection,
            line=line,
            is_alternate=is_alternate,
            decimal_odds=decimal_odds,
            american_odds=american_odds,
            implied_probability=implied_probability(decimal_odds),
            source_market_id=market_id,
            source_selection_id=_selection_id(runner),
            status=QuoteStatus.ACTIVE if (market_open and runner_active) else QuoteStatus.SUSPENDED,
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            source,
            "invalid_quote",
            f"{market_type}/{selection} on event {game.event_id}: {exc}",
            market_id=market_id,
            event_id=game.event_id,
        )
        return None


def _resolve_runner(
    *, runner: Mapping[str, Any], spec: MarketSpec, game: _Game
) -> tuple[Selection | None, float | None, str | None]:
    """``(selection, line, reject_reason)`` for one runner.

    The line is always returned from *this runner's* perspective, which is what
    makes ``home.line == -away.line`` hold for spreads and ``over.line ==
    under.line`` hold for totals.
    """
    runner_name = str(runner.get("runnerName") or "")
    needs_line = spec.market in MARKETS_REQUIRING_LINE

    if spec.selection_from is SelectionFrom.OVER_UNDER_NAME:
        match = _TOTAL_RUNNER.match(runner_name.strip())
        if match is None:
            return None, None, "unparseable_total_runner"
        selection = (
            Selection.OVER if match.group("side").lower() == "over" else Selection.UNDER
        )
        line = float(match.group("line"))
    elif spec.selection_from is SelectionFrom.PARTICIPANT:
        name_part = runner_name.strip()
        line = None
        if spec.line_from is LineFrom.RUNNER_NAME:
            match = _HANDICAP_RUNNER.match(name_part)
            if match is None:
                return None, None, "unparseable_handicap_runner"
            name_part = match.group("name")
            line = float(match.group("line"))
        # Through the fixture's own competition marker.  Applied when the
        # fixture was built and not here, a women's or reserve competition —
        # which this page carries by name — rejected every priced runner and
        # marked the source unhealthy.
        participant = canonical_participant(
            with_marker(name_part, game.marker), game.competition
        )
        if participant is None:
            return None, None, "unresolved_runner_participant"
        if participant.key == game.home.key:
            selection = Selection.HOME
        elif participant.key == game.away.key:
            selection = Selection.AWAY
        else:
            return None, None, "runner_not_a_participant_of_the_event"
    else:
        result_type = str((runner.get("result") or {}).get("type") or "")
        found = RESULT_TYPES.get(result_type)
        if found is None:
            return None, None, "unknown_selection"
        selection = found
        line = None

    if needs_line and line is None:
        if spec.line_from is LineFrom.HANDICAP:
            handicap = runner.get("handicap")
            if handicap is None:
                return None, None, "market_without_line"
            try:
                line = float(handicap)
            except (TypeError, ValueError):
                return None, None, "unparseable_line"
        else:
            return None, None, "market_without_line"
    if not needs_line:
        # FanDuel sends handicap 0 on a moneyline; a market that carries no line
        # must not acquire one.
        line = None

    if line is not None and spec.declared_line is not None:
        if abs(abs(line) - spec.declared_line) > 1e-9:
            return None, None, "line_disagrees_with_market_type"

    return selection, line, None


def _selection_id(runner: Mapping[str, Any]) -> str | None:
    value = runner.get("selectionId")
    return None if value is None else str(value)
