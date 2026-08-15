"""Smarkets: a betting exchange whose vocabulary is entirely structured.

``api.smarkets.com/v3`` is open and unauthenticated, and it is the most
type-safe feed in this pipeline: a market carries
``market_type: {"name": "OVER_UNDER", "param": "8.5"}`` and a contract carries
``contract_type: {"name": "HOME"}``.  Nothing here is read off a label a human
wrote.

Four calls per pass, in stages, because the API is normalised: events, then the
markets on those events, then the contracts in those markets, then the quotes on
those contracts.  Each stage takes a comma-separated batch of ids, so the cost is
a handful of requests rather than one per fixture — but it grows with the number
of *markets*.

**The moneyline, and only the moneyline.**  Smarkets publishes a rate limit of
twenty requests per minute and enforces it: a pass that also fetched the totals
and handicaps needed several hundred requests and was throttled part-way through
four of six sports, losing whole sports to a ``429``.  There is no version of
collecting the ladder here that is both complete and polite, so it is not
collected — a stated limit is a constraint to work within, not an obstacle to
route around.

What is left is still worth having: the moneyline is the market every source in
this pipeline prices, it is the one the distinctness gate compares on, and it is
one market per fixture rather than fifteen.  Both tiers collect it, and
:meth:`SmarketsAdapter.capabilities` claims nothing else, so the absent handicaps
are a declared scope rather than a market that appears to have vanished.

**You back by taking an offer, not by matching a bid.**  ``bids`` are people
wanting to buy the contract; ``offers`` are people willing to sell it, and
selling is what makes a back bet possible.  The two readings are not close: on a
captured MLB moneyline the offers sum to 102.4% — a thin book's spread — while
the bids sum to 98.6%, which would be free money sitting on a public exchange.

**Prices are in units of 0.01% of probability.**  ``5780`` is 57.80%, so the
decimal odds are ``10000 / 5780``.

**Quantities are unit-less, so no liquidity is claimed.**  The quote payload
states a ``quantity`` with no currency and no scale anywhere in the response, and
``limit_amount`` is a *stake in currency* everywhere else in this pipeline.
Guessing a divisor would either cap every position at a fraction of what is
available or fail to cap one that should be, so this source states ``None`` —
which the schema already means as "unknown" — and the exchanges that do publish a
currency amount (Matchbook, SX Bet) carry that load.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.normalize import decimal_to_american, implied_probability, is_plausible_decimal_odds
from src.participants import (
    with_marker,
    canonical_participant,
    competition_marker,
    is_pairing,
)
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
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
    latest_capture,
    latest_per_endpoint,
    parse_iso_time,
    priced_quote,
)
from src.sources.base import ParseOutcome
from src.sources.guards import CoverageCappedError, FormatChangeError, SourceError, require_mapping

log = logging.getLogger(__name__)

SOURCE_KEY = "smarkets"
DEFAULT_BASE_URL = "https://api.smarkets.com/v3"

#: Prices are integers in hundredths of a percent of probability.
PRICE_SCALE = 10_000.0

#: Smarkets publishes twenty requests per minute and enforces it, returning a
#: ``429`` whose body states the limit.  Three seconds between requests is that
#: rate; the retry path honours the server's own expiry when it is exceeded
#: anyway, and this is what keeps it from being.
HOST_INTERVAL = 3.1

#: Ids per comma-separated batch.  Kept modest because the id strings are long
#: and a URL has a length limit that fails as a 414 rather than as a truncation.
BATCH = 20

#: Events per ``events`` request.
DEFAULT_PAGE_SIZE = 100

#: Bound on ``events`` pages per sport — a runaway-paging guard, not a limit
#: anyone expects to reach.  Matches ``kalshi.MAX_PAGES_PER_SERIES`` and
#: ``sxbet.MAX_PAGES_PER_SPORT``; this adapter was the one paging source without
#: one, so a sport with more than a page of upcoming fixtures was truncated in
#: silence.  ``state=upcoming`` spans weeks rather than a day, so soccer and
#: tennis reach that in the ordinary course of things.
MAX_PAGES_PER_SPORT = 10

_OPEN = "open"


@dataclass(frozen=True)
class EventType:
    """One Smarkets event type and the sport it carries."""

    name: str
    sport: Sport


EVENT_TYPES: tuple[EventType, ...] = (
    EventType("baseball_match", Sport.BASEBALL),
    EventType("basketball_match", Sport.BASKETBALL),
    EventType("ice_hockey_match", Sport.HOCKEY),
    EventType("american_football_match", Sport.FOOTBALL),
    EventType("football_match", Sport.SOCCER),
    EventType("tennis_match", Sport.TENNIS),
)

TYPE_BY_SPORT: dict[Sport, EventType] = {entry.sport: entry for entry in EVENT_TYPES}


@dataclass(frozen=True)
class MarketRule:
    market: Market
    period: Period


#: Exact ``(Sport, market_type.name)`` -> what it settles as.
#:
#: Keyed by **sport**, because the same type name is a different contract in
#: different ones and mapping both onto ``MONEYLINE/FULL_GAME`` is not a loss of
#: precision — it is a collision.  Two rows for one selection at one source share
#: a ``dedup_key``, so one of them is dropped, and which one survives is decided
#: by a string sort over market ids.
#:
#: * Soccer's winner is ``WINNER_3_WAY`` ("Full-time result"), verified live.  A
#:   *two-way* soccer winner is draw-no-bet, which refunds on a level score — a
#:   different contract, counted out of scope.
#: * Tennis, baseball, basketball and gridiron use ``WINNER_2_WAY`` ("Match
#:   winner"), verified live for tennis and baseball.
#: * Hockey is the one that would hurt: a three-way hockey winner is the
#:   **60-minute** market and the two-way one includes overtime, so collapsing
#:   them files a regulation price as a full-game price — and an overtime win
#:   then loses a leg that was reported as covered.  The NHL is out of season, so
#:   the labels cannot be checked against live data; rather than guess, the
#:   three-way is counted out of scope until it can be.
MARKET_RULES: dict[tuple[Sport, str], MarketRule] = {
    (Sport.BASEBALL, "WINNER_2_WAY"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.BASKETBALL, "WINNER_2_WAY"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.FOOTBALL, "WINNER_2_WAY"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.TENNIS, "WINNER_2_WAY"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.HOCKEY, "WINNER_2_WAY"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
    (Sport.SOCCER, "WINNER_3_WAY"): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
}

#: Winner types that are deliberately not collected, with the reason each is a
#: different contract from the one that is.
OUT_OF_SCOPE: dict[tuple[Sport, str], str] = {
    (Sport.SOCCER, "WINNER_2_WAY"): "draw_voids_market",
    (Sport.HOCKEY, "WINNER_3_WAY"): "unverified_three_way_window",
    (Sport.BASEBALL, "WINNER_3_WAY"): "unverified_three_way_window",
    (Sport.BASKETBALL, "WINNER_3_WAY"): "unverified_three_way_window",
    (Sport.FOOTBALL, "WINNER_3_WAY"): "unverified_three_way_window",
    (Sport.TENNIS, "WINNER_3_WAY"): "unverified_three_way_window",
}

#: The market types worth fetching contracts and quotes for.  See this module's
#: docstring: the ladder is not affordable within the venue's own published rate
#: limit, so it is declared out of scope rather than half-collected.
COLLECTED_MARKET_TYPES: frozenset[str] = frozenset({"WINNER_2_WAY", "WINNER_3_WAY"})

#: Types this venue publishes that **are** one of the four kinds this pipeline
#: collects, and that this adapter nevertheless does not fetch.
#:
#: Not out of scope — a baseball ``HANDICAP`` is a run line and ``OVER_UNDER``
#: is a total, both core markets.  They are left because this venue publishes a
#: rate limit of twenty requests a minute and there is no version of collecting
#: the ladder here that is both complete and polite.  That is a coverage
#: decision and it belongs under its own reason: filed as
#: ``market_type_out_of_scope`` they rendered on the dashboard as "not one of
#: the four kinds collected here", which is the opposite of true for 254 rows
#: on one slate.
IN_SCOPE_BUT_NOT_FETCHED: frozenset[str] = frozenset(
    {
        # spread / total, full game
        "HANDICAP",
        "OVER_UNDER",
        # ...and on the two partial windows baseball collects
        "INNINGS_1_TO_5_HANDICAP",
        "INNINGS_1_TO_5_OVER_UNDER",
        "FIRST_INNING_OVER_UNDER",
        # team totals, which are the fourth kind
        "HOME_TEAM_OVER_UNDER",
        "AWAY_TEAM_OVER_UNDER",
    }
)

#: ``contract_type.name`` -> canonical selection.
CONTRACT_TYPES: dict[str, Selection] = {
    "HOME": Selection.HOME,
    "AWAY": Selection.AWAY,
    "DRAW": Selection.DRAW,
    "OVER": Selection.OVER,
    "UNDER": Selection.UNDER,
}

#: The competition segment of ``full_slug`` -> canonical league, per sport.
#: ``/sport/baseball/mlb/2026/07/28/22-45/toronto-blue-jays-at-washington-nationals``
LEAGUE_BY_SLUG: dict[Sport, dict[str, str]] = {
    Sport.BASEBALL: {"mlb": "MLB"},
    Sport.BASKETBALL: {"nba": "NBA", "wnba": "WNBA"},
    Sport.HOCKEY: {"nhl": "NHL"},
    Sport.FOOTBALL: {"nfl": "NFL"},
    # Smarkets prefixes every football competition with its country, so these
    # keys are the ones its own URLs carry: ``/sport/football/england-premier-
    # league/…``.  The table used to hold the bare forms — ``premier-league``,
    # ``la-liga``, ``serie-a`` — and therefore **matched nothing**: on the
    # 2026-08-14 capture it resolved 0 of 947 football events, and every real
    # EPL, La Liga, Serie A, Ligue 1, Bundesliga and MLS fixture Smarkets priced
    # was filed under the catch-all.  Nine ``league_disagreement`` findings on
    # that run were this one bug.
    #
    # No row was ever lost — league is not part of event identity — but the
    # league filter, the coverage grid and every per-league count were wrong for
    # the largest soccer feed in the run.
    #
    # Matched **exactly**, which is what makes the country prefix load-bearing:
    # the same capture carries ``brazil-serie-a`` beside ``italy-serie-a`` and
    # ``spain-la-liga-2`` beside ``spain-la-liga``, and neither collides here.
    Sport.SOCCER: {
        "england-premier-league": "EPL",
        "us-major-league-soccer": "MLS",
        "spain-la-liga": "LA_LIGA",
        "italy-serie-a": "SERIE_A",
        "germany-bundesliga": "BUNDESLIGA",
        "france-ligue-1": "LIGUE_1",
    },
}

CATCH_ALL: dict[Sport, str] = {
    Sport.SOCCER: "SOCCER_OTHER",
    Sport.TENNIS: "TENNIS_OTHER",
    Sport.HOCKEY: "HOCKEY_OTHER",
}

TENNIS_TOUR_PREFIXES: tuple[tuple[str, str], ...] = (
    ("atp-challenger", "ATP_CHALLENGER"),
    ("challenger", "ATP_CHALLENGER"),
    ("itf", "ITF"),
    ("wta", "WTA"),
    ("atp", "ATP"),
)

DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB", "WNBA", "NBA", "NHL", "HOCKEY_OTHER", "NFL",
    "ATP", "WTA", "ATP_CHALLENGER", "ITF", "TENNIS_OTHER",
    "EPL", "MLS", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1", "SOCCER_OTHER",
)

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    sport: frozenset({Market.MONEYLINE}) for sport in Sport
}


def events_endpoint(sport: Sport, page: int = 1) -> str:
    """Label for one page of a sport's events.

    Carries the page number because :func:`latest_per_endpoint` keeps the newest
    response *per label*: without it, page two would overwrite page one and the
    fix for the truncation would have quietly reintroduced it.

    Page one keeps the unsuffixed label so captures taken before paging existed
    still replay under the same name.
    """
    return f"events:{sport.value}" if page <= 1 else f"events:{sport.value}:{page:02d}"


def markets_endpoint(sport: Sport, batch: int) -> str:
    return f"markets:{sport.value}:{batch:02d}"


def contracts_endpoint(sport: Sport, batch: int) -> str:
    return f"contracts:{sport.value}:{batch:02d}"


def quotes_endpoint(sport: Sport, batch: int) -> str:
    return f"quotes:{sport.value}:{batch:02d}"


class SmarketsAdapter:
    """Collects Smarkets' pregame exchange prices for the configured leagues."""

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
        if not keys:
            raise ValueError("SmarketsAdapter needs at least one league to collect")
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
    def sports(self) -> tuple[Sport, ...]:
        wanted = {league_registry.league(key).sport for key in self._leagues}
        return tuple(sport for sport in Sport if sport in wanted and sport in TYPE_BY_SPORT)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """The moneyline, in both tiers.

        Declared rather than left to be inferred, because the absence of totals
        and handicaps here is a *decision* — the venue's rate limit will not
        support fetching them — and an undeclared absence is reported by
        validation as a market that has disappeared.
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
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for sport in self.sports:
            tally.requested(sport.value)
            # ``into=raws`` rather than a returned list: a scope that fails
            # part-way through keeps the pages it already fetched.  They were
            # paid for out of somebody else's bandwidth, they are valid, and
            # discarding them turned "page 2 of 3 timed out" into "this whole
            # sport returned nothing" — 4 of 6 requests thrown away on a run
            # that reported OK.
            self._last_event_count = 0
            try:
                self._fetch_sport(sport, tier, into=raws)
            except SourceError as exc:
                log.warning("%s: %s failed: %s", self._source_key, sport.value, exc)
                tally.failed(sport.value, exc)
                # Counted as failed, not as produced.  The pages it managed to
                # fetch are kept — that is the point of ``into=`` — but a scope
                # that raised has not produced a slate, and letting its partial
                # pages mark it "produced" would satisfy ``require_something``
                # and turn a source that lost every sport into a healthy one.
                continue
            produced = self._last_event_count
            # Counted in **events**, as every sibling counts items rather than
            # responses.  Counting responses made an empty sport score 1 — it
            # always appends exactly one events page, which is now deliberately
            # kept — so ``require_something`` could never fire, one empty sport
            # excused every refused one, and an off day was reported as an
            # upstream format change.
            tally.produced(sport.value, produced)
        tally.require_something(what="upcoming event")
        return raws

    def _fetch_sport(
        self, sport: Sport, tier: Tier, *, into: list[RawResponse] | None = None
    ) -> list[RawResponse]:
        """Every page of one sport, appended to *into* as they arrive.

        Appending as it goes rather than returning at the end is what lets a
        failure part-way through keep the pages already fetched; see the caller.
        """
        raws: list[RawResponse] = [] if into is None else into
        entry = TYPE_BY_SPORT[sport]
        # ``into=raws``: the event pages land in the caller's list as they
        # arrive.  They are kept even for a sport with no fixtures, because this
        # page is the only thing that distinguishes "the venue has no soccer
        # today" from "the field we read the fixtures out of was renamed" — and
        # they are kept even when a later page raises, which is the case that
        # left four of this source's six sports with nothing on disk at all.
        _, events = self._fetch_events(sport, entry, into=raws)
        self._last_event_count = len(events)
        if not events:
            return raws

        event_ids = [str(event["id"]) for event in events if event.get("id") is not None]
        # ``into=raws`` for the same reason as the event pages, and it matters
        # more here: batches are 141 of this source's 156 requests, so a loop
        # that built its own list and raised on the last batch threw away almost
        # the entire pass — responses already fetched, already paid for in
        # latency, and already inside the observation window.
        first_batch = len(raws)
        self._batched(
            f"{self.base_url}/events/{{ids}}/markets/",
            event_ids,
            lambda index: markets_endpoint(sport, index),
            into=raws,
        )
        market_raws = raws[first_batch:]

        del tier  # the ladder is out of scope for this source in both tiers
        market_ids = sorted(_market_ids(market_raws, COLLECTED_MARKET_TYPES, sport))
        if not market_ids:
            return raws
        self._batched(
            f"{self.base_url}/markets/{{ids}}/contracts/",
            market_ids,
            lambda index: contracts_endpoint(sport, index),
            into=raws,
        )
        self._batched(
            f"{self.base_url}/markets/{{ids}}/quotes/",
            market_ids,
            lambda index: quotes_endpoint(sport, index),
            into=raws,
        )
        return raws

    def _fetch_events(
        self,
        sport: Sport,
        entry: EventType,
        *,
        into: list[RawResponse] | None = None,
    ) -> tuple[list[RawResponse], list[dict[str, Any]]]:
        """Every upcoming event of one sport, following the venue's own cursor.

        ``pagination.next_page`` is in every response and was read by nothing, so
        a sport with more than ``page_size`` upcoming fixtures lost the rest with
        no counter, no warning, and a non-zero page count that made the sport
        look healthy.

        Every capture so far has ``next_page: null``, so its exact form is
        unobserved.  A path or URL is followed; anything else is **not** guessed
        at — it stops the loop and raises, because a token this adapter cannot
        read means the slate is incomplete and quietly returning most of it is
        the failure being fixed, not a smaller version of it.
        """
        # Appended to the caller's list as they arrive, so a cursor that fails
        # on page two does not take page one with it.  Keeping the pages one
        # level up was not enough: this helper raises *before* returning, so the
        # sports whose fetch failed still left nothing on disk — which is the
        # whole reason the empty page is kept in the first place.
        pages: list[RawResponse] = [] if into is None else into
        events: list[dict[str, Any]] = []
        url: str = f"{self.base_url}/events/"
        params: dict[str, str] | None = {
            "type": entry.name,
            "state": "upcoming",
            "limit": str(self.page_size),
        }
        for page in range(1, MAX_PAGES_PER_SPORT + 1):
            raw = self._http.get(url, endpoint=events_endpoint(sport, page), params=params)
            payload = require_mapping(
                raw.json(), source=self._source_key, endpoint=raw.endpoint
            )
            found = [e for e in (payload.get("events") or []) if isinstance(e, dict)]
            # Kept even when it holds nothing.  The contract is that every byte a
            # source returns reaches disk before anything interprets it, and this
            # is the one response worth having when the key is renamed upstream:
            # discarding it meant an empty sport produced no capture at all, and
            # the ``EmptyResponseError`` that follows carries no payload of its
            # own, so the failure arrived with nothing to read.
            pages.append(raw)
            if not found:
                break
            events.extend(found)
            nxt = (payload.get("pagination") or {}).get("next_page")
            if not nxt or len(found) < self.page_size:
                break
            token = str(nxt)
            if token.startswith("http://") or token.startswith("https://"):
                url = token
            elif token.startswith("/"):
                url = f"{self.base_url.rstrip('/')}{token}"
            elif token.startswith("?"):
                # A query-only reference — "same path, this query" — which is
                # what Smarkets actually sends:
                #
                #   ?state=upcoming&type=football_match&limit=100&sort=id
                #   &pagination_last_id=45164938
                #
                # Refusing it cost **three whole sports**.  Soccer, tennis and
                # football each returned a full first page and a cursor, this
                # adapter raised on the cursor, and the caller dropped the sport
                # — every time, on every run, invisibly, because a refused scope
                # was not reported anywhere until the health row learned to carry
                # it.  The venue's own paging worked; the guard written to stop
                # silent truncation was causing total loss instead.
                url = f"{self.base_url.rstrip('/')}/events/{token}"
            else:
                raise FormatChangeError(
                    f"{self._source_key}:{raw.endpoint}: pagination.next_page is "
                    f"{token!r}, which is neither a path nor a URL — this adapter "
                    "cannot follow it, and the remaining fixtures would be dropped "
                    "without being counted"
                )
            # The cursor carries every parameter it needs, including the type
            # and page size; sending ours as well would replace its own query.
            params = None
        else:
            # Falling off the cap with the venue's cursor still live is a
            # truncated slate, and reporting the scope as fully produced makes it
            # invisible.  ``CoverageCappedError`` exists for exactly this: "a
            # bound on request volume is politeness and stays; reporting the run
            # as complete afterwards is not".
            if nxt and self.last_fetch is not None:
                # Filed under the scope's own name.  ``failed()`` *registers*
                # the name it is given, so a decorated one ("soccer: stopped at
                # the 2-page cap") is a scope the book was never asked for: it
                # lands in the numerator and the denominator both, and a source
                # whose every scope truncated then grades 0.5 — a WARNING saying
                # it "returned the rest" — when the share lost is 1.0.
                self.last_fetch.truncated(
                    sport.value,
                    CoverageCappedError(
                        f"{self._source_key}: {sport.value} still had a cursor when "
                        f"the {MAX_PAGES_PER_SPORT}-page cap was reached; the rest "
                        f"were not collected"
                    ),
                )
        return pages, events

    def _batched(
        self,
        template: str,
        ids: Sequence[str],
        label,
        *,
        into: list[RawResponse] | None = None,
    ) -> list[RawResponse]:
        raws: list[RawResponse] = [] if into is None else into
        for index in range(0, len(ids), BATCH):
            batch = ids[index : index + BATCH]
            raws.append(
                self._http.get(
                    template.format(ids=",".join(batch)),
                    endpoint=label(index // BATCH + 1),
                    record_params={"ids": ",".join(batch)},
                )
            )
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_smarkets(raws)

    def close(self) -> None:
        self._http.close()


def _market_ids(raws: Iterable[RawResponse], wanted: Any, sport: Sport | None = None) -> set[str]:
    """Ids of the markets worth fetching contracts and quotes for.

    *sport* applies the same ``OUT_OF_SCOPE`` rule the parser applies, so the
    ids requested are the ids used.  Without it the two disagreed: soccer
    publishes a ``WINNER_2_WAY`` (draw-no-bet) beside its ``WINNER_3_WAY`` and
    hockey a ``WINNER_3_WAY`` beside its ``WINNER_2_WAY``, both of which
    :func:`_parse_market` then rejects — after their contracts and quotes had
    been paid for.  On a hundred-fixture soccer slate that is two hundred ids
    instead of one hundred, ten batches instead of five, and about thirty
    seconds of a twenty-requests-per-minute budget this adapter has already lost
    whole sports to.
    """
    found: set[str] = set()
    for raw in raws:
        try:
            payload = raw.json()
        except ValueError:
            continue
        for market in (payload or {}).get("markets") or []:
            if not isinstance(market, dict):
                continue
            name = str((market.get("market_type") or {}).get("name") or "")
            if wanted and name not in wanted:
                continue
            if sport is not None and (sport, name) in OUT_OF_SCOPE:
                continue
            if str(market.get("state") or "") != _OPEN:
                continue
            if market.get("id") is not None:
                found.add(str(market["id"]))
    return found


# ── parsing (pure) ───────────────────────────────────────────────────────────


#: ``book_home_key`` on the fixtures below is the competitor Smarkets calls
#: ``HOME`` — its first-named side, not always ``home``.  For **tennis** there is no
#: home player, so :func:`src.events.orient` imposes an ordering by participant key
#: while Smarkets orders by its event name, and the two disagree on about half of all
#: matches.  Reading a ``HOME`` contract as our home side there attaches the price to
#: the wrong player, which no downstream check can see: the two books agree on the
#: participants, each book's own overround is normal, and validation deliberately
#: does not compare orientation for a sport that has none.


def parse_smarkets(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured Smarkets responses into normalized rows."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    selected = latest_per_endpoint(latest_capture(raws))

    events: dict[str, Mapping[str, Any]] = {}
    markets: dict[str, Mapping[str, Any]] = {}
    contracts: dict[str, list[Mapping[str, Any]]] = {}
    quotes: dict[str, Mapping[str, Any]] = {}
    provenance: dict[str, RawResponse] = {}

    for raw in selected:
        kind = raw.endpoint.partition(":")[0]
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: expected a JSON object"
            )
        if kind == "events":
            for event in payload.get("events") or []:
                if isinstance(event, dict) and event.get("id") is not None:
                    events.setdefault(str(event["id"]), event)
        elif kind == "markets":
            for market in payload.get("markets") or []:
                if isinstance(market, dict) and market.get("id") is not None:
                    markets.setdefault(str(market["id"]), market)
        elif kind == "contracts":
            for contract in payload.get("contracts") or []:
                if isinstance(contract, dict) and contract.get("market_id") is not None:
                    contracts.setdefault(str(contract["market_id"]), []).append(contract)
        elif kind == "quotes":
            for contract_id, quote in payload.items():
                if isinstance(quote, dict):
                    quotes[str(contract_id)] = quote
                    provenance[str(contract_id)] = raw
        else:
            outcome.skipped[f"unrecognised_response:{kind}"] += 1

    if not events:
        raise FormatChangeError(
            f"{source}: no events response among "
            f"{[raw.endpoint for raw in selected]}, so no market can be placed"
        )

    price_raw = next((raw for raw in selected if raw.endpoint.startswith("quotes:")), None)
    if price_raw is None:
        # Every row's price comes from a quotes response; without one there is
        # nothing to emit and saying so is better than emitting an empty run.
        outcome.skipped["no_quotes_response"] += 1
        return outcome

    captured_at = min(raw.fetched_at for raw in selected)
    fixtures: dict[str, Fixture] = {}
    for event_id, event in events.items():
        fixture = _accept_event(event, event_id, source, captured_at, outcome)
        if fixture is not None:
            fixtures[event_id] = fixture
    if not fixtures:
        return outcome

    event_keys = resolve_doubleheaders(
        {event_id: (f.base_key, f.commence_time) for event_id, f in fixtures.items()}
    )

    for market_id, market in sorted(markets.items()):
        fixture = fixtures.get(str(market.get("event_id") or ""))
        if fixture is None:
            outcome.skipped["market_on_out_of_scope_event"] += 1
            continue
        _parse_market(
            market=market,
            market_id=market_id,
            contracts=contracts.get(market_id, []),
            quotes=quotes,
            provenance=provenance,
            fallback_raw=price_raw,
            source=source,
            fixture=fixture,
            event_key=event_keys[fixture.event_id],
            outcome=outcome,
        )

    drop_duplicate_selections(source, outcome)
    return outcome


def _accept_event(
    event: Mapping[str, Any],
    event_id: str,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> Fixture | None:
    if str(event.get("state") or "") != "upcoming":
        outcome.skipped[f"event_state:{event.get('state')}"] += 1
        return None
    if not event.get("bettable", True):
        outcome.skipped["event_not_bettable"] += 1
        return None

    classified = _classify(str(event.get("full_slug") or ""))
    if classified is None:
        outcome.skipped["competition_unmapped"] += 1
        return None
    sport, league_key = classified
    if not league_registry.is_known(league_key):
        outcome.skipped[f"league_not_registered:{league_key}"] += 1
        return None
    competition = league_registry.league(league_key)

    name = str(event.get("name") or "")
    if name.count(" at ") != 1 and name.count(" vs ") != 1:
        outcome.skipped["non_game_event"] += 1
        return None
    separator = " at " if name.count(" at ") == 1 else " vs "
    first_raw, second_raw = (part.strip() for part in name.split(separator, 1))
    # "A at B" is away at home; "A vs B" is home first, matching every other
    # source's convention and checked against Bovada's explicit home flag.
    away_raw, home_raw = (
        (first_raw, second_raw) if separator == " at " else (second_raw, first_raw)
    )
    if any(is_pairing(part, competition.sport) for part in (away_raw, home_raw)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    # A women's, reserve or youth competition whose *name* carries the marker
    # while the team names do not.  The venue's own competition slug is the
    # third segment of ``full_slug`` — "football/england-womens-championship" —
    # and unrecognised competitions fall back to one catch-all league, so it is
    # otherwise read by nothing.
    marker = competition_marker(
        _competition_slug(str(event.get("full_slug") or "")), competition.sport
    )
    home_raw = with_marker(home_raw, marker)
    away_raw = with_marker(away_raw, marker)
    home = canonical_participant(home_raw, competition)
    away = canonical_participant(away_raw, competition)
    if home is None or away is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} ({name!r}) in {competition.key}: {away_raw!r} -> "
            f"{away.key if away else None}, {home_raw!r} -> {home.key if home else None}",
            event_id=event_id,
        )
        return None

    commence_time = parse_iso_time(event.get("start_datetime"))
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} has unparseable start_datetime "
            f"{event.get('start_datetime')!r}",
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
        sport=sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=home.key,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _competition_slug(full_slug: str) -> str:
    """The competition segment of ``/sport/{sport}/{competition}/…``, as words.

    Hyphens become spaces so the marker patterns, which are word-boundary
    anchored, can see the words inside a slug.
    """
    parts = [part for part in full_slug.split("/") if part]
    return parts[2].replace("-", " ") if len(parts) >= 3 and parts[0] == "sport" else ""


def _classify(full_slug: str) -> tuple[Sport, str] | None:
    """``(sport, league key)`` from ``/sport/{sport}/{competition}/…``."""
    parts = [part for part in full_slug.split("/") if part]
    if len(parts) < 3 or parts[0] != "sport":
        return None
    sport_slug, competition = parts[1], parts[2]
    sport = {
        "baseball": Sport.BASEBALL,
        "basketball": Sport.BASKETBALL,
        "ice-hockey": Sport.HOCKEY,
        "american-football": Sport.FOOTBALL,
        "football": Sport.SOCCER,
        "tennis": Sport.TENNIS,
    }.get(sport_slug)
    if sport is None:
        return None
    named = LEAGUE_BY_SLUG.get(sport, {}).get(competition)
    if named is not None:
        return sport, named
    if sport is Sport.TENNIS:
        for prefix, key in TENNIS_TOUR_PREFIXES:
            if competition.startswith(prefix):
                return sport, key
    catch_all = CATCH_ALL.get(sport)
    return (sport, catch_all) if catch_all else None


def _best_offer(quote: Mapping[str, Any]) -> float | None:
    """Best takeable back price, as decimal odds.

    ``offers`` and not ``bids``.  A bid is somebody wanting to buy the contract;
    an offer is somebody willing to sell it, and selling is what makes a back bet
    possible.  Measured on a captured MLB moneyline, the offers sum to 102.4% and
    the bids to 98.6% — the second would be free money resting on a public
    exchange, which settles which side is which.
    """
    best: float | None = None
    for level in quote.get("offers") or []:
        if not isinstance(level, dict):
            continue
        try:
            price = float(level["price"]) / PRICE_SCALE
        except (KeyError, TypeError, ValueError):
            continue
        if not 0.0 < price < 1.0:
            continue
        if best is None or price < best:
            best = price
    return None if best is None else 1.0 / best


def _parse_market(
    *,
    market: Mapping[str, Any],
    market_id: str,
    contracts: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    provenance: Mapping[str, RawResponse],
    fallback_raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    market_type = market.get("market_type") or {}
    name = str(market_type.get("name") or "")
    named = OUT_OF_SCOPE.get((fixture.sport, name))
    if named is not None:
        outcome.skipped[f"{named}:{fixture.sport.value}:{name}"] += 1
        return
    rule = MARKET_RULES.get((fixture.sport, name))
    if rule is None:
        reason = (
            "market_in_scope_but_not_fetched"
            if name in IN_SCOPE_BUT_NOT_FETCHED
            else "market_type_out_of_scope"
        )
        outcome.skipped[f"{reason}:{fixture.sport.value}:{name or 'missing'}"] += 1
        return
    if rule.market not in MARKETS_BY_SPORT.get(fixture.sport, frozenset()):
        outcome.skipped[
            f"market_out_of_scope:{fixture.sport.value}:{rule.market.value}"
        ] += 1
        return
    if str(market.get("state") or "") != _OPEN:
        outcome.skipped[f"market_state:{market.get('state')}"] += 1
        return
    if not contracts:
        # Its contracts were not fetched — the core tier deliberately does not
        # ask for the ladder.
        outcome.skipped["market_without_contracts"] += 1
        return

    line: float | None = None
    if rule.market in MARKETS_REQUIRING_LINE:
        try:
            line = float(market_type["param"])
        except (KeyError, TypeError, ValueError):
            outcome.reject(
                source,
                "market_without_line",
                f"{name} market {market_id} carries no readable param, so its line is "
                "unknown",
                event_id=fixture.event_id,
            )
            return

    for contract in contracts:
        contract_id = str(contract.get("id") or "")
        selection = CONTRACT_TYPES.get(
            str((contract.get("contract_type") or {}).get("name") or "")
        )
        if selection in (Selection.HOME, Selection.AWAY):
            # ``HOME`` is the competitor *Smarkets* named first, which is our home
            # side only where the league has one — see ``Fixture.our_side``.
            selection = fixture.our_side(selection)
        if selection is None:
            outcome.skipped[
                f"contract_type_out_of_scope:"
                f"{(contract.get('contract_type') or {}).get('name')}"
            ] += 1
            continue
        if selection is Selection.DRAW and not draw_is_priced(fixture.sport, rule.period):
            outcome.reject(
                source,
                "draw_not_priced",
                f"a draw contract on {fixture.sport.value}/{rule.period.value} for "
                f"event {fixture.event_id}",
                event_id=fixture.event_id,
            )
            continue

        quote = quotes.get(contract_id)
        if not quote:
            outcome.skipped["contract_without_quote"] += 1
            continue
        decimal_odds = _best_offer(quote)
        if decimal_odds is None:
            outcome.skipped["contract_without_a_takeable_offer"] += 1
            continue
        # An extreme is not corruption on an order-driven venue.  A resting
        # offer at 99.99% or at a tenth of a percent is a real order somebody
        # placed, unlike a sportsbook's posted price where a number outside the
        # band is a units error.  Counted out of scope rather than rejected: a
        # rejection marks the whole source unhealthy for a market working
        # normally, which is what took Smarkets down on its first live run.
        if not is_plausible_decimal_odds(decimal_odds):
            outcome.skipped["price_outside_the_plausible_band"] += 1
            continue

        # ``param`` is stated from the **home** side's perspective, matching the
        # market's own label ("Blue Jays +3.5 / Nationals -3.5" at param -3.5,
        # with Nationals at home).
        own_line = line
        if line is not None and rule.market is Market.SPREAD and selection is Selection.AWAY:
            own_line = -line

        raw = provenance.get(contract_id, fallback_raw)
        # The started-game gate again, against **this price's own** response.
        #
        # The event-level check above uses the earliest response in the capture,
        # which is the moment the pass *began*.  This venue is paced to its own
        # published rate limit — 3.1 s between requests, 48 to 156 requests —
        # so a pass runs two and a half to eight minutes, and the quotes batch
        # that prices the last sport lands minutes after the events page that
        # listed it.  A fixture starting inside that window cleared the gate and
        # was emitted ACTIVE and pregame off an in-play price: measured on a
        # live capture, events page 12:12:39, first pitch 12:13:39, quotes batch
        # 12:15:09 — two moneyline rows a minute and a half into the game.
        # Matchbook, given the same fixture and the same fetch time, emitted
        # nothing and counted the skip, because its pass is seconds long and its
        # earliest response is also its latest.
        if fixture.commence_time <= raw.fetched_at:
            outcome.skipped["event_already_started"] += 1
            continue
        try:
            outcome.quotes.append(
                priced_quote(
                    fixture,
                    source=source,
                    raw=raw,
                    event_key=event_key,
                    market=rule.market,
                    period=rule.period,
                    selection=selection,
                    line=None if own_line is None else own_line + 0.0,
                    is_alternate=False,
                    decimal_odds=decimal_odds,
                    american_odds=decimal_to_american(decimal_odds),
                    implied_probability=implied_probability(decimal_odds),
                    source_market_id=market_id,
                    source_selection_id=contract_id,
                    # Quantities carry no currency or scale anywhere in the
                    # payload; see this module's docstring.  ``None`` is the
                    # schema's own word for "unknown", which is the truth.
                    limit_amount=None,
                    status=QuoteStatus.ACTIVE,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(
                source,
                "invalid_quote",
                f"{rule.market.value}/{selection.value} on {fixture.event_id}: {exc}",
                event_id=fixture.event_id,
            )
