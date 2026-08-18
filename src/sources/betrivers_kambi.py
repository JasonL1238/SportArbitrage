"""BetRivers game markets from the public Kambi offering API.

BetRivers' web sportsbook is served by Kambi, whose offering API at
``eu-offering-api.kambicdn.com`` is the unauthenticated endpoint the public page
reads from.  Two stages are needed per league:

1. ``listView/{path}.json`` enumerates the slate but carries only the headline
   market — one bet offer per event.  Collecting only this is why an early
   version produced 32 rows for a 16-game slate and no run lines or totals.
2. ``betoffer/event/{ids}.json`` returns the full market set per event and
   accepts comma-separated ids, so a whole league costs a couple of requests
   rather than one per game.

One adapter serves every sport because the payload shape is identical across
them; only the *vocabulary* differs, and that lives in the declarative tables
below rather than in code paths.

Field traps this parser exists to get right
-------------------------------------------

**Odds and lines are both in thousandths.**  ``1640`` is 1.640 decimal and a
``line`` of ``8000`` is a total of 8.0.  Dividing the odds but not the line
emits totals of 8000 runs.

**``closed`` is a betting cutoff timestamp, not a boolean.**  Every normal
pre-match offer carries one (``"2026-09-29T23:00:00Z"``), so reading it as
truthy marks the entire slate suspended.

**Criterion labels encode the settlement rule, so they are matched exactly.**
``"Match Odds (Chase Burns must start)"`` is a pitcher-conditional market and
``"Match Odds - Regular Time"`` is a 60-minute three-way hockey market; both
would be collected as the moneyline by a substring match, and each would put a
second, differently-settled price on the same selection.  The tables are keyed by
``(Sport, label)`` because the same text means different things in different
sports — ``"Total Points - Including Overtime"`` occurs in both basketball and
gridiron football, and ``"Handicap"`` is a first-five-innings market in baseball
and a 90-minute two-way goal handicap in soccer.

**Sides come from ``participant``, never from outcome ordering.**  A two-way
market labels its outcomes with team names, but a three-way market labels them
``"1"``/``"X"``/``"2"``, so ``OT_ONE``/``OT_TWO`` ordering carries no identity.
``OT_CROSS`` is the draw.

**``tags`` carries ``MAIN_LINE`` on exactly one offer per (event, criterion)
group** — verified 39/39 on the captured MLB slate and 1-per-group on every
other sport.  That is the only signal separating the primary line from the
alternates, and ``Quote.dedup_key`` distinguishes a primary from an alternate at
the same number, so failing to read it made up to 13 distinct total lines per
event all look like the primary.

Soccer is where the labels stop being interchangeable
----------------------------------------------------

``Full Time`` is the 90-minute three-way moneyline.  ``Total Goals`` is the
90-minute total.  ``Handicap`` is a two-way goal handicap quoted only at
half-goal lines (verified: -3.5 … +2.5 across 24 fixtures in three
competitions), which is exactly :attr:`~src.vocab.Market.SPREAD` — it cannot
push and it cannot split.

Three neighbouring soccer markets are *not* that contract and are counted out of
scope instead:

* ``3-Way Handicap`` has three outcomes (the handicapped draw is priced).  It is
  a different product from a two-way handicap and there is no faithful
  :attr:`~src.vocab.Market.SPREAD` representation of it.
* ``Asian Total`` and ``Asian Handicap`` quote quarter lines (``-250`` = -0.25,
  ``-750`` = -0.75) which split the stake across two neighbouring lines and
  half-push, and integer lines which void.  Neither is representable on a row
  that carries a single ``line``.  Their half-line subset would additionally
  collide with ``Handicap`` on ``dedup_key`` — ``Asian Handicap`` and
  ``Handicap`` both quote -0.5 and +0.5 on the same fixture — so mapping both
  products would abort the run's insert on a UNIQUE violation.
* Extra-time and "to qualify" markets settle on a different scoring window than
  the 90 minutes :attr:`~src.vocab.Period.FULL_GAME` means for soccer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator, Mapping, Sequence

import httpx

from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
from src.leagues import is_known as league_is_known
from src.leagues import league as get_league
from src.normalize import (
    american_to_decimal,
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import (
    with_marker,
    canonical_participant,
    competition_marker,
    is_pairing,
    is_statistic,
)
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
    MARKETS_REQUIRING_SIDE,
    Market,
    Period,
    Quote,
    QuoteStatus,
    Selection,
    Side,
    Sport,
)
from src.sources._common import (
    Fixture,
    ScopeTally,
    SourceClient,
    Tier,
    capabilities_from,
    drop_duplicate_selections,
    envelope_source,
    parse_iso_time,
    priced_quote,
    response_order,
)
from src.sources.base import ParseOutcome
from src.sources.guards import (
    FormatChangeError,
    SourceError,
    require_keys,
    require_mapping,
)

log = logging.getLogger(__name__)

SOURCE_KEY = "betrivers_kambi"
DEFAULT_BASE_URL = "https://eu-offering-api.kambicdn.com/offering/v2018"
DEFAULT_OPERATOR = "rsiusil"
DEFAULT_MARKET = "US-IL"
DEFAULT_LANG = "en_US"

#: Kambi thousandths divisor for both odds and handicap lines.
THOUSANDTHS = 1000.0

#: The offering API truncates a ``betoffer`` response at this many offers and
#: reports the true count in ``range.total``.  A busy basketball or soccer
#: fixture carries 200-450 offers, so a large batch silently loses whole
#: markets; :meth:`BetRiversKambiAdapter.fetch_raw` halves the batch and retries
#: instead, and :func:`parse_kambi` reports any truncated payload it is handed.
MAX_BETOFFERS_PER_RESPONSE = 2000

#: Only pregame markets are in scope.  Kambi reports in-play events in the same
#: slate with ``state == "STARTED"``.
PREGAME_STATE = "NOT_STARTED"

#: The tag marking the primary line of a group.  Absent on markets that have no
#: line at all (moneylines), present on exactly one offer per line market.
MAIN_LINE_TAG = "MAIN_LINE"


# ── declarative vocabularies ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SportPath:
    """One Kambi group path this adapter knows how to collect.

    :param path: the ``termKey`` path used in the offering API's URLs.
    :param league: canonical :mod:`src.leagues` key.  For soccer this is the
        default for the path; the per-event competition in
        :data:`SOCCER_COMPETITIONS` refines it, because a country path such as
        ``football/brazil`` mixes several competitions.
    :param batch_size: how many event ids to request per ``betoffer`` call.
        Sport-specific because the response cap is on *offers*, not events, and
        a soccer or basketball fixture carries an order of magnitude more offers
        than a baseball one.
    """

    path: str
    sport: Sport
    league: str
    batch_size: int = 8


#: Every Kambi path this adapter can collect.  Adding one here is enough to make
#: it selectable; nothing else in the module is keyed on the path.
SPORT_PATHS: dict[str, SportPath] = {
    entry.path: entry
    for entry in (
        SportPath("baseball/mlb", Sport.BASEBALL, "MLB", batch_size=8),
        SportPath("basketball/wnba", Sport.BASKETBALL, "WNBA", batch_size=4),
        # Registered but out of season: the NBA slate is futures containers only
        # (50 of them on 2026-07-28), so it is deliberately not in
        # DEFAULT_PATHS — a configured league that can never produce a row is
        # indistinguishable, in the coverage report, from one that broke.
        SportPath("basketball/nba", Sport.BASKETBALL, "NBA", batch_size=4),
        SportPath("ice_hockey/nhl", Sport.HOCKEY, "NHL", batch_size=8),
        SportPath("american_football/nfl", Sport.FOOTBALL, "NFL", batch_size=8),
        SportPath("tennis/atp", Sport.TENNIS, "ATP", batch_size=8),
        SportPath("tennis/wta", Sport.TENNIS, "WTA", batch_size=8),
        SportPath("tennis/challenger", Sport.TENNIS, "ATP_CHALLENGER", batch_size=8),
        SportPath("tennis/itf_men", Sport.TENNIS, "ITF", batch_size=8),
        SportPath("tennis/itf_women", Sport.TENNIS, "ITF", batch_size=8),
        # A representative set of soccer competitions rather than the 57 country
        # paths the tree offers: the whole soccer tree is 3243 events, which is
        # neither polite to fetch nor useful to compare against two books that
        # cover a fraction of it.
        SportPath("football/england/premier_league", Sport.SOCCER, "EPL", batch_size=4),
        SportPath("football/usa/mls", Sport.SOCCER, "MLS", batch_size=4),
        SportPath("football/spain/la_liga", Sport.SOCCER, "LA_LIGA", batch_size=4),
        SportPath("football/italy/serie_a", Sport.SOCCER, "SERIE_A", batch_size=4),
        SportPath("football/germany/bundesliga", Sport.SOCCER, "BUNDESLIGA", batch_size=4),
        SportPath("football/france/ligue_1", Sport.SOCCER, "LIGUE_1", batch_size=4),
        SportPath("football/brazil", Sport.SOCCER, "SOCCER_OTHER", batch_size=4),
    )
}

#: What a default-constructed adapter collects.
DEFAULT_PATHS: tuple[str, ...] = (
    "baseball/mlb",
    "basketball/wnba",
    "ice_hockey/nhl",
    "american_football/nfl",
    "tennis/atp",
    "tennis/wta",
    "tennis/challenger",
    "football/england/premier_league",
    "football/usa/mls",
    "football/spain/la_liga",
    "football/italy/serie_a",
    "football/germany/bundesliga",
    "football/france/ligue_1",
    "football/brazil",
)

#: Kambi soccer competition ``termKey`` -> canonical league key.  Consulted with
#: the *event's own* path, so a country path that mixes competitions
#: (``football/brazil`` carries both Brasileirão tiers) still lands each fixture
#: on the right key.  Anything unlisted becomes ``SOCCER_OTHER``, never a
#: rejection: an unrecognised competition is a reporting detail, and league is
#: deliberately not part of event identity.
SOCCER_COMPETITIONS: dict[str, str] = {
    "premier_league": "EPL",
    "mls": "MLS",
    "la_liga": "LA_LIGA",
    "serie_a": "SERIE_A",
    "bundesliga": "BUNDESLIGA",
    "ligue_1": "LIGUE_1",
}

#: Fallback for a soccer fixture whose competition is not registered.
SOCCER_FALLBACK_LEAGUE = "SOCCER_OTHER"


@dataclass(frozen=True)
class MarketRule:
    """What one exact criterion label settles as."""

    market: Market
    period: Period


#: Exact Kambi criterion label -> canonical market and period, namespaced by
#: sport.  Matching is exact, and the namespace is the sport rather than a
#: separate vocabulary name so that a label can never be read with another
#: sport's meaning.
#:
#: The hockey block is the reason :attr:`~src.vocab.Period.REGULATION` exists.
#: ``"... - Including Overtime and Penalty Shootout"`` and
#: ``"... - Regular Time"`` are different contracts: the 60-minute moneyline is
#: three-way because a tie is a real settlement outcome, the one including the
#: shootout is two-way because it cannot tie.  Note the literal inconsistent
#: capitalisation of "penalty shootout" between the moneyline and the other two
#: hockey labels — it is the book's, not a typo here.
CRITERIA: dict[tuple[Sport, str], MarketRule] = {
    # ── baseball ─────────────────────────────────────────────────────────────
    (Sport.BASEBALL, "Moneyline"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASEBALL, "Run Line"): MarketRule(Market.SPREAD, Period.FULL_GAME),
    (Sport.BASEBALL, "Total Runs"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.BASEBALL, "Handicap - First 5 Innings"): MarketRule(
        Market.SPREAD, Period.FIRST_5_INNINGS
    ),
    (Sport.BASEBALL, "Total Runs - First 5 Innings"): MarketRule(
        Market.TOTAL, Period.FIRST_5_INNINGS
    ),
    (Sport.BASEBALL, "Inning 1"): MarketRule(Market.MONEYLINE, Period.FIRST_1_INNING),
    (Sport.BASEBALL, "Total Runs - Inning 1"): MarketRule(
        Market.TOTAL, Period.FIRST_1_INNING
    ),
    # ── basketball ───────────────────────────────────────────────────────────
    (Sport.BASKETBALL, "Moneyline - Including Overtime"): MarketRule(
        Market.MONEYLINE, Period.FULL_GAME
    ),
    (Sport.BASKETBALL, "Point Spread - Including Overtime"): MarketRule(
        Market.SPREAD, Period.FULL_GAME
    ),
    (Sport.BASKETBALL, "Total Points - Including Overtime"): MarketRule(
        Market.TOTAL, Period.FULL_GAME
    ),
    # ── hockey ───────────────────────────────────────────────────────────────
    (Sport.HOCKEY, "Moneyline - Including Overtime and penalty shootout"): MarketRule(
        Market.MONEYLINE, Period.FULL_GAME
    ),
    (Sport.HOCKEY, "Puck Line - Including Overtime and Penalty Shootout"): MarketRule(
        Market.SPREAD, Period.FULL_GAME
    ),
    (Sport.HOCKEY, "Total Goals - Including Overtime and Penalty Shootout"): MarketRule(
        Market.TOTAL, Period.FULL_GAME
    ),
    (Sport.HOCKEY, "Match Odds - Regular Time"): MarketRule(
        Market.MONEYLINE, Period.REGULATION
    ),
    (Sport.HOCKEY, "Puck Line - Regular Time"): MarketRule(
        Market.SPREAD, Period.REGULATION
    ),
    (Sport.HOCKEY, "Total Goals - Regular Time"): MarketRule(
        Market.TOTAL, Period.REGULATION
    ),
    # ── gridiron football ────────────────────────────────────────────────────
    (Sport.FOOTBALL, "Moneyline - Including Overtime"): MarketRule(
        Market.MONEYLINE, Period.FULL_GAME
    ),
    (Sport.FOOTBALL, "Point Spread - Including Overtime"): MarketRule(
        Market.SPREAD, Period.FULL_GAME
    ),
    (Sport.FOOTBALL, "Total Points - Including Overtime"): MarketRule(
        Market.TOTAL, Period.FULL_GAME
    ),
    # ── tennis ───────────────────────────────────────────────────────────────
    (Sport.TENNIS, "Match Odds"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    # ── soccer ───────────────────────────────────────────────────────────────
    (Sport.SOCCER, "Full Time"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.SOCCER, "Total Goals"): MarketRule(Market.TOTAL, Period.FULL_GAME),
    (Sport.SOCCER, "Handicap"): MarketRule(Market.SPREAD, Period.FULL_GAME),
}

#: Team-total criteria, as ``(prefix, suffix)`` around the club name:
#: ``"Total Runs by CIN Reds"``.  Baseball only — the other sports offer team
#: totals too, but they are outside the collected market set and are counted out
#: of scope rather than half-supported.
TEAM_TOTAL_AFFIXES: dict[Sport, tuple[str, str]] = {
    Sport.BASEBALL: ("Total Runs by ", ""),
}

#: How Kambi separates a market from the scoring window it applies to, as in
#: ``"Total Runs - First 5 Innings"``.  Used to refuse a team-total label that
#: carries one, since that market's period is not the one the rule assumes.
_PERIOD_QUALIFIER = " - "

#: How a book decorates the second game of a doubleheader on a participant
#: name: ``"White Sox - Game 2"``.  Stripped before the period test, because it
#: is an identity marker and not a scoring window.
_GAME_ORDINAL = re.compile(r"\s*-\s*Game\s+\d+\s*$", re.IGNORECASE)

def _full_game_markets(sport: Sport) -> frozenset[Market]:
    """Markets this adapter maps for *sport* at the whole-contest window.

    Derived from :data:`CRITERIA` rather than restated, so a capability claim
    cannot drift away from what the parser will actually produce — a claim that
    outran the code would tell validation to expect rows that never arrive.
    """
    return frozenset(
        rule.market
        for (mapped_sport, _), rule in CRITERIA.items()
        if mapped_sport is sport and rule.period is Period.FULL_GAME
    ) | (
        {Market.TEAM_TOTAL} if sport in TEAM_TOTAL_AFFIXES else frozenset()
    )


#: Criteria that are deliberately not collected, with the reason each is a
#: different contract rather than a variant of one we keep.  Listed explicitly
#: so the traps stay visible in the skip counters instead of disappearing into
#: the generic bucket.
OUT_OF_SCOPE: dict[tuple[Sport, str], str] = {
    (Sport.SOCCER, "3-Way Handicap"): "soccer_3way_handicap_prices_a_draw",
    (Sport.SOCCER, "3-Way Handicap - 1st Half"): "soccer_3way_handicap_prices_a_draw",
    (Sport.SOCCER, "Corners 3-Way Handicap"): "corners_market",
    (Sport.SOCCER, "Asian Total"): "asian_quarter_line_splits_the_stake",
    (Sport.SOCCER, "Asian Total - 1st Half"): "asian_quarter_line_splits_the_stake",
    (Sport.SOCCER, "Asian Handicap"): "asian_quarter_line_splits_the_stake",
    (Sport.SOCCER, "Asian Handicap - 1st Half"): "asian_quarter_line_splits_the_stake",
    (Sport.SOCCER, "Half Time"): "half_only_market",
    (Sport.SOCCER, "2nd Half"): "half_only_market",
    (Sport.SOCCER, "Double Chance"): "combined_outcome_market",
    (Sport.SOCCER, "Draw No Bet"): "draw_voids_market",
    (Sport.SOCCER, "Both Teams To Score"): "yes_no_market",
    (Sport.TENNIS, "Set Handicap"): "alternate_scoring_unit",
    (Sport.TENNIS, "Game Handicap"): "alternate_scoring_unit",
    (Sport.TENNIS, "Total Games"): "alternate_scoring_unit",
    (Sport.TENNIS, "Total Sets"): "alternate_scoring_unit",
    (Sport.TENNIS, "Set Betting"): "correct_score_market",
    (Sport.BASEBALL, "Draw No Bet - First 5 Innings"): "draw_voids_market",
    (Sport.BASEBALL, "Lead After 5 Innings"): "not_a_full_period_result",
    (Sport.BASEBALL, "Total Runs Odd/Even"): "odd_even_market",
}

#: Label fragments that identify an out-of-scope contract when no exact rule
#: matched.  Applied **only after** the exact lookups, so a legitimate label
#: that happens to contain one of these words (every hockey full-game label
#: contains "penalty shootout") is unaffected.  These classify a skip; they
#: never select a market.
OUT_OF_SCOPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("must start", "pitcher_conditional_market"),
    ("to qualify", "qualification_market"),
    ("extra time", "extra_time_market"),
    ("after penalties", "penalty_shootout_market"),
    ("by the player", "player_prop"),
    ("the player", "player_prop"),
    ("settled using opta data", "opta_derived_market"),
)

#: Kambi outcome type -> canonical selection, for outcomes that are not a
#: competitor.  ``OT_CROSS`` is the draw.
OUTCOME_TYPES: dict[str, Selection] = {
    "OT_OVER": Selection.OVER,
    "OT_UNDER": Selection.UNDER,
    "OT_CROSS": Selection.DRAW,
}

#: Outcome types that name a competitor.  ``OT_UNTYPED`` appears on soccer
#: handicaps.  Which competitor is read from ``participant``, never from the
#: type: a three-way market labels its outcomes ``"1"``/``"X"``/``"2"``.
COMPETITOR_OUTCOME_TYPES: frozenset[str] = frozenset(
    {"OT_ONE", "OT_TWO", "OT_UNTYPED"}
)

#: A line more than this many times the league's plausible full-game total is a
#: units error, not a deep alternate — the thousandths trap is a factor of 1000,
#: while the deepest alternate any book quotes is within a factor of a few.
#: Deliberately loose: the strict per-league bounds are validation's job, and
#: rejecting a legitimate alternate here would look like a format change.
LINE_UNITS_ERROR_FACTOR = 10.0

#: How far the feed's own American price may sit from its own decimal price and
#: still be used verbatim, measured on the **net payout** so the tolerance means
#: the same thing for a -5000 favourite as for a +2400 longshot.
#:
#: Kept deliberately equal to :data:`src.validation.AMERICAN_PAYOUT_TOLERANCE`
#: and computed the same way, because a row that fails that check is reported as
#: a corrupt price.  BetRivers rounds its American value to "nice" numbers — 1520
#: thousandths (1.520 decimal, arithmetically -192) is quoted as -195 — and 45 of
#: 510 captured MLB outcomes drift past 1% that way, so preferring the feed
#: unconditionally would emit 45 rows that validation calls broken.  Beyond this
#: tolerance the derived value is used and the discard is counted.
AMERICAN_PAYOUT_AGREEMENT = 0.01


# ── endpoint labels ──────────────────────────────────────────────────────────

_LISTVIEW_KIND = "listview"
_BETOFFER_KIND = "betoffer"

#: Endpoint labels written by the MLB-only version of this adapter, which
#: carried no league.  Kept readable so captures from before the multi-sport
#: rewrite still replay: a parser upgrade must not orphan stored raw data.
LEGACY_ENDPOINTS: dict[str, tuple[str, str]] = {
    "listview": (_LISTVIEW_KIND, "baseball/mlb"),
}
LEGACY_BETOFFER_PREFIX = "betoffer-batch-"


def listview_endpoint(path: str, operator: str = DEFAULT_OPERATOR) -> str:
    """Endpoint label for one tenant's slate response for one league.

    The operator is in the label because a Kambi payload is only interpretable
    against the tenant that served it: ``rsiusil`` sends ``"CIN Reds"`` where
    ``leo`` sends ``"Cincinnati Reds"``, and the two price differently.  Without
    it a stored capture cannot say which book it came from except by the
    directory it happens to be filed in.
    """
    return f"{_LISTVIEW_KIND}:{operator}:{path}"


def betoffer_endpoint(path: str, index: int, operator: str = DEFAULT_OPERATOR) -> str:
    """Endpoint label for one batch of a tenant's per-event market responses.

    The index is a **position counter within one run**, not an identity: the
    number of batches changes with the slate size, so ``batch-03`` in one run
    covers different events than ``batch-03`` in the next.  Nothing downstream
    derives identity from it — :func:`parse_kambi` deduplicates on the event ids
    a response actually carries.
    """
    return f"{_BETOFFER_KIND}:{operator}:{path}:batch-{index:02d}"


def _resolve_endpoint(endpoint: str) -> tuple[str, str]:
    """``(kind, sport path)`` for a stored endpoint label.

    Reads the path **positionally from the end** — last segment for a listView,
    second-to-last for a betoffer batch — which is what makes the operator
    segment optional.  Captures written before tenants existed carry
    ``listview:baseball/mlb``; captures written now carry
    ``listview:rsiusil:baseball/mlb``; both resolve to the same league, because a
    parser upgrade must not orphan last month's raw data.
    """
    legacy = LEGACY_ENDPOINTS.get(endpoint)
    if legacy is not None:
        return legacy
    if endpoint.startswith(LEGACY_BETOFFER_PREFIX):
        return _BETOFFER_KIND, "baseball/mlb"
    parts = endpoint.split(":")
    kind = parts[0]
    if kind == _LISTVIEW_KIND and len(parts) >= 2:
        return kind, parts[-1]
    if kind == _BETOFFER_KIND and len(parts) >= 3:
        return kind, parts[-2]
    raise FormatChangeError(
        f"{SOURCE_KEY}: cannot tell which league the stored endpoint "
        f"{endpoint!r} belongs to"
    )


# ── the adapter ──────────────────────────────────────────────────────────────


class BetRiversKambiAdapter:
    """Collects one Kambi tenant's pregame game markets from the offering API.

    One class, many books.  Kambi is a *platform*: BetRivers, LeoVegas, Unibet
    and a long tail of others all serve the same payload shape from the same CDN
    under different operator tokens, so one adapter parameterized by operator is
    the whole of what it takes to add another.

    That is only safe because most of those tokens are **licences of one book**
    rather than different books.  ``rsiusil``, ``rsiusnj``, ``rsiuspa`` and the
    plain ``kambi`` reference tenant answer with the same fixtures at
    byte-identical prices, verified 30 of 30 shared moneylines; counting them as
    separate sources would let the arbitrage engine pair BetRivers against
    itself, which passes ``require_distinct_sources`` while being one
    counterparty.  Every instance registered here must therefore clear the
    distinctness gate in :mod:`src.distinctness` first.  ``leo`` (LeoVegas) does:
    it prices the same slate differently.
    """

    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        operator: str = DEFAULT_OPERATOR,
        base_url: str = DEFAULT_BASE_URL,
        market: str = DEFAULT_MARKET,
        timeout: float = 20.0,
        batch_size: int | None = None,
        client: httpx.Client | None = None,
        *,
        source_key: str = SOURCE_KEY,
        lang: str = DEFAULT_LANG,
        proxy_state: str | None = None,
    ) -> None:
        """:param leagues: Kambi sport paths (``"baseball/mlb"``) or canonical
        league keys (``"MLB"``) to collect.  Defaults to
        :data:`DEFAULT_PATHS`.
        :param batch_size: overrides every :attr:`SportPath.batch_size`.  Only
            useful for capturing a small, readable fixture; the per-sport
            defaults are what a real run should use.
        :param source_key: the identity every row this instance produces will
            carry.  Two instances **must** differ here: ``source`` is the first
            element of ``dedup_key``, so two tenants sharing a key collide on the
            storage layer's UNIQUE constraint and abort the whole run's insert.
        """
        self.operator = operator
        self.base_url = base_url.rstrip("/")
        self.market = market
        self.lang = lang
        self.batch_size = batch_size
        self._source_key = source_key
        self._paths = _resolve_requested_paths(
            DEFAULT_PATHS if leagues is None else leagues
        )
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, proxy_state=proxy_state
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        """Canonical league keys this instance is configured to collect."""
        seen: dict[str, None] = {}
        for path in self._paths:
            seen.setdefault(SPORT_PATHS[path].league, None)
        return tuple(seen)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Markets this instance claims to price, per league, at the full-game window.

        Derived from :data:`CRITERIA`, so it cannot drift from what the parser
        actually maps.  Tennis claims the moneyline alone — the set and game
        handicaps Kambi also publishes are a different scoring unit and are
        counted out of scope, not collected badly.

        *tier* changes nothing: Kambi's ``betoffer`` calls are batched by league
        rather than issued per event, so the core pass already carries the whole
        market set.
        """
        del tier
        return capabilities_from(
            {
                SPORT_PATHS[path].league: _full_game_markets(SPORT_PATHS[path].sport)
                for path in self._paths
            },
            self.leagues,
        )

    @property
    def paths(self) -> tuple[str, ...]:
        """The Kambi group paths this instance requests, in request order."""
        return self._paths

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Slate plus batched market sets for every configured league.

        *tier* is accepted and ignored.  Kambi's second call is batched by league
        — eight event ids per request — not issued per event, so there is no
        per-event hop to defer and a core pass collects the same rows a full one
        does.  Saying so is the point: a source that quietly returned less under
        ``--tier core`` would make the two tiers incomparable.
        """
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for path in self._paths:
            entry = SPORT_PATHS[path]
            tally.requested(path)
            try:
                collected = self._fetch_path(entry)
            except SourceError as exc:
                # One league failing must not discard the leagues already
                # fetched.  It used to: a refusal on the sixth path propagated
                # out of ``fetch_raw``, and the collector writes nothing to the
                # raw store on that path — so five leagues' captures were thrown
                # away unparsed over one league's bad minute.
                log.warning("%s: %s failed: %s", self._source_key, path, exc)
                tally.failed(path, exc)
                continue
            raws.extend(collected)
            # An empty league is normal — an off day, or a slate that is futures
            # containers only (the NBA in July).  It is only a fault if *every*
            # configured league is like that, which is what the tally decides.
            tally.produced(path, max(len(collected) - 1, 0))
        tally.require_something(what="pregame event")
        return raws

    def _fetch_path(self, entry: SportPath) -> list[RawResponse]:
        """One league's slate, plus its batched market sets."""
        listview = self._get(
            f"/{self.operator}/listView/{entry.path}.json",
            listview_endpoint(entry.path, self.operator),
        )
        payload = require_mapping(
            listview.json(), source=self.source_key, endpoint=listview.endpoint
        )
        require_keys(
            payload, ("events",), source=self.source_key, endpoint=listview.endpoint
        )
        event_ids = _pregame_event_ids(payload.get("events") or [])
        if not event_ids:
            log.info("%s: %s has no pregame events", self.source_key, entry.path)
            return [listview]
        return [listview, *self._fetch_betoffers(entry, event_ids)]

    def _fetch_betoffers(
        self, entry: SportPath, event_ids: Sequence[str]
    ) -> list[RawResponse]:
        """Fetch a league's market set, halving a batch that came back truncated.

        The offering API caps a response at :data:`MAX_BETOFFERS_PER_RESPONSE`
        offers and reports the real count in ``range.total``.  Storing a
        truncated payload would lose whole markets silently — a fixture's
        moneyline can fall off the end while its spreads survive — so the
        truncated capture is discarded and the batch re-requested in halves.
        """
        raws: list[RawResponse] = []
        size = self.batch_size or entry.batch_size
        queue: list[Sequence[str]] = list(_batched(event_ids, size))
        index = 0
        while queue:
            batch = queue.pop(0)
            index += 1
            raw = self._get(
                f"/{self.operator}/betoffer/event/{','.join(batch)}.json",
                betoffer_endpoint(entry.path, index, self.operator),
                extra_params={"event_ids": ",".join(batch)},
            )
            payload = require_mapping(
                raw.json(), source=self.source_key, endpoint=raw.endpoint
            )
            # Halved when the venue says the response was cut short, or when it
            # is at the cap *and* short of events.  A missing event on a small
            # response is an event that closed, not a truncation, and re-asking
            # for it in halves spends requests on a slate that has not changed.
            capped = len(payload.get("betOffers") or []) >= MAX_BETOFFERS_PER_RESPONSE
            truncated = _truncated_count(payload) is not None or (
                capped and _missing_events(payload, batch)
            )
            if truncated and len(batch) > 1:
                middle = len(batch) // 2
                queue[:0] = [batch[:middle], batch[middle:]]
                index -= 1
                continue
            raws.append(raw)
        return raws

    def _get(
        self, path: str, endpoint: str, extra_params: dict[str, str] | None = None
    ) -> RawResponse:
        params = {"lang": self.lang, "market": self.market}
        return self._http.get(
            f"{self.base_url}{path}",
            endpoint=endpoint,
            params=params,
            record_params={**params, **(extra_params or {})},
        )

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_kambi(raws)

    def close(self) -> None:
        self._http.close()


def _resolve_requested_paths(requested: Sequence[str]) -> tuple[str, ...]:
    """Accept either Kambi paths or canonical league keys, preserving order."""
    by_league: dict[str, list[str]] = {}
    for entry in SPORT_PATHS.values():
        by_league.setdefault(entry.league, []).append(entry.path)

    resolved: dict[str, None] = {}
    for item in requested:
        if item in SPORT_PATHS:
            resolved.setdefault(item, None)
            continue
        paths = by_league.get(item)
        if not paths:
            raise KeyError(
                f"unknown BetRivers/Kambi league or path {item!r}; known paths: "
                f"{sorted(SPORT_PATHS)}; known league keys: {sorted(by_league)}"
            )
        for path in paths:
            resolved.setdefault(path, None)
    if not resolved:
        raise ValueError("BetRiversKambiAdapter needs at least one league to collect")
    return tuple(resolved)


def _batched(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), max(size, 1)):
        yield items[start : start + max(size, 1)]


def _pregame_event_ids(events: Iterable[Any]) -> list[str]:
    """The ids worth asking for markets on.

    Filtering here rather than fetching everything is the polite version of the
    same result: an in-play tennis slate is half started, and the July NBA slate
    is entirely futures containers, and neither produces a collectable row.  The
    parser applies the same two tests independently and counts what it drops, so
    this filter cannot change what a stored capture parses to.
    """
    return [
        str(event["id"])
        for event in _listview_events(events)
        if event.get("id") is not None and _is_pregame_fixture(event)
    ]


def _listview_events(events: Iterable[Any]) -> Iterator[dict[str, Any]]:
    for entry in events:
        event = (entry or {}).get("event") if isinstance(entry, dict) else None
        if isinstance(event, dict):
            yield event


def _is_pregame_fixture(event: dict[str, Any]) -> bool:
    """Is this listView entry a not-yet-started match between two competitors?

    Futures and outright containers arrive in the same slate — the July NBA
    slate is 50 of them — and are recognisable before any name resolution
    because they carry no away side: ``homeName`` is
    ``"Eastern Conference Winner 2026/2027"`` and ``awayName`` is absent.
    """
    return (
        str(event.get("state") or "") == PREGAME_STATE
        and bool(str(event.get("homeName") or "").strip())
        and bool(str(event.get("awayName") or "").strip())
    )


def _truncated_count(payload: dict[str, Any]) -> int | None:
    """The real offer count when a betoffer response was capped, else ``None``.

    Kambi says so itself, and only when it matters: ``range`` is **absent from
    every response that fits** and present on exactly the ones that do not,
    carrying the true total.  From one live capture, 4 of 102 responses:

    ===================================  ======  ==========================
    endpoint                             offers  range
    ===================================  ======  ==========================
    ``betoffer:rsiusil:baseball/mlb:01``   2000   ``{size: 2000, total: 2752}``
    ``betoffer:rsiusil:baseball/mlb:02``   2000   ``{size: 2000, total: 2256}``
    ===================================  ======  ==========================

    This guard was once removed on the grounds that ``range`` "appears in none of
    the captured betoffer responses" — true of the committed fixtures, whose
    largest response is 1,828 offers and therefore never truncated, and false of
    reality.  Replacing it with an event-shortfall check produced a guard that
    could not fire at all, because Kambi truncates **without dropping an event**:
    all 8 requested events came back in a response missing 752 offers.  Two MLB
    fixtures lost 77% and 90% of their priced selections on both tenants at once,
    with zero errors, zero rejections and zero skips.

    The lesson is narrow and worth keeping: a field absent from the fixtures is
    not a field the API does not send.  These fixtures could not contain it,
    because none of them is big enough to be truncated.
    """
    reported = payload.get("range")
    if not isinstance(reported, dict):
        return None
    total = reported.get("total")
    offers = payload.get("betOffers")
    if isinstance(total, int) and isinstance(offers, list) and total > len(offers):
        return total
    return None


def _missing_events(payload: dict[str, Any], requested: Sequence[str]) -> list[str]:
    """Event ids that were asked for and did not come back.

    The second truncation signal, and the weaker one: Kambi's own ``range`` is
    exact where this is inferential.  Kept because the two fail differently — a
    response can drop an event without reaching the offer cap (an event that
    closed between the listView call and this one), and one can reach the cap
    without dropping an event, which is what actually happens.
    """
    if not requested:
        return []
    returned = {
        str(event.get("id"))
        for event in (payload.get("events") or [])
        if isinstance(event, dict) and event.get("id") is not None
    }
    return [event_id for event_id in requested if str(event_id) not in returned]


# ── parsing (pure) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Fixture(Fixture):
    """One accepted pregame event, resolved onto canonical identities."""

    marker: str | None = None
    """The competition's identity marker, carried so the **priced side** is
    resolved the same way the fixture was.

    Applied to ``home``/``away`` when the fixture was built and nowhere else, it
    broke exactly the case it exists for: where a venue marks the competition
    and not the teams, the fixture keys became ``...w`` while every
    competitor-named price still resolved to the unmarked slug, matched neither
    side, and was **rejected** — one such fixture marks the whole source
    unhealthy."""


def parse_kambi(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Kambi responses into normalized rows.

    Pure: no network, no clock, no filesystem.  Two deduplication passes make it
    safe to hand this a whole raw directory containing several runs, which is
    what replay does.  Without them, two runs of the same league produced two
    copies of every row, and every one of those copies collided on
    ``dedup_key`` — which the storage layer enforces, so the *entire* run's
    insert aborted.

    The first pass keeps the newest response per endpoint label.  The second
    keeps, for each league, the newest response that carried a given event id:
    the batch index in an endpoint label is a position counter that shifts when
    the slate size changes, so ``batch-01`` from yesterday and ``batch-01`` from
    today are different event sets and label-level deduplication alone is not
    enough.

    Which *book* the rows belong to comes from the envelope, not from this
    object: two Kambi tenants share this parser, and ``source`` is the first
    element of ``dedup_key``, so reading it off an instance would make replay —
    which constructs the adapter with no arguments — file LeoVegas's prices under
    BetRivers and collide the two on insert.
    """
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    listviews, betoffers = _select_responses(raws)
    if not listviews:
        raise FormatChangeError(
            f"{source}: replay has no listView response, so no slate can be resolved"
        )

    fixtures: dict[str, _Fixture] = {}
    for path, raw in listviews.items():
        entry = SPORT_PATHS.get(path)
        if entry is None:
            raise FormatChangeError(
                f"{source}: stored response for unknown sport path {path!r}"
            )
        fixtures.update(_accepted_fixtures(raw, entry, source, outcome))

    if not fixtures:
        return outcome

    event_keys = resolve_doubleheaders(
        {
            event_id: (fixture.base_key, fixture.commence_time)
            for event_id, fixture in fixtures.items()
        }
    )

    for path, responses in betoffers.items():
        if path not in listviews:
            raise FormatChangeError(
                f"{source}: replay has betoffer responses for {path!r} but no "
                "listView to resolve its slate against"
            )
        for raw, owned in responses:
            _parse_betoffer_response(raw, owned, fixtures, event_keys, source, outcome)

    drop_duplicate_selections(source, outcome)
    return outcome


def _select_responses(
    raws: Sequence[RawResponse],
) -> tuple[dict[str, RawResponse], dict[str, list[tuple[RawResponse, frozenset[str]]]]]:
    """Pick the responses that describe the most recent state of each league.

    Returns the newest listView per league, and per league a list of
    ``(response, event ids this response is authoritative for)``.  Assigning
    each event to exactly one response is what makes replaying a directory of
    several runs produce one row per selection instead of one per run.
    """
    latest_by_endpoint: dict[str, RawResponse] = {}
    for raw in raws:
        _resolve_endpoint(raw.endpoint)  # rejects labels that name no league
        current = latest_by_endpoint.get(raw.endpoint)
        if current is None or response_order(raw) > response_order(current):
            latest_by_endpoint[raw.endpoint] = raw

    listviews: dict[str, RawResponse] = {}
    per_path: dict[str, list[RawResponse]] = {}
    for raw in latest_by_endpoint.values():
        kind, path = _resolve_endpoint(raw.endpoint)
        if kind == _LISTVIEW_KIND:
            current = listviews.get(path)
            if current is None or response_order(raw) > response_order(current):
                listviews[path] = raw
        else:
            per_path.setdefault(path, []).append(raw)

    betoffers: dict[str, list[tuple[RawResponse, frozenset[str]]]] = {}
    for path, responses in per_path.items():
        claimed: set[str] = set()
        owned_by: dict[str, frozenset[str]] = {}
        # Newest first, so the newest response wins every event it carries and
        # an older one contributes only the events no newer response covered.
        for raw in sorted(responses, key=response_order, reverse=True):
            owned = _event_ids_in(raw) - claimed
            if not owned:
                continue
            claimed |= owned
            owned_by[raw.endpoint] = owned
        betoffers[path] = [
            (raw, owned_by[raw.endpoint])
            for raw in sorted(responses, key=response_order)
            if raw.endpoint in owned_by
        ]
    return listviews, betoffers


def _event_ids_in(raw: RawResponse) -> frozenset[str]:
    try:
        payload = raw.json()
    except ValueError:
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    return frozenset(
        str(offer.get("eventId"))
        for offer in payload.get("betOffers") or []
        if isinstance(offer, dict) and offer.get("eventId") is not None
    )


def _accepted_fixtures(
    raw: RawResponse, entry: SportPath, source: str, outcome: ParseOutcome
) -> dict[str, _Fixture]:
    fixtures: dict[str, _Fixture] = {}
    payload = raw.json()
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise FormatChangeError(
            f"{source}:{raw.endpoint}: listView payload has no 'events' array"
        )

    for item in events:
        event = (item or {}).get("event") if isinstance(item, dict) else None
        if not isinstance(event, dict) or event.get("id") is None:
            outcome.skipped["event_without_id"] += 1
            continue
        event_id = str(event["id"])

        if not (str(event.get("homeName") or "").strip() and str(event.get("awayName") or "").strip()):
            # No away side: an outright/futures container, not a fixture.
            outcome.skipped["outright_or_futures_container"] += 1
            continue
        if str(event.get("state") or "") != PREGAME_STATE:
            outcome.skipped["event_not_pregame"] += 1
            continue

        competition = get_league(_league_key_for(event, entry))
        # A women's, reserve or youth competition whose *name* carries the
        # marker while the team names do not.  Unrecognised soccer competitions
        # fall back to one catch-all league here, so the venue's own name for
        # the competition is then read by nothing — and ``football/brazil`` is a
        # default path, where the women's tier is called Campeonato Brasileiro
        # Feminino.  Without this, a women's fixture here and the men's fixture
        # of the same two clubs at another venue produce a byte-identical event
        # key and are priced as one market.
        marker = competition_marker(_competition_name(event), competition.sport)
        home_raw, away_raw = event.get("homeName"), event.get("awayName")
        home_raw = with_marker(home_raw, marker)
        away_raw = with_marker(away_raw, marker)
        # A doubles entry names two players a side ("R Galloway / E King"), so
        # it is deliberately unresolvable — but it is a market this collector
        # does not cover, not a participant it failed to recognise.  Both
        # tenants collect ATP, WTA and Challenger, and one rejection marks the
        # whole source failed: on a full slate that turns 1,107 good rows across
        # six other sports into a source graded broken.  Pinnacle records the
        # same thing happening to it — "failed the entire Pinnacle run over one
        # doubles match … discarding 2300 good rows across five other sports" —
        # and every other adapter but these two already carried this guard.
        if any(is_pairing(name, competition.sport) for name in (home_raw, away_raw)):
            outcome.skipped["doubles_or_team_pairing"] += 1
            continue
        # A statistic dressed as a fixture, for the same reason.
        if any(is_statistic(name) for name in (home_raw, away_raw)):
            outcome.skipped["statistic_not_a_fixture"] += 1
            continue

        first = canonical_participant(home_raw, competition)
        second = canonical_participant(away_raw, competition)
        if first is None or second is None or first.key == second.key:
            outcome.reject(
                source,
                "unknown_participant",
                f"event {event_id} ({event.get('englishName')!r}) in {competition.key} "
                f"did not resolve to two competitors: home={event.get('homeName')!r} "
                f"away={event.get('awayName')!r}",
                event_id=event_id,
                league=competition.key,
            )
            continue

        commence_time = parse_iso_time(event.get("start"))
        if commence_time is None:
            outcome.reject(
                source,
                "missing_commence_time",
                f"event {event_id} has unparseable start {event.get('start')!r}",
                event_id=event_id,
            )
            continue

        # Tennis has no home player: each book orders the two names as it likes,
        # so trusting Kambi's would mislabel which player every price refers to.
        away, home = orient(
            first,
            second,
            competition,
            home=first if competition.has_home_away else None,
        )
        fixtures[event_id] = _Fixture(
            event_id=event_id,
            sport=entry.sport,
            competition=competition,
            home=home,
            away=away,
            commence_time=commence_time,
            base_key=build_event_key(away.key, home.key, commence_time, competition),
            marker=marker,
        )
    return fixtures


def _competition_name(event: Mapping[str, Any]) -> str:
    """The venue's own label for the competition this event sits in.

    The deepest step of the event's path, which is where Kambi puts the
    competition — ``englandwomenschampionship``, ``brazilcampeonatofeminino``.
    Read as text and not mapped, because the point is the marker it carries and
    not which registered league it is.
    """
    for step in reversed(event.get("path") or []):
        if isinstance(step, dict):
            name = str(step.get("name") or step.get("englishName") or "")
            if name:
                return name
    return str(event.get("group") or "")


def _league_key_for(event: dict[str, Any], entry: SportPath) -> str:
    """Canonical league key for one event.

    Soccer is resolved from the event's own path because a country group mixes
    competitions: ``football/brazil`` carries both Brasileirão tiers, and only
    one of them is a registered league.  An unrecognised competition falls back
    to ``SOCCER_OTHER`` rather than being rejected — books disagree about
    classification, and league is deliberately not part of event identity.
    """
    if entry.sport is not Sport.SOCCER:
        return entry.league
    for step in reversed(event.get("path") or []):
        key = SOCCER_COMPETITIONS.get(str((step or {}).get("termKey") or ""))
        if key is not None:
            return key
    if league_is_known(entry.league) and get_league(entry.league).sport is Sport.SOCCER:
        return entry.league
    return SOCCER_FALLBACK_LEAGUE


def _parse_betoffer_response(
    raw: RawResponse,
    owned_event_ids: frozenset[str],
    fixtures: dict[str, _Fixture],
    event_keys: dict[str, str],
    source: str,
    outcome: ParseOutcome,
) -> None:
    payload = raw.json()
    if not isinstance(payload, dict):
        raise FormatChangeError(
            f"{source}:{raw.endpoint}: expected a JSON object in a betoffer response"
        )
    # Asked for on the envelope, so a replay checks the same thing the live run
    # did without needing the batch that produced it.
    asked = [
        part
        for part in str((raw.request_params or {}).get("event_ids", "")).split(",")
        if part
    ]
    missing = _missing_events(payload, asked)
    offers = len(payload.get("betOffers") or [])
    shortfall = _truncated_count(payload)
    if shortfall is not None:
        # The venue stated the shortfall, so there is nothing to infer.
        outcome.reject(
            source,
            "betoffer_response_truncated",
            f"{raw.endpoint} carries {offers} of {shortfall} offers — the response "
            f"hit the {MAX_BETOFFERS_PER_RESPONSE}-offer cap and the rest were "
            "dropped, so markets are missing from this batch",
            endpoint=raw.endpoint,
        )
    elif missing and offers >= MAX_BETOFFERS_PER_RESPONSE:
        # Both signals together, and only together.
        #
        # A missing event on its own is ordinary: one that closed or started
        # between the listView call and the betoffer call, or that has no open
        # offer, simply is not echoed.  Graded a rejection it took the whole
        # tenant down — dropping one event from a real capture put 3,455 good
        # rows behind ``ok=False`` — which is the treatment this module
        # deliberately does not give a doubles entry or a statistic container.
        #
        # A response at the offer cap *and* short of events is the truncation
        # this guard exists for, and the two together cannot be produced by an
        # event quietly closing.
        outcome.reject(
            source,
            "betoffer_response_truncated",
            f"{raw.endpoint} carries {offers} offers — at or past the "
            f"{MAX_BETOFFERS_PER_RESPONSE}-offer cap — and returned "
            f"{len(asked) - len(missing)} of {len(asked)} requested event(s), "
            f"missing {', '.join(missing[:5])}: markets are being dropped off the end",
            endpoint=raw.endpoint,
        )
    elif missing:
        # Counted, so a feed that quietly stops echoing events is still visible.
        outcome.skipped["event_absent_from_betoffer_response"] += len(missing)

    for offer in payload.get("betOffers") or []:
        if not isinstance(offer, dict):
            outcome.skipped["betoffer_not_an_object"] += 1
            continue
        event_id = str(offer.get("eventId", ""))
        if event_id not in owned_event_ids:
            # A newer response already carried this event's markets.
            outcome.skipped["betoffer_superseded_by_newer_response"] += 1
            continue
        fixture = fixtures.get(event_id)
        if fixture is None:
            outcome.skipped["betoffer_for_uncollected_event"] += 1
            continue

        label = str((offer.get("criterion") or {}).get("englishLabel") or "")
        resolved = _resolve_criterion(label, fixture)
        if isinstance(resolved, str):
            outcome.skipped[resolved] += 1
            continue
        rule, side = resolved

        # ``closed`` is the betting *cutoff timestamp*, not a boolean flag.
        # Reading it as truthy marks every normal pre-match offer suspended.
        closes_at = parse_iso_time(offer.get("closed"))
        tags = offer.get("tags") or []
        # Exactly one offer per (event, criterion) group carries MAIN_LINE, and
        # only line markets carry it at all.
        is_alternate = rule.market in MARKETS_REQUIRING_LINE and (
            MAIN_LINE_TAG not in tags
        )
        offer_id = str(offer.get("id", ""))

        for raw_outcome in offer.get("outcomes") or []:
            if not isinstance(raw_outcome, dict):
                outcome.skipped["outcome_not_an_object"] += 1
                continue
            quote = _build_quote(
                raw=raw,
                source=source,
                raw_outcome=raw_outcome,
                rule=rule,
                side=side,
                is_alternate=is_alternate,
                closes_at=closes_at,
                offer_id=offer_id,
                fixture=fixture,
                event_key=event_keys[event_id],
                label=label,
                outcome=outcome,
            )
            if quote is not None:
                outcome.quotes.append(quote)


def _resolve_criterion(
    label: str, fixture: _Fixture
) -> tuple[MarketRule, Side | None] | str:
    """Map an exact criterion label, or return the reason it is out of scope.

    Exact lookups run first so that a collected label is never reinterpreted by
    the fragment scan below it — every hockey full-game label contains the words
    "penalty shootout", and the baseball first-five handicap is literally
    ``"Handicap - First 5 Innings"``.
    """
    if not label:
        return "criterion_without_label"

    rule = CRITERIA.get((fixture.sport, label))
    if rule is not None:
        return rule, None

    affixes = TEAM_TOTAL_AFFIXES.get(fixture.sport)
    if affixes is not None:
        side = _team_total_side(label, affixes, fixture)
        if side is not None:
            return MarketRule(Market.TEAM_TOTAL, Period.FULL_GAME), side

    named = OUT_OF_SCOPE.get((fixture.sport, label))
    if named is not None:
        return named

    lowered = label.lower()
    for fragment, reason in OUT_OF_SCOPE_MARKERS:
        if fragment in lowered:
            return reason

    # Stable, bounded reason: the labels themselves are unbounded (they embed
    # player and club names), so keying the counter on them would produce
    # thousands of one-off entries instead of a usable coverage signal.
    return f"unmapped_criterion:{fixture.sport.value}"


def _team_total_side(
    label: str, affixes: tuple[str, str], fixture: _Fixture
) -> Side | None:
    prefix, suffix = affixes
    if not label.startswith(prefix) or not label.endswith(suffix):
        return None
    name = label[len(prefix) : len(label) - len(suffix) if suffix else None]
    # Whatever follows the club name has to be nothing at all.
    #
    # The suffix here is the empty string, so ``endswith`` is vacuously true and
    # the pair is a bare prefix test with no anchor on the right — everything
    # after "Total Runs by " went to :func:`canonical_participant`, which is
    # deliberately loose and resolves "Cincinnati Reds - First 5 Innings" to
    # MLB-CIN.  The period qualifier was absorbed into the club name and the
    # rule above returned ``Period.FULL_GAME`` regardless, so a five-inning team
    # total would be stored and compared as a nine-inning one.  Every other
    # baseball period product Kambi ships is exact-matched; this was the one
    # family where the period was inferred rather than read.
    # A trailing doubleheader marker is not a period qualifier.
    #
    # The guard exists to stop "Cincinnati Reds - First 5 Innings" being
    # absorbed into the club name, and it cannot tell that apart from
    # "White Sox - Game 2", which is how a book decorates the second game of a
    # doubleheader — so every team total on such a fixture was dropped into
    # ``unmapped_criterion:baseball``, the same bucket that holds hundreds of
    # legitimate props, and the loss was invisible.
    name = _GAME_ORDINAL.sub("", name).strip()
    if _PERIOD_QUALIFIER in name:
        return None
    who = canonical_participant(name, fixture.competition)
    if who is None:
        return None
    if who.key == fixture.home.key:
        return Side.HOME
    if who.key == fixture.away.key:
        return Side.AWAY
    return None


def _build_quote(
    *,
    raw: RawResponse,
    source: str,
    raw_outcome: dict[str, Any],
    rule: MarketRule,
    side: Side | None,
    is_alternate: bool,
    closes_at: datetime | None,
    offer_id: str,
    fixture: _Fixture,
    event_key: str,
    label: str,
    outcome: ParseOutcome,
) -> Quote | None:
    selection = _selection(raw_outcome, fixture)
    if selection is None:
        outcome.reject(
            source,
            "unknown_selection",
            f"outcome type {raw_outcome.get('type')!r} / participant "
            f"{raw_outcome.get('participant')!r} on {label!r} for event "
            f"{fixture.event_id}",
            offer_id=offer_id,
        )
        return None

    kambi_odds = raw_outcome.get("odds")
    if kambi_odds is None:
        # A suspended leg of an otherwise live market carries no price.  Normal,
        # and not something this parser can invent a value for.
        outcome.skipped["outcome_without_odds"] += 1
        return None

    kambi_line = raw_outcome.get("line")
    if rule.market in MARKETS_REQUIRING_LINE and kambi_line is None:
        outcome.reject(
            source,
            "missing_line",
            f"{rule.market} outcome without a line on {label!r} for event "
            f"{fixture.event_id}",
            offer_id=offer_id,
        )
        return None
    if rule.market in MARKETS_REQUIRING_SIDE and side is None:
        outcome.reject(
            source,
            "missing_side",
            f"{rule.market} without a resolvable side on {label!r} for event "
            f"{fixture.event_id}",
            offer_id=offer_id,
        )
        return None

    # Suspended means the source says so, or the offer's cutoff had already
    # passed when we observed it — never merely that a cutoff exists.
    suspended = str(raw_outcome.get("status") or "OPEN").upper() != "OPEN" or (
        closes_at is not None and closes_at <= raw.fetched_at
    )

    try:
        decimal_odds = float(kambi_odds) / THOUSANDTHS
        line = None
        if rule.market in MARKETS_REQUIRING_LINE:
            # Lines are in thousandths too: 8000 is a total of 8.0, not 8000
            # runs.  ``+ 0.0`` normalizes -0.0 to 0.0 so the two sides of a
            # pick'em handicap format identically in ``dedup_key``.
            line = float(kambi_line) / THOUSANDTHS + 0.0
    except (TypeError, ValueError) as exc:
        outcome.reject(
            source,
            "unreadable_price",
            f"{rule.market}/{selection} on event {fixture.event_id}: {exc}",
            offer_id=offer_id,
        )
        return None

    if not is_plausible_decimal_odds(decimal_odds):
        outcome.reject(
            source,
            "implausible_odds",
            f"{kambi_odds!r} thousandths is {decimal_odds} decimal on {label!r} "
            f"for event {fixture.event_id} — no book publishes that price",
            offer_id=offer_id,
        )
        return None
    if line is not None and not _line_units_look_right(line, fixture.competition):
        outcome.reject(
            source,
            "line_units_error",
            f"line {line} on {label!r} for event {fixture.event_id} is far outside "
            f"{fixture.competition.key}'s plausible range "
            f"{fixture.competition.plausible_total_range} — check the thousandths divisor",
            offer_id=offer_id,
        )
        return None

    american = _american_odds(raw_outcome, decimal_odds, outcome)
    try:
        return priced_quote(
            fixture,
            source=source,
            raw=raw,
            event_key=event_key,
            market=rule.market,
            period=rule.period,
            selection=selection,
            side=side,
            line=line,
            is_alternate=is_alternate,
            decimal_odds=decimal_odds,
            american_odds=american,
            implied_probability=implied_probability(decimal_odds),
            source_market_id=offer_id,
            source_selection_id=_optional_str(raw_outcome.get("id")),
            status=QuoteStatus.SUSPENDED if suspended else QuoteStatus.ACTIVE,
            last_change_at=parse_iso_time(raw_outcome.get("changedDate")),
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            source,
            "invalid_quote",
            f"{rule.market}/{rule.period}/{selection} on event {fixture.event_id} "
            f"from {label!r}: {exc}",
            offer_id=offer_id,
        )
        return None


def _american_odds(
    raw_outcome: dict[str, Any], decimal_odds: float, outcome: ParseOutcome
) -> int:
    """The feed's own American price, when it agrees with the decimal one.

    Kambi publishes both formats, and the row is required to carry both, so the
    feed's value is preferred: deriving one from the other would make the
    cross-format check downstream a tautology.  The feed rounds its American
    value its own way (``1120`` thousandths is quoted as ``-835``, not the
    arithmetic ``-833``), which is honest rounding; a disagreement wider than
    that is a corrupt field, and then the derived value is used instead so the
    row still carries a price at all.
    """
    derived = decimal_to_american(decimal_odds)
    stated = raw_outcome.get("oddsAmerican")
    if stated is None:
        return derived
    try:
        value = int(str(stated).strip().replace("+", ""))
        payout = american_to_decimal(value) - 1.0
    except (TypeError, ValueError):
        outcome.repaired["feed_american_odds_unreadable"] += 1
        return derived
    if (
        abs(payout - (decimal_odds - 1.0)) / (decimal_odds - 1.0)
        > AMERICAN_PAYOUT_AGREEMENT
    ):
        outcome.repaired["feed_american_odds_disagreed_with_decimal"] += 1
        return derived
    return value


def _line_units_look_right(line: float, competition: League) -> bool:
    ceiling = competition.plausible_total_range[1] * LINE_UNITS_ERROR_FACTOR
    return abs(line) <= ceiling


def _selection(raw_outcome: dict[str, Any], fixture: _Fixture) -> Selection | None:
    """Resolve a Kambi outcome to a canonical selection.

    Competitor sides come from ``participant`` matched against this fixture's
    resolved identities, never from ``OT_ONE``/``OT_TWO`` ordering: a three-way
    market labels its outcomes ``"1"``/``"X"``/``"2"``, and for tennis the
    home/away assignment is this pipeline's own deterministic ordering rather
    than the book's, so position carries no meaning at all.
    """
    outcome_type = str(raw_outcome.get("type") or "")
    mapped = OUTCOME_TYPES.get(outcome_type)
    if mapped is not None:
        return mapped
    if outcome_type not in COMPETITOR_OUTCOME_TYPES:
        return None

    # ``participant`` first, and only the *English* label as a fallback: the
    # localized ``label`` is a different string in another locale, and an
    # open-roster resolution of it could land on a slug that is not this
    # fixture's competitor at all.
    # Resolved through the **same marker the fixture was built with**.
    #
    # The competition marker was applied to ``homeName``/``awayName`` and
    # nowhere else, so in the only case it exists for — the venue marks the
    # competition and not the teams — the fixture's keys carried the marker and
    # every competitor-named price did not.  Neither side matched, and the row
    # became an ``unknown_selection`` rejection, which marks the whole source
    # unhealthy over one fixture.
    for field in ("participant", "englishLabel"):
        who = canonical_participant(
            with_marker(raw_outcome.get(field), fixture.marker), fixture.competition
        )
        if who is None:
            continue
        if who.key == fixture.home.key:
            return Selection.HOME
        if who.key == fixture.away.key:
            return Selection.AWAY
    return None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
