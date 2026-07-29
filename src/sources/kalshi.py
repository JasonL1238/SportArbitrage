"""Kalshi: a regulated exchange where a bet is a **contract**, not a price.

``api.elections.kalshi.com/trade-api/v2`` is fully open — no key, no account, no
geo gate from here — and its sports series are the cleanest structured data of
any source in this pipeline.  Every fact this adapter needs is a field: the
strike is a number, the team is a ticker segment, the start is a timestamp.  No
title is ever parsed, which is the whole reason the deleted
``market_decomposer`` — a *confidence-scored* hypothesis gated at ``>= 0.7`` — is
not being revived.

A contract is a different instrument from a posted price, and three consequences
are structural here:

**You buy at the ask, and only at the ask.**  ``yes_bid`` is what somebody will
pay *you*; taking it is selling, which is not the bet being modelled.  Every row
here is a purchase at ``yes_ask_dollars`` or ``no_ask_dollars``, and a market
with no ask has nothing takeable in it.

**The price is a probability, so the odds are its reciprocal.**  A contract
bought at $0.43 pays $1, which is decimal odds of 1/0.43 = 2.326.

**The fee is charged on entry and depends on the price.**  Kalshi's published
schedule is ``0.07 × p × (1 − p)`` per contract, paid whether or not the contract
settles your way — so it is not a rate on winnings with some equivalent number.
It lives in :mod:`src.commission`, and :mod:`src.arb` prices every leg net of it.

Field traps this parser exists to get right
-------------------------------------------

**The documented price fields are empty.**  ``yes_bid``, ``yes_ask`` and
``open_interest`` all return ``None`` on the current API; the live values are in
``yes_ask_dollars``, ``no_ask_dollars`` and ``open_interest_fp``.  Reviving the
old adapter verbatim would have produced nulls in silence.

**Who is at home is in the ticker, not in the title.**  An event ticker is
``KXMLBGAME-26JUL302210SEALAD``: a timestamp followed by the away and home team
codes, concatenated with no separator.  It is split against the league's own
roster abbreviations, and a tail that splits **two** ways is refused rather than
guessed at — "SEA|LAD" is the only reading of ``SEALAD``, and if some future tail
were ambiguous, picking one would silently swap home for away on a whole fixture.
Verified against another book's explicit home flag on the captured slate.

**A market's two sides are not always two markets.**  The moneyline lists one
contract per team and each carries only its own YES side; the total lists one
contract per line whose YES *and* NO are both takeable.  Emitting the NO of a
team contract as well would put two prices on one selection at one source, which
collides on ``dedup_key`` and aborts that source's insert.  The shape is declared
per series rather than inferred.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.leagues import US_EASTERN
from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
from src.normalize import decimal_to_american, implied_probability, is_plausible_decimal_odds
from src.participants import Participant, canonical_participant
from src.raw_store import RawResponse
from src.schema import (
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

SOURCE_KEY = "kalshi"
DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

#: Kalshi rate-limits harder than the shared default.  An unpaced probe drew
#: ``429 too_many_requests`` on its second request, so this source asks the
#: shared pacer for more room on its own host.  The retry path honours the
#: server's ``Retry-After`` when it sends one; this is what keeps it from being
#: needed.
HOST_INTERVAL = 0.6

#: Markets per page.  The API caps this; 200 covers a whole MLB series in one
#: request and the cursor loop below exists for the day it does not.
DEFAULT_PAGE_SIZE = 200

#: Bound on cursor pages per series — a runaway-paging guard, not a limit anyone
#: expects to reach.
MAX_PAGES_PER_SERIES = 10


class Shape(str):
    """How one series lays its two sides out across contracts."""

    TEAM_WINNER = "team_winner"
    """One contract per team; only its YES is that team's price."""

    TEAM_SPREAD = "team_spread"
    """One contract per (team, line); YES is that team giving the handicap and
    NO is the other team receiving it."""

    GAME_TOTAL = "game_total"
    """One contract per line; YES is over and NO is under."""


@dataclass(frozen=True)
class Series:
    """One Kalshi series this adapter knows how to read."""

    ticker: str
    sport: Sport
    league: str
    market: Market
    period: Period
    shape: str


#: The series collected, and nothing else.
#:
#: MLB only, deliberately.  The mapping from a ticker segment to a competitor
#: depends on Kalshi's codes matching the league's own abbreviations, which is
#: exactly true for MLB (``SEA``, ``LAD``, ``ATH``, ``SF``, ``NYM`` …) and is
#: **not** true elsewhere: the WNBA series uses ``NY`` and ``LV`` where the
#: roster has ``NYL`` and ``LVA``.  Adding a league here without checking its
#: codes first would turn every one of its fixtures into a rejection, which marks
#: the whole source unhealthy for a mapping that was never verified.
SERIES: tuple[Series, ...] = (
    Series("KXMLBGAME", Sport.BASEBALL, "MLB", Market.MONEYLINE, Period.FULL_GAME,
           Shape.TEAM_WINNER),
    Series("KXMLBTOTAL", Sport.BASEBALL, "MLB", Market.TOTAL, Period.FULL_GAME,
           Shape.GAME_TOTAL),
    Series("KXMLBSPREAD", Sport.BASEBALL, "MLB", Market.SPREAD, Period.FULL_GAME,
           Shape.TEAM_SPREAD),
)

SERIES_BY_TICKER: dict[str, Series] = {entry.ticker: entry for entry in SERIES}

DEFAULT_LEAGUES: tuple[str, ...] = tuple(
    dict.fromkeys(entry.league for entry in SERIES)
)

#: Kalshi ticker codes that are not the league's own abbreviation.
#:
#: Kept as an explicit, per-league table rather than as a fuzzy match, and small
#: by construction: 29 of the 30 MLB codes are already the roster's own
#: abbreviations and only Arizona differs.  A code missing from here and from the
#: roster produces a **rejection**, not a guess — the ticker's two codes are what
#: decide which side is home, so a wrong reading swaps every price on the fixture
#: rather than losing it.
#:
#: This is also why the series table below is MLB-only: the WNBA series uses
#: ``NY`` and ``LV`` where the roster has ``NYL`` and ``LVA``, and adding a
#: league before checking its codes would turn all of its fixtures into
#: rejections.
TICKER_ALIASES: dict[str, dict[str, str]] = {
    "MLB": {"AZ": "ARI"},
}


def _resolve_code(code: str | None, competition: League) -> Participant | None:
    """A ticker code as a competitor, through the alias table if it needs it."""
    if not code:
        return None
    found = canonical_participant(code, competition)
    if found is not None:
        return found
    alias = TICKER_ALIASES.get(competition.key, {}).get(code)
    return canonical_participant(alias, competition) if alias else None


#: The game part of a ticker: ``26JUL302210SEALAD`` — two-digit year, three-letter
#: month, two-digit day, four-digit **US Eastern** time, then the two team codes
#: run together.
#:
#: The timestamp is read from here and **not** from ``occurrence_datetime``,
#: which is the field it looks like it should come from and is the game's
#: expected *end*: on all 354 captured markets it equals
#: ``expected_expiration_time`` and sits exactly three hours after the ticker's
#: own time, and the market's ``rules_primary`` states the ticker's time as the
#: scheduled start ("originally scheduled for Jul 28, 2026 at 7:45 PM EDT" against
#: ticker ``…26JUL281945…``).
#:
#: Three hours is not a rounding error, it is the length of a baseball game, and
#: using it cost more than a wrong display value:
#:
#: * **Kalshi joined nothing.** Its fixtures landed three hours from every other
#:   book's, outside MLB's 90-minute clustering tolerance, so its 40 events shared
#:   an ``event_key`` with zero other sources and the distinctness gate could
#:   never reach a verdict on it.
#: * **On a doubleheader it joined the wrong game.**  Game two starts about three
#:   hours after game one — exactly the offset — so Kalshi's game *one* fell
#:   inside every other book's game *two* cluster.  That is the phantom the
#:   reconciliation pass exists to prevent, and it produced a 20% "guaranteed"
#:   position between two unrelated games.
#: * **The pregame gate was defeated for three hours**, since a contract that had
#:   been trading in-play for two hours still looked like it started later.
#: ``26JUL291310ATLNYMG1`` — stamp, optional start time, the two team codes run
#: together, and an optional ``G1``/``G2`` doubleheader ordinal.
#:
#: The ordinal is what a live slate turned up: without it the trailing ``G1``
#: was read as part of the home code, no split resolved, and the whole fixture
#: was refused as an unreadable ticker — which marks the entire source
#: unhealthy, so 556 good rows were graded broken over one doubleheader.
_GAME_PART = re.compile(
    r"^(?P<stamp>\d{2}[A-Z]{3}\d{2})(?P<time>\d{4})?"
    r"(?P<teams>[A-Z]{4,8}?)(?P<game>G[1-9])?$"
)

#: Kalshi writes its ticker timestamps in US Eastern, whatever the venue's own
#: zone: a 19:45 ET ticker is an 18:45 first pitch in St. Louis.
_TICKER_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: How long after the stated start ``occurrence_datetime`` may fall and still be
#: a believable expiry for the same fixture.  A cross-check, not a source of
#: truth: if the two ever stop being consistent the convention has changed, and
#: refusing is better than filing a fixture at a time nobody agrees on.
#:
#: Measured rather than guessed: across all 354 captured markets the gap is
#: **exactly three hours, with no variance at all**.  Twelve hours was four times
#: looser than the data and, more to the point, too loose to catch the failure
#: this check exists for.  The bound is one-sided in effect — ``commence <=
#: expiry`` catches a ticker that decodes *later* than it should — and a ticker
#: that decodes *earlier* is the dangerous direction: if Kalshi began stamping
#: venue-local time, a Pacific game would decode three hours early, its expiry
#: would land at decoded + 6h, and twelve hours would wave it through.  That is
#: the same three-hour offset that put Kalshi's game one inside every other
#: book's game two cluster and produced a 20% "guaranteed" position.
#:
#: Five hours accepts every market ever observed with two hours to spare and
#: rejects that shift with one hour to spare.
MAX_EXPIRY_AFTER_START = timedelta(hours=5)


def _ticker_start(game_id: str) -> datetime | None:
    """The scheduled start encoded in an event ticker, in UTC.

    ``26JUL302210`` is 22:10 US Eastern on 2026-07-30.  Returns ``None`` when the
    ticker carries no time at all — some series omit it — because a date without a
    time cannot place a fixture against another book's clock.
    """
    match = _GAME_PART.match(game_id)
    if match is None or not match.group("time"):
        return None
    stamp, clock = match.group("stamp"), match.group("time")
    month = _TICKER_MONTHS.get(stamp[2:5])
    if month is None:
        return None
    try:
        local = datetime(
            2000 + int(stamp[:2]),
            month,
            int(stamp[5:7]),
            int(clock[:2]),
            int(clock[2:]),
            tzinfo=US_EASTERN,
        )
    except ValueError:
        return None
    return local.astimezone(UTC)

#: A market ticker's own suffix, after the event ticker: a team code, optionally
#: followed by a line index (``STL4``), or a bare line index (``9``).
_MARKET_SUFFIX = re.compile(r"^(?P<code>[A-Z]+)?(?P<index>\d+)?$")

_ACTIVE_STATUSES = frozenset({"active"})


class KalshiAdapter:
    """Collects Kalshi's open sports contracts for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        keys: list[str] = []
        for key in leagues:
            league_registry.league(key)
            if key not in keys:
                keys.append(key)
        known = {entry.league for entry in SERIES}
        unknown = [key for key in keys if key not in known]
        if unknown:
            raise ValueError(
                f"Kalshi has no series registered for league(s) {unknown}; known: "
                f"{sorted(known)}"
            )
        if not keys:
            raise ValueError("KalshiAdapter needs at least one league to collect")
        self._leagues: tuple[str, ...] = tuple(keys)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self._http = SourceClient(
            source_key, timeout=timeout, client=client, host_interval=HOST_INTERVAL
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    @property
    def series(self) -> tuple[Series, ...]:
        return tuple(entry for entry in SERIES if entry.league in self._leagues)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Exactly the markets the registered series cover, and no more.

        Derived from :data:`SERIES`, so a claim cannot outrun the code: a
        prediction market that prices moneylines and nothing else is not a broken
        sportsbook, and validation must not report the absence as a renamed label.
        """
        del tier
        claims: dict[str, set[Market]] = {}
        for entry in self.series:
            if entry.period is Period.FULL_GAME:
                claims.setdefault(entry.league, set()).add(entry.market)
        return capabilities_from(
            {league: frozenset(markets) for league, markets in claims.items()},
            self._leagues,
        )

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Every configured series' open markets.

        *tier* changes nothing: a series' whole market set arrives from one
        paged endpoint, so there is no per-event follow-up to defer.
        """
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for entry in self.series:
            tally.requested(entry.ticker)
            # ``into=raws`` rather than a returned list: a scope that fails
            # part-way through keeps the pages it already fetched.  They were
            # paid for out of somebody else's bandwidth, they are valid, and
            # discarding them turned "page 2 of 3 timed out" into "this whole
            # sport returned nothing" — 4 of 6 requests thrown away on a run
            # that reported OK.
            before = len(raws)
            try:
                self._fetch_series(entry, into=raws)
            except SourceError as exc:
                log.warning("%s: series %s failed: %s", self._source_key, entry.ticker, exc)
                tally.failed(entry.ticker, exc)
                # Counted as failed, not as produced.  The pages it managed to
                # fetch are kept — that is the point of ``into=`` — but a scope
                # that raised has not produced a slate, and letting its partial
                # pages mark it "produced" would satisfy ``require_something``
                # and turn a source that lost every sport into a healthy one.
                continue
            pages = raws[before:]
            tally.produced(entry.ticker, sum(_market_count(page) for page in pages))
        tally.require_something(what="open market")
        return raws

    def _fetch_series(
        self, entry: Series, *, into: list[RawResponse] | None = None
    ) -> list[RawResponse]:
        """Every page of this scope, appended to *into* as they arrive.

        Appending as it goes rather than returning at the end is what lets a
        failure part-way through keep the pages already fetched; see the caller.
        """
        pages: list[RawResponse] = [] if into is None else into
        cursor: str | None = None
        for page in range(1, MAX_PAGES_PER_SERIES + 1):
            params = {
                "series_ticker": entry.ticker,
                "limit": str(self.page_size),
                "status": "open",
            }
            if cursor:
                params["cursor"] = cursor
            raw = self._http.get(
                f"{self.base_url}/markets",
                endpoint=f"markets:{entry.ticker}:{page:02d}",
                params=params,
            )
            payload = require_mapping(
                raw.json(), source=self._source_key, endpoint=raw.endpoint
            )
            markets = payload.get("markets") or []
            # Kept even when it holds nothing: an empty first page is what a
            # renamed field looks like, and the EmptyResponseError that follows
            # carries no payload of its own, so the failure would otherwise
            # arrive with nothing to read.
            pages.append(raw)
            if not markets:
                break
            cursor = payload.get("cursor") or None
            if not cursor or len(markets) < self.page_size:
                break
        return pages

    # ── parse ────────────────────────────────────────────────────────────────

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_kalshi(raws)

    def close(self) -> None:
        self._http.close()


def _market_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    return len(payload.get("markets") or []) if isinstance(payload, dict) else 0


# ── parsing (pure) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Game:
    game_id: str
    """The ticker's game part, shared by every series covering the fixture."""

    series: Series
    competition: League
    home: Participant
    away: Participant
    commence_time: datetime
    base_key: str


def parse_kalshi(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Kalshi responses into normalized rows.

    Pure: no network, no clock, no filesystem.  Every mapping below reads a
    structured field — a ticker segment, a strike, a timestamp — and anything not
    unambiguously mappable is refused with a stable reason rather than guessed at.
    """
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

    # Pass 1: fixtures.  Both passes are needed because a contract's own ticker
    # names only one team, and the other comes from the event ticker it shares
    # with its sibling.
    games: dict[tuple[str, str], _Game] = {}
    judged: set[tuple[str, str]] = set()
    started: set[tuple[str, str]] = set()
    work: list[tuple[RawResponse, Mapping[str, Any], Series]] = []
    for raw in latest_per_endpoint(latest_capture(raws)):
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: expected a JSON object in a markets response"
            )
        for market in payload.get("markets") or []:
            if not isinstance(market, dict):
                outcome.skipped["market_not_an_object"] += 1
                continue
            entry = _series_for(market)
            if entry is None:
                outcome.skipped[f"unregistered_series:{_series_ticker(market)}"] += 1
                continue
            # Per **market**, which is the thing that carries a price.
            #
            # This test used to live in ``_accept_game``, and that runs only for
            # the first market of a fixture — every other market of the same
            # game was never status-checked at all, and the row builder below
            # hard-codes ``QuoteStatus.ACTIVE``.  So a settled or closed
            # contract sitting alongside an active one was published as a
            # takeable price with its stale last ask.  When the closed one came
            # *first* it was worse: the skip was counted and the row was emitted
            # anyway, because the next market re-entered ``_accept_game`` and
            # established the fixture, leaving the counter asserting a drop that
            # had not happened.  Both sibling exchanges test this per market.
            if str(market.get("status") or "") not in _ACTIVE_STATUSES:
                outcome.skipped[f"market_status:{market.get('status')}"] += 1
                continue
            key = (entry.league, _game_id(market))
            # Judged **once** per fixture, whether it is accepted or not.
            #
            # ``games[key]`` was only set on success, so a declined fixture was
            # re-judged for every market it has: 19 markets on one already-
            # started game produced ``game_already_started: 19`` *and*
            # ``market_on_out_of_scope_game: 19`` — 38 counted drops for 19
            # records — and on the rejecting branches one bad ticker would have
            # become 19 separate ``Rejection`` rows, so the health line read
            # "19 record(s) rejected" for one bad fixture.
            if not key[1]:
                # A market whose series filter it passed but which carries no
                # ``event_ticker``: ``_game_id`` returns "" so the fixture is
                # never judged, ``unreadable_game_ticker`` never runs, and every
                # such market fell through to a skip whose note reads "belongs
                # to a fixture that was not collected" — which is not what
                # happened.  It is an unreadable identity, and that is a fault.
                outcome.reject(
                    source,
                    "unreadable_game_ticker",
                    f"market {market.get('ticker')!r} carries no event_ticker, so the "
                    "fixture it belongs to cannot be named",
                )
                continue
            # Queued **after** the guard.  Queued before it, the record was
            # rejected here and then counted a second time in the pass below as
            # ``market_on_out_of_scope_game`` — whose note reads "belongs to a
            # fixture that was not collected", which is not what happened.  One
            # record, two counts: the same double-count this loop was rewritten
            # to remove, reintroduced by the branch that rewrite added.
            work.append((raw, market, entry))
            if key not in judged:
                judged.add(key)
                try:
                    game = _accept_game(market, entry, source, raw.fetched_at, outcome)
                except _GameNotPregame:
                    started.add(key)
                    game = None
                if game is not None:
                    games[key] = game

    if not games:
        return outcome

    event_keys = resolve_doubleheaders(
        {
            f"{league}:{game.game_id}": (game.base_key, game.commence_time)
            for (league, _), game in games.items()
        }
    )

    for raw, market, entry in work:
        key = (entry.league, _game_id(market))
        game = games.get(key)
        if game is None:
            # One count per dropped market, under the fixture's own reason where
            # there is one.  This used to add ``market_on_out_of_scope_game`` on
            # top of a per-fixture counter that had already fired once per
            # market — 38 counted drops for 19 records — and naming the cause
            # here is what makes the number readable as well as right.
            outcome.skipped[
                "game_already_started" if key in started else "market_on_out_of_scope_game"
            ] += 1
            continue
        _parse_market(
            market=market,
            raw=raw,
            source=source,
            entry=entry,
            game=game,
            event_key=event_keys[f"{entry.league}:{game.game_id}"],
            outcome=outcome,
        )

    drop_duplicate_selections(source, outcome)
    return outcome


def _series_ticker(market: Mapping[str, Any]) -> str:
    return str(market.get("event_ticker") or market.get("ticker") or "").split("-")[0]


def _series_for(market: Mapping[str, Any]) -> Series | None:
    return SERIES_BY_TICKER.get(_series_ticker(market))


def _game_id(market: Mapping[str, Any]) -> str:
    """The fixture part of an event ticker, shared across a game's series.

    ``KXMLBGAME-26JUL302210SEALAD`` and ``KXMLBTOTAL-26JUL302210SEALAD`` describe
    one game, so the identity is the part after the series.
    """
    event_ticker = str(market.get("event_ticker") or "")
    _, _, tail = event_ticker.partition("-")
    return tail


def _split_codes(game_id: str, competition: League) -> tuple[str, str] | None:
    """``(away code, home code)`` from a ticker's team segment, or ``None``.

    The two codes are concatenated with no separator, so the split is found by
    testing every position and keeping only the ones where **both** halves
    resolve to a competitor in this league.  Exactly one such split is an answer;
    zero or several is a refusal.

    Refusing on ambiguity rather than picking the first is the whole point: the
    order is away-then-home, so choosing wrongly would not lose a fixture, it
    would silently swap which side every price on it refers to — two legs backing
    the same team, which is the shape of a phantom arbitrage.
    """
    match = _GAME_PART.match(game_id)
    if match is None:
        return None
    teams = match.group("teams")
    found: list[tuple[str, str]] = []
    for cut in range(1, len(teams)):
        away, home = teams[:cut], teams[cut:]
        if (
            _resolve_code(away, competition) is not None
            and _resolve_code(home, competition) is not None
        ):
            found.append((away, home))
    return found[0] if len(found) == 1 else None


class _GameNotPregame(Exception):
    """The fixture has started.  Raised so the caller can count each market it
    drops, rather than counting the fixture once for nineteen records."""


def _accept_game(
    market: Mapping[str, Any],
    entry: Series,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> _Game | None:
    game_id = _game_id(market)
    competition = league_registry.league(entry.league)

    # No status test here: the caller applies it per market, before this is
    # reached.  See the note there for why per-fixture was not enough.
    codes = _split_codes(game_id, competition)
    if codes is None:
        outcome.reject(
            source,
            "unreadable_game_ticker",
            f"event ticker segment {game_id!r} does not split into exactly one pair of "
            f"{competition.key} competitors, so which side is home cannot be read",
            game_id=game_id,
        )
        return None
    away_code, home_code = codes
    away = _resolve_code(away_code, competition)
    home = _resolve_code(home_code, competition)
    if away is None or home is None or away.key == home.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"{game_id!r} resolved to {away_code}/{home_code}, which is not two "
            f"distinct {competition.key} competitors",
            game_id=game_id,
        )
        return None

    commence_time = _ticker_start(game_id)
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event ticker segment {game_id!r} carries no scheduled start time, and "
            "occurrence_datetime is the expected *end* rather than the start",
            game_id=game_id,
        )
        return None
    # Cross-checked against the venue's own expiry, which should sit after the
    # start and within a game's length of it.  Not used as the time — it is the
    # expiry — but a disagreement means the ticker convention has changed, and
    # filing a fixture at a time nobody else agrees on is how it joins the wrong
    # game.
    expiry = parse_iso_time(market.get("occurrence_datetime"))
    if expiry is not None and not (
        commence_time <= expiry <= commence_time + MAX_EXPIRY_AFTER_START
    ):
        outcome.reject(
            source,
            "start_time_disagrees_with_expiry",
            f"{game_id!r} decodes to a {commence_time.isoformat()} start, but the "
            f"market expires at {expiry.isoformat()} — the ticker's time convention "
            "has changed, so the start cannot be trusted",
            game_id=game_id,
        )
        return None
    if commence_time <= captured_at:
        # Kalshi keeps a contract open through the game; an in-play contract is
        # not a pregame price.
        #
        # Counted by the caller, once per **market** it drops, rather than here
        # once per fixture: the contract is that every dropped *record* is
        # counted, and one already-started game carries nineteen of them.
        raise _GameNotPregame

    away_side, home_side = orient(away, home, competition, home=home)
    return _Game(
        game_id=game_id,
        series=entry,
        competition=competition,
        home=home_side,
        away=away_side,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _market_suffix(market: Mapping[str, Any]) -> str:
    ticker = str(market.get("ticker") or "")
    event_ticker = str(market.get("event_ticker") or "")
    if event_ticker and ticker.startswith(f"{event_ticker}-"):
        return ticker[len(event_ticker) + 1 :]
    return ticker.rpartition("-")[2]


def _price(market: Mapping[str, Any], field: str) -> float | None:
    """A dollar price, or ``None`` when there is nothing takeable.

    ``yes_ask``/``yes_bid`` are the documented names and return ``None`` on the
    live API; the values are in the ``_dollars`` fields.  Reading the documented
    ones would have produced nulls in silence, which is why the field name is
    passed in explicitly rather than tried in a fallback chain.
    """
    raw = market.get(field)
    if raw is None:
        return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    # 0 is "no offer" and 1 is "certain", and neither is a price you can profit
    # from; both would also break the reciprocal below.
    return price if 0.0 < price < 1.0 else None


#: Depth on the NO side, which Kalshi does not publish under that name.
#:
#: ``no_ask_size_fp`` appears in **none** of the 354 captured markets — the only
#: size fields are ``yes_ask_size_fp`` and ``yes_bid_size_fp`` — so reading it
#: returned ``None`` for every UNDER on a total and one side of every spread:
#: 255 of 590 rows silently carried no bankroll cap at all.  ``src.arb`` reads a
#: missing cap as "this leg states none", so those positions were sized entirely
#: from the other leg, under a note saying the real cap can only be lower.  It
#: could be a great deal lower: 47 of the affected rows have under $100 of real
#: depth and one has **$0.12**.
#:
#: The value is not missing, only differently named.  Buying NO at its ask is
#: the same trade as selling YES at its bid — ``no_ask == 1 - yes_bid`` holds
#: exactly on all 354 markets — so the depth behind a NO ask is the depth behind
#: the YES bid.
_NO_ASK_SIZE = "yes_bid_size_fp"


def _size(market: Mapping[str, Any], field: str, price: float) -> float | None:
    """The largest stake that can be filled at *price*, in dollars.

    Kalshi states a contract *count*; the stake is that count times the price
    paid.  ``limit_amount`` is a stake everywhere else in this pipeline, so the
    conversion happens here rather than leaving two units in one column.
    """
    raw = market.get(field)
    if raw is None:
        return None
    try:
        contracts = float(raw)
    except (TypeError, ValueError):
        return None
    stake = contracts * price
    return stake if stake > 0 else None


def _parse_market(
    *,
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    entry: Series,
    game: _Game,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    suffix = _MARKET_SUFFIX.match(_market_suffix(market))
    if suffix is None:
        outcome.skipped["unreadable_market_ticker"] += 1
        return
    code = suffix.group("code")

    if entry.shape == Shape.GAME_TOTAL:
        line = _strike(market)
        if line is None:
            outcome.reject(
                source,
                "missing_strike",
                f"{market.get('ticker')!r} carries no floor_strike, so its total has no line",
                game_id=game.game_id,
            )
            return
        # Same reasoning as the spread branch below: ``strike_type: "greater"``
        # is strict, so a whole strike makes UNDER win on the exact total while
        # ``settlement_outcomes`` models it as a push for both sides.
        if line == int(line):
            outcome.skipped["whole_number_strike"] += 1
            return
        sides = [
            (Selection.OVER, line, "yes_ask_dollars", "yes_ask_size_fp"),
            (Selection.UNDER, line, "no_ask_dollars", _NO_ASK_SIZE),
        ]
        market_id = str(market.get("ticker"))
    elif entry.shape == Shape.TEAM_SPREAD:
        line = _strike(market)
        subject = _side_for(code, game)
        if line is None or subject is None:
            outcome.reject(
                source,
                "unreadable_spread_market",
                f"{market.get('ticker')!r}: strike {market.get('floor_strike')!r}, team "
                f"code {code!r} — one of the two does not resolve for this fixture",
                game_id=game.game_id,
            )
            return
        other = Selection.AWAY if subject is Selection.HOME else Selection.HOME
        # "X wins by over 3.5" is X giving 3.5, so YES is X at -3.5 and NO is the
        # other side receiving +3.5.  A half line cannot be landed on, so the two
        # really are complementary — and that is now checked rather than assumed.
        #
        # ``strike_type: "greater"`` is a strict inequality, so on a *whole*
        # strike NO wins when the margin lands exactly on the number, while
        # :func:`src.arb.settlement_outcomes` models a whole spread as a push for
        # both sides.  The YES pairing is the dangerous direction: a modelled
        # floor of zero against a real loss of that leg's stake.  All 272
        # captured strikes are ``x.5``, so this is a guard against a shape the
        # venue has not yet sent rather than a fix for one it has.
        if line == int(line):
            outcome.skipped["whole_number_strike"] += 1
            return
        sides = [
            (subject, -line, "yes_ask_dollars", "yes_ask_size_fp"),
            (other, line, "no_ask_dollars", _NO_ASK_SIZE),
        ]
        market_id = str(market.get("ticker"))
    else:  # Shape.TEAM_WINNER
        subject = _side_for(code, game)
        if subject is None:
            outcome.reject(
                source,
                "unreadable_team_market",
                f"{market.get('ticker')!r}: team code {code!r} is not a competitor in "
                f"this fixture ({game.away.key} at {game.home.key})",
                game_id=game.game_id,
            )
            return
        # Only the YES side.  The NO side of this contract is economically a
        # purchase of the other team, which the *sibling* contract already
        # prices — emitting both would put two prices on one selection at one
        # source, which collides on dedup_key and aborts this source's insert.
        sides = [(subject, None, "yes_ask_dollars", "yes_ask_size_fp")]
        # Both team contracts share one market id, because they are one market:
        # Kalshi lists the moneyline's two sides as two contracts, and grouping
        # them lets the completeness and overround checks see a whole market.
        market_id = f"{entry.ticker}:{game.game_id}"

    for selection, line, price_field, size_field in sides:
        price = _price(market, price_field)
        if price is None:
            outcome.skipped[f"no_ask_on:{price_field}"] += 1
            continue
        decimal_odds = 1.0 / price
        # An extreme is not corruption on an order-driven venue.  A resting
        # offer at 99.99% or at a tenth of a percent is a real order somebody
        # placed, unlike a sportsbook's posted price where a number outside the
        # band is a units error.  Counted out of scope rather than rejected: a
        # rejection marks the whole source unhealthy for a market working
        # normally, which is what took Smarkets down on its first live run.
        if not is_plausible_decimal_odds(decimal_odds):
            outcome.skipped["price_outside_the_plausible_band"] += 1
            continue
        try:
            outcome.quotes.append(
                Quote(
                    source=source,
                    observed_at=raw.fetched_at,
                    raw_ref=raw.ref,
                    sport=entry.sport,
                    league=game.competition.key,
                    event_key=event_key,
                    source_event_id=game.game_id,
                    home_participant=game.home.key,
                    away_participant=game.away.key,
                    home_team=game.home.name,
                    away_team=game.away.name,
                    commence_time=game.commence_time,
                    market=entry.market,
                    period=entry.period,
                    selection=selection,
                    line=None if line is None else line + 0.0,
                    is_alternate=False,
                    decimal_odds=decimal_odds,
                    american_odds=decimal_to_american(decimal_odds),
                    implied_probability=implied_probability(decimal_odds),
                    source_market_id=market_id,
                    source_selection_id=str(market.get("ticker")),
                    limit_amount=_size(market, size_field, price),
                    status=QuoteStatus.ACTIVE,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(
                source,
                "invalid_quote",
                f"{entry.market.value}/{selection.value} on {game.game_id}: {exc}",
                game_id=game.game_id,
            )


def _strike(market: Mapping[str, Any]) -> float | None:
    """The line, from ``floor_strike`` on a ``greater`` market."""
    if str(market.get("strike_type") or "") != "greater":
        return None
    try:
        return float(market["floor_strike"])
    except (KeyError, TypeError, ValueError):
        return None


def _side_for(code: str | None, game: _Game) -> Selection | None:
    if not code:
        return None
    who = _resolve_code(code, game.competition)
    if who is None:
        return None
    if who.key == game.home.key:
        return Selection.HOME
    if who.key == game.away.key:
        return Selection.AWAY
    return None
