"""SX Bet: a peer-to-peer order book, where a "price" is somebody else's order.

``api.sx.bet`` is open and unauthenticated.  It needs two calls: ``markets/active``
describes the markets and carries **no prices at all**, and ``orders`` returns the
resting maker orders for a batch of market hashes.  A price here is not something
the venue publishes — it is a specific counterparty's offer, with a size, and
taking it is the whole transaction.

Two things about that are structural, and getting either wrong inverts the data
rather than degrading it:

**A resting order states the *maker's* odds; yours are the complement.**  An
order carries ``percentageOdds`` scaled by 10^20 and a flag saying which outcome
the maker is on.  The taker is on the *other* outcome at the *complementary*
implied probability.  Read the field as the taker's own probability instead and
every price is replaced by a different real number — 10.0 where 1.11 is correct.

That reading is not taken on trust.  Measured across six NFL moneylines with
orders on both outcomes: as the maker's probability the two takeable prices sum
to 1.09–1.12, a thin book's spread; as the taker's they sum to 0.66–0.90, which
would be free money resting unclaimed on a public order book.

**Size is a maker stake in token units, and the taker's is not the same number.**
Both sides put money in a pot and the winner takes it, so the two stakes are in
the ratio of the two implied probabilities: a 10 USDC maker order at implied 0.10
backs a 90 USDC taker bet at implied 0.90.  ``limit_amount`` is a *stake*
everywhere in this pipeline, so the conversion happens here.

Only USDC orders are read.  The other collateral is WSX at a different decimal
scale, and a limit in an unfamiliar token is not comparable with the dollar
figures every other source states.

**Which side is home is stated, and was checked.**  ``teamOneName`` is the home
side: 16 of 16 NFL fixtures agreed with Bovada's explicit ``home`` flag, with no
disagreements.
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
    parse_epoch_time,
    priced_quote,
)
from src.sources.base import ParseOutcome
from src.sources.guards import CoverageCappedError, FormatChangeError, SourceError, require_mapping

log = logging.getLogger(__name__)

SOURCE_KEY = "sxbet"
DEFAULT_BASE_URL = "https://api.sx.bet"

#: The rollup the public order book lives on.
CHAIN_VERSION = "SXR"

#: Implied probabilities are integers scaled by 10^20.
ODDS_SCALE = 10**20

#: USDC on the SX rollup, and its decimal scale.  Confirmed against the API's own
#: ``/metadata``: ``makerOrderMinimums`` is 10000000 for this token, which is the
#: documented 10 USDC minimum at six decimals.
USDC_ADDRESS = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
USDC_DECIMALS = 6

#: Market hashes per ``orders`` request.  The endpoint takes a comma-separated
#: list, so a whole sport's book costs a handful of calls rather than one per
#: market.
ORDER_BATCH = 25

#: Most order records ``/orders`` will return for one request.
#:
#: Observed, not documented: the endpoint carries no cursor, no total and no
#: truncation flag, and two captured responses asking for 25 and 23 market
#: hashes both came back with exactly this many records.  A response at this
#: length is therefore assumed truncated — see
#: :meth:`SXBetAdapter._fetch_orders`.
ORDERS_PAGE_CAP = 100

#: Bound on paging per sport — a runaway guard, not a limit anyone expects to hit.
MAX_PAGES_PER_SPORT = 10

_ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class MarketRule:
    market: Market
    period: Period


#: ``(sport, SX type)`` -> what it settles as.  Numeric rather than named, so
#: the table is the only place the meaning of a number is recorded.
#:
#: Keyed by **sport**, because SX's ``type`` is not a global vocabulary and the
#: collision is live.  From a census of 1,704 market records:
#:
#: * ``52`` in tennis is the match winner — ``("Adrian Mannarino", "Learner
#:   Tien")``, no line — and tennis has no draw, so it is a moneyline.
#: * ``52`` in **soccer** is also two team names with no line — ``("Lech
#:   Poznan", "Aarhus AGF")`` — but soccer *does* have a draw, so it is a
#:   draw-loses two-way and emphatically not a moneyline.  Alongside it sits
#:   ``1``: ``("Tie", "Not tie")``.
#:
#: One flat table cannot hold both.  It previously held only the US-sport codes,
#: which meant the whole soccer and tennis book was fetched and discarded while
#: ``capabilities()`` promised eighteen leagues and the parse delivered three.
#: Keying by sport is what makes it safe to add tennis and leave soccer out.
MARKET_TYPES: dict[tuple[Sport, int], MarketRule] = {
    **{
        (sport, code): rule
        for sport in (Sport.BASEBALL, Sport.BASKETBALL, Sport.FOOTBALL, Sport.HOCKEY)
        for code, rule in (
            (226, MarketRule(Market.MONEYLINE, Period.FULL_GAME)),
            (28, MarketRule(Market.TOTAL, Period.FULL_GAME)),
            (342, MarketRule(Market.SPREAD, Period.FULL_GAME)),
        )
    },
    # Tennis: the match winner only.  Handicaps and totals here are counted in
    # games or sets (``166``, ``201``, ``165``, ``866``) and the ``202``-``204``
    # family is per-set, all of which are different contracts from the ones this
    # pipeline compares.
    (Sport.TENNIS, 52): MarketRule(Market.MONEYLINE, Period.FULL_GAME),
}

#: SX ``sportId`` -> canonical sport.  From ``/sports``.
SPORT_IDS: dict[Sport, int] = {
    Sport.BASKETBALL: 1,
    Sport.HOCKEY: 2,
    Sport.BASEBALL: 3,
    Sport.SOCCER: 5,
    Sport.TENNIS: 6,
    Sport.FOOTBALL: 8,
}
SPORT_BY_ID: dict[int, Sport] = {value: key for key, value in SPORT_IDS.items()}

#: SX ``leagueLabel`` -> canonical league, per sport, matched exactly.
LEAGUE_BY_LABEL: dict[Sport, dict[str, str]] = {
    Sport.BASEBALL: {"MLB": "MLB"},
    Sport.BASKETBALL: {"NBA": "NBA", "WNBA": "WNBA"},
    Sport.HOCKEY: {"NHL": "NHL"},
    Sport.FOOTBALL: {"NFL": "NFL"},
    Sport.SOCCER: {
        "English Premier League": "EPL",
        "Premier League": "EPL",
        "MLS": "MLS",
        "La Liga": "LA_LIGA",
        "Serie A": "SERIE_A",
        "Bundesliga": "BUNDESLIGA",
        "Ligue 1": "LIGUE_1",
    },
}

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


#: The sports :data:`MARKET_TYPES` can actually read.
#:
#: SX Bet's ``type`` is **not** a global vocabulary, and the table above holds
#: only the US-sport codes.  A census of 1,704 live market records:
#:
#: * baseball, basketball, football — ``226``/``28``/``342``, as mapped.
#: * soccer — ``1`` (Tie/Not tie), ``2`` (totals), ``3`` (handicap), ``52`` (a
#:   two-way "12", i.e. draw-loses).
#: * tennis — ``52`` (match winner), ``166`` (total games), ``201`` (games
#:   handicap), ``866`` (set handicap).
#:
#: None of the soccer or tennis codes is mapped, so the adapter fetched nine
#: soccer and nine tennis market pages, discarded every record, and requested no
#: order books for either — while ``capabilities()`` declared **18 leagues** and
#: the parse produced rows for **three**.  A declared-but-empty league is worse
#: than an absent one: it is a promise the coverage check then cannot audit,
#: because a ``(source, sport)`` pair with no rows never enters the comparison.
#:
#: Narrowed to the truth rather than widened to the promise.  Widening needs
#: care that belongs in its own change: ``52`` is the match winner in tennis and
#: a *draw-loses* two-way in soccer, so one table without a sport key would file
#: a contract that loses on a draw as a moneyline — the phantom this pipeline
#: exists to avoid.
READABLE_SPORTS: frozenset[Sport] = frozenset(sport for sport, _ in MARKET_TYPES)

DEFAULT_LEAGUES: tuple[str, ...] = (
    "MLB", "WNBA", "NBA", "NHL", "HOCKEY_OTHER", "NFL",
    "ATP", "WTA", "ATP_CHALLENGER", "ITF", "TENNIS_OTHER",
)

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    sport: frozenset(
        rule.market for (other, _), rule in MARKET_TYPES.items() if other is sport
    )
    for sport in READABLE_SPORTS
}


def markets_endpoint(sport: Sport, page: int) -> str:
    return f"markets:{sport.value}:{page:02d}"


def orders_endpoint(sport: Sport, batch: int) -> str:
    return f"orders:{sport.value}:{batch:02d}"


class SxBetAdapter:
    """Collects SX Bet's resting order book for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        keys: list[str] = []
        for key in leagues:
            league_registry.league(key)
            if key not in keys:
                keys.append(key)
        if not keys:
            raise ValueError("SxBetAdapter needs at least one league to collect")
        self._leagues: tuple[str, ...] = tuple(keys)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        self._http = SourceClient(source_key, timeout=timeout, client=client)

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    @property
    def sports(self) -> tuple[Sport, ...]:
        """The sports actually fetched.

        Narrowed by :data:`READABLE_SPORTS`, not only by ``SPORT_IDS``.  The two
        diverged when the market table was keyed by sport: ``capabilities()``
        stopped claiming soccer and the fetch loop kept asking for it, so the
        adapter spent **9 metadata pages and 466 KB — 24% of its whole pass** on
        a sport whose every record ``_collectable_hashes`` then discarded, and
        requested no order books for it at all.  Somebody else's bandwidth, for
        rows that cannot exist.
        """
        wanted = {league_registry.league(key).sport for key in self._leagues}
        return tuple(
            sport
            for sport in Sport
            if sport in wanted and sport in SPORT_IDS and sport in READABLE_SPORTS
        )

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {
                key: MARKETS_BY_SPORT[league_registry.league(key).sport]
                for key in self._leagues
                if league_registry.league(key).sport in READABLE_SPORTS
            },
            self._leagues,
        )

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Market metadata per sport, then the order book for those markets.

        Two stages because the venue splits them: ``markets/active`` carries no
        prices at all, so a market list on its own is a list of things nobody has
        offered anything on.

        *tier* is a real saving here, and the only source where it changes the
        row count rather than the request count alone.  The order book is fetched
        by market hash in batches, so every alternate line is a share of a
        request: a full pass over the whole alternate ladder was 135 requests and
        7.6 MB, against 19 requests for the main lines alone.  ``core`` therefore
        asks for main lines only, and :meth:`capabilities` is unchanged because
        the *markets* are the same — it is the ladder that is deferred, not the
        moneyline.
        """
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for sport in self.sports:
            tally.requested(sport.value)
            # Appended into the caller's list as each request lands, not gathered
            # locally and handed over only if the whole scope succeeds.
            #
            # Five sibling adapters were changed to do exactly this, each with the
            # note "4 of 6 requests thrown away on a run that reported OK"; this
            # was the one that still gathered locally.  Measured: an order-book
            # batch answering 403 discarded all eleven of that sport's successful
            # captures — 12 of 24 responses reached the parser and the raw store —
            # and because the refusal is swallowed here rather than raised, the
            # collector's refusal-persisting path never ran either, so the sport
            # left nothing on disk to diagnose the 403 from.  Those pages are
            # valid, already paid for, and inside the observation window.
            first = len(raws)
            hashes: set[str] = set()
            try:
                # ``into=raws``, not ``raws.extend(self._fetch_markets(...))``.
                # The metadata half was still gathering locally and handing over
                # only on success, so a refusal on page ten discarded the nine
                # pages before it — the very shape the orders half was changed
                # to stop, in the same method, one line down.
                self._fetch_markets(sport, tier, into=raws)
                pages = raws[first:]
                # Only ask for the order book of markets a row could come from.
                # Every hash is a share of a request, and the metadata already
                # says which markets are out of scope, suspended, or on a game
                # that has started — fetching those is paid for in somebody
                # else's bandwidth for data that is discarded on arrival.
                hashes = _collectable_hashes(pages)
                self._fetch_orders(sport, sorted(hashes), into=raws)
            except SourceError as exc:
                log.warning(
                    "%s: %s failed after %d response(s), which are kept: %s",
                    self._source_key, sport.value, len(raws) - first, exc,
                )
                tally.failed(sport.value, exc)
                continue
            tally.produced(sport.value, len(hashes))
        tally.require_something(what="active market")
        return raws

    def _fetch_markets(
        self, sport: Sport, tier: Tier, *, into: list[RawResponse] | None = None
    ) -> list[RawResponse]:
        """Every metadata page of one sport, appended to *into* as they arrive.

        Appending as it goes rather than returning at the end is what lets a
        failure part-way through keep the pages already fetched; see the caller.
        """
        pages: list[RawResponse] = [] if into is None else into
        pagination: str | None = None
        for page in range(1, MAX_PAGES_PER_SPORT + 1):
            params = {
                "chainVersion": CHAIN_VERSION,
                "sportIds": str(SPORT_IDS[sport]),
                "onlyMainLine": "false" if tier.includes_depth else "true",
            }
            if pagination:
                params["paginationKey"] = pagination
            raw = self._http.get(
                f"{self.base_url}/markets/active",
                endpoint=markets_endpoint(sport, page),
                params=params,
            )
            payload = require_mapping(
                raw.json(), source=self._source_key, endpoint=raw.endpoint
            )
            data = payload.get("data") or {}
            markets = data.get("markets") or []
            # Kept even when empty: an empty first page is what a renamed field
            # looks like, and it is the only evidence of that.
            pages.append(raw)
            if not markets:
                break
            pagination = data.get("nextKey") or None
            if not pagination:
                break
        else:
            # Falling off the cap with the venue's cursor still live is a
            # truncated slate, and reporting the scope as fully produced makes it
            # invisible.  ``CoverageCappedError`` exists for exactly this — see
            # its docstring: "A bound on request volume is politeness and stays;
            # reporting the run as complete afterwards is not." Pinnacle's
            # league-index fallback was the only place doing it.
            if pagination and self.last_fetch is not None:
                # Filed under the scope's own name; a decorated one would be a
                # scope the book was never asked for — see the sibling adapters.
                self.last_fetch.truncated(
                    sport.value,
                    CoverageCappedError(
                        f"{self._source_key}: {sport.value} still had pages when the "
                        f"{MAX_PAGES_PER_SPORT}-page cap was reached; the rest were not collected"
                    ),
                )
        return pages

    def _fetch_orders(
        self,
        sport: Sport,
        hashes: Sequence[str],
        *,
        into: list[RawResponse] | None = None,
    ) -> list[RawResponse]:
        """One ``orders`` response per batch of market hashes, splitting a batch
        that comes back at the venue's page cap.

        ``/orders`` returns at most :data:`ORDERS_PAGE_CAP` records and says so
        nowhere: it carries no cursor, no total and no truncation flag.  The two
        captured responses were the evidence — one asked for 25 market hashes
        and one for 23, and **both** came back with exactly 100 orders covering
        23 markets.  The two hashes that fell off were the run-line markets on
        CLE@CIN and SEA@LAD, and their four sides were filed as
        ``no_resting_order_for_outcome``, a skip whose own comment reads "Nobody
        is offering this side.  Ordinary on a thin order book" — so an API page
        limit was recorded as a fact about the venue's liquidity, and validation
        then faulted the venue for the market being absent.

        Splitting on the cap rather than just lowering the batch size: a smaller
        batch makes truncation less likely and cannot rule it out, since how
        many orders rest on a market is not knowable in advance.  Extra requests
        are spent only where the cap was actually hit.
        """
        # The caller's list when it gives one, so a batch that fails partway
        # leaves the batches that already landed in the run rather than
        # discarding them with a local accumulator.
        raws: list[RawResponse] = [] if into is None else into
        pending: list[tuple[Sequence[str], int]] = [
            (hashes[index : index + ORDER_BATCH], index // ORDER_BATCH + 1)
            for index in range(0, len(hashes), ORDER_BATCH)
        ]
        page = len(pending)
        while pending:
            batch, label = pending.pop(0)
            raw = self._http.get(
                f"{self.base_url}/orders",
                endpoint=orders_endpoint(sport, label),
                params={
                    "marketHashes": ",".join(batch),
                    "chainVersion": CHAIN_VERSION,
                },
            )
            raws.append(raw)
            if len(batch) > 1 and _at_page_cap(raw):
                # Halve and ask again.  A single hash that still hits the cap
                # is genuinely more than one page of orders on one market; it
                # stops there rather than looping, and the parser counts it —
                # see ``orders_page_cap_reached`` below, which this docstring
                # used to promise without anything producing it.
                half = len(batch) // 2
                for piece in (batch[:half], batch[half:]):
                    page += 1
                    pending.append((piece, page))
        return raws

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_sxbet(raws)

    def close(self) -> None:
        self._http.close()


def _at_page_cap(raw: RawResponse) -> bool:
    """Did this ``orders`` response come back exactly full?"""
    try:
        payload = raw.json()
    except Exception:  # noqa: BLE001 - a body that will not parse is not a cap
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return isinstance(data, list) and len(data) >= ORDERS_PAGE_CAP


def _requested_hashes(raw: RawResponse) -> set[str]:
    """Market hashes this ``orders`` response was asked about.

    Read back out of the captured request parameters, so the parser can tell
    "this market has no resting orders" from "this market was never in the
    answer" without leaving the bytes — ``parse`` stays a pure function of the
    capture.
    """
    stated = (raw.request_params or {}).get("marketHashes") or ""
    return {piece for piece in str(stated).split(",") if piece}


def _collectable_hashes(pages: Sequence[RawResponse]) -> set[str]:
    """Market hashes worth asking for an order book on.

    A market that is out of scope, suspended, or on a game that has already
    started cannot produce a row, and its order book is a request nobody needed.
    Judged against the moment its own page was captured, not against a clock, so
    the decision is the same on a replay.

    "Out of scope" has to mean the same thing here as it does in the parser, or
    the saving is only partial.  Two of the parser's tests were missing: whether
    the market's league resolves at all, and whether the sport collects that
    market at all.

    Both are reachable.  The captured baseball page carries 14 KBO markets of 99
    and baseball has no ``CATCH_ALL`` entry, so none of them can produce a row;
    they cost nothing *on that capture* only because Korean games had already
    started by the time it was taken, and the start check caught them first.
    Captured earlier in the day they would have been 14 wasted order books.  The
    market test is unconditional: ``MARKETS_BY_SPORT`` restricts tennis to the
    moneyline, so every tennis spread and total on the page is fetched and then
    discarded.
    """
    wanted: set[str] = set()
    for page in pages:
        for market in _markets_in(page):
            market_hash = str(market.get("marketHash") or "")
            if not market_hash:
                continue
            sport = SPORT_BY_ID.get(market.get("sportId"))
            if sport is None:
                continue
            rule = MARKET_TYPES.get(
                (sport, market["type"]) if isinstance(market.get("type"), int) else (sport, -1)
            )
            if rule is None:
                continue
            if _league_for(sport, str(market.get("leagueLabel") or "")) is None:
                continue
            if rule.market not in MARKETS_BY_SPORT.get(sport, frozenset()):
                continue
            if str(market.get("status") or "") != _ACTIVE:
                continue
            if market.get("isQuarterLineMarket"):
                continue
            starts = parse_epoch_time(market.get("gameTime"))
            if starts is None or starts <= page.fetched_at:
                continue
            wanted.add(market_hash)
    return wanted


def _markets_in(raw: RawResponse) -> list[Mapping[str, Any]]:
    try:
        payload = raw.json()
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    markets = data.get("markets") if isinstance(data, dict) else None
    return [m for m in (markets or []) if isinstance(m, dict)]


# ── parsing (pure) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Offer:
    """The best price a taker can get on one outcome of one market."""

    decimal_odds: float
    max_stake: float
    raw: RawResponse
    """The ``orders`` response this price was read from.

    Carried on the offer rather than looked up later because it is the only
    place that knows it.  Every row used to cite the ``markets`` response
    instead — the one this module's own docstring says "carries no prices at
    all" — so ``raw_ref`` pointed at bytes the price is not in, and
    ``observed_at`` was the metadata fetch, systematically early by the whole
    metadata-to-orders gap of a 135-request pass.  That timestamp feeds the
    stale-leg gate and the observation window."""


def parse_sxbet(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Turn captured SX Bet responses into normalized rows."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    selected = latest_per_endpoint(latest_capture(raws))

    markets: dict[str, Mapping[str, Any]] = {}
    orders: list[tuple[Mapping[str, Any], RawResponse]] = []
    truncated: set[str] = set()
    for raw in selected:
        kind = raw.endpoint.partition(":")[0]
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: expected a JSON object"
            )
        if kind == "markets":
            for market in _markets_in(raw):
                market_hash = str(market.get("marketHash") or "")
                if not market_hash:
                    # Counted, not dropped silently: docs/INPUT_CONTRACT.md
                    # requires every discarded record to land in a bucket, and
                    # three paths in this parser used a bare ``continue``.
                    outcome.skipped["market_without_a_hash"] += 1
                    continue
                if market_hash in markets:
                    outcome.skipped["duplicate_market_hash"] += 1
                    continue
                markets[market_hash] = market
        elif kind == "orders":
            answered: set[str] = set()
            for order in payload.get("data") or []:
                if isinstance(order, dict):
                    orders.append((order, raw))
                    answered.add(str(order.get("marketHash") or ""))
                else:
                    outcome.skipped["order_not_an_object"] += 1
            if _at_page_cap(raw):
                # A response at the page cap was cut off, and the markets it
                # never mentioned are unknown rather than empty.  Recorded so
                # the rows below can say which, instead of filing an API limit
                # as "nobody is offering this side".
                truncated |= _requested_hashes(raw) - answered
            # ...and a hash a *later* response answered in full is known again.
            # ``_fetch_orders`` splits an over-cap batch and re-asks, so the
            # same hash appears in both the truncated response and a short one;
            # without this, a side that genuinely has no resting order on such a
            # market was counted ``orders_response_truncated`` for good.
            else:
                truncated -= _requested_hashes(raw)
            if _at_page_cap(raw) and len(_requested_hashes(raw)) == 1:
                # One market with more than a page of resting orders: the split
                # has nowhere left to go, so the best offer below is taken from
                # a page that was cut off.  Conservative — a better price may be
                # sitting past the cap — but it must not be silent.
                outcome.skipped["orders_page_cap_reached"] += 1
        else:
            outcome.skipped[f"unrecognised_response:{kind}"] += 1

    if not markets:
        raise FormatChangeError(
            f"{source}: no market metadata among {[raw.endpoint for raw in selected]}, "
            "so an order book cannot be attached to anything"
        )

    best = _best_offers(orders, outcome)

    # Pass 1: fixtures, from the market metadata (every market of a game repeats
    # the same two team names, so any one of them establishes the fixture).
    fixtures: dict[str, Fixture] = {}
    captured_at = min(raw.fetched_at for raw in selected)
    for market in markets.values():
        event_id = str(market.get("sportXeventId") or "")
        if not event_id or event_id in fixtures:
            continue
        fixture = _accept_fixture(market, source, captured_at, outcome)
        if fixture is not None:
            fixtures[event_id] = fixture

    if not fixtures:
        return outcome

    event_keys = resolve_doubleheaders(
        {event_id: (f.base_key, f.commence_time) for event_id, f in fixtures.items()}
    )

    for market_hash, market in sorted(markets.items()):
        fixture = fixtures.get(str(market.get("sportXeventId") or ""))
        if fixture is None:
            outcome.skipped["market_on_out_of_scope_event"] += 1
            continue
        _parse_market(
            market=market,
            market_hash=market_hash,
            best=best,
            identity_raw=_raw_for(selected, market),
            truncated=market_hash in truncated,
            source=source,
            fixture=fixture,
            event_key=event_keys[fixture.event_id],
            outcome=outcome,
        )

    drop_duplicate_selections(source, outcome)
    return outcome


def _raw_for(selected: Sequence[RawResponse], market: Mapping[str, Any]) -> RawResponse:
    """The metadata response a market came from, for provenance."""
    market_hash = str(market.get("marketHash") or "")
    for raw in selected:
        if raw.endpoint.startswith("markets:") and any(
            str(entry.get("marketHash")) == market_hash for entry in _markets_in(raw)
        ):
            return raw
    return selected[0]


def _best_offers(
    orders: Iterable[tuple[Mapping[str, Any], RawResponse]], outcome: ParseOutcome
) -> dict[tuple[str, int], _Offer]:
    """Best takeable price and size per ``(market hash, taker outcome)``.

    ``percentageOdds`` is the **maker's** implied probability, so a taker's is
    its complement — see this module's docstring for the measurement that settles
    which way round it is.  The best offer for a taker is the one with the
    *lowest* implied probability, which is the highest decimal price.
    """
    best: dict[tuple[str, int], _Offer] = {}
    for order, raw in orders:
        if str(order.get("orderStatus") or "") != _ACTIVE:
            outcome.skipped["order_not_active"] += 1
            continue
        if str(order.get("baseToken") or "") != USDC_ADDRESS:
            # A limit denominated in the venue's own token is not comparable
            # with the dollar figures every other source states.
            outcome.skipped["order_not_in_usdc"] += 1
            continue
        try:
            maker_implied = int(order["percentageOdds"]) / ODDS_SCALE
            total = int(order["totalBetSize"])
            filled = int(order.get("fillAmount") or 0) + int(order.get("pendingFillAmount") or 0)
        except (KeyError, TypeError, ValueError):
            outcome.skipped["order_unreadable"] += 1
            continue
        taker_implied = 1.0 - maker_implied
        remaining = total - filled
        if not (0.0 < taker_implied < 1.0) or remaining <= 0 or maker_implied <= 0.0:
            outcome.skipped["order_exhausted_or_degenerate"] += 1
            continue

        # Both sides put money in a pot and the winner takes it, so the stakes
        # sit in the ratio of the two implied probabilities.
        max_stake = (remaining / 10**USDC_DECIMALS) * (taker_implied / maker_implied)
        taker_outcome = 2 if order.get("isMakerBettingOutcomeOne") else 1
        key = (str(order.get("marketHash") or ""), taker_outcome)
        offer = _Offer(
            decimal_odds=1.0 / taker_implied, max_stake=max_stake, raw=raw
        )
        current = best.get(key)
        if current is None or offer.decimal_odds > current.decimal_odds:
            best[key] = offer
    return best


def _accept_fixture(
    market: Mapping[str, Any],
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> Fixture | None:
    event_id = str(market.get("sportXeventId") or "")
    sport = SPORT_BY_ID.get(market.get("sportId"))
    if sport is None:
        outcome.skipped[f"sport_unmapped:{market.get('sportId')}"] += 1
        return None
    league_key = _league_for(sport, str(market.get("leagueLabel") or ""))
    if league_key is None or not league_registry.is_known(league_key):
        outcome.skipped[f"league_unmapped:{sport.value}:{market.get('leagueLabel')}"] += 1
        return None
    competition = league_registry.league(league_key)

    home_name = str(market.get("teamOneName") or "").strip()
    away_name = str(market.get("teamTwoName") or "").strip()
    if not home_name or not away_name:
        outcome.skipped["market_without_two_teams"] += 1
        return None
    if any(is_pairing(part, competition.sport) for part in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    # A women's, reserve or youth competition whose *name* carries the marker
    # while the team names do not — see :func:`src.participants.competition_marker`.
    marker = competition_marker(str(market.get("leagueLabel") or ""), competition.sport)
    home_name = with_marker(home_name, marker)
    away_name = with_marker(away_name, marker)
    home = canonical_participant(home_name, competition)
    away = canonical_participant(away_name, competition)
    if home is None or away is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id} in {competition.key}: {home_name!r} -> "
            f"{home.key if home else None}, {away_name!r} -> {away.key if away else None}",
            event_id=event_id,
        )
        return None

    commence_time = parse_epoch_time(market.get("gameTime"))
    if commence_time is None:
        outcome.reject(
            source,
            "missing_commence_time",
            f"event {event_id} has unreadable gameTime {market.get('gameTime')!r}",
            event_id=event_id,
        )
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    # ``teamOneName`` is the competitor every outcome index is stated against, so it
    # is what ``book_home_key`` carries below — and it is not always our ``home``.
    # For a team sport it is: 16 of 16 NFL fixtures agreed with Bovada's explicit
    # home flag, with no disagreements.  For **tennis** there is no home player, so
    # orient() imposes an ordering by participant key and SX's ordering is its own;
    # the two disagree on about half of all matches.
    #
    # Reading outcome one as "home" there does not lose a row, it attaches the price
    # to the wrong player — and nothing downstream can see it.  The two books agree
    # on the participants, so the fixture check is silent; each source's own overround
    # is normal, so the self-pricing check is silent; and validation deliberately does
    # not compare home/away orientation for tennis because there is nothing to
    # compare.  What comes out is a position whose two legs back the same player,
    # reported as a guaranteed profit.  ``Fixture.our_side`` is the translation.
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


def _league_for(sport: Sport, label: str) -> str | None:
    """The canonical league for one of SX's own labels.

    Matched case-insensitively.  The table was compared exactly, and the sports
    without a ``CATCH_ALL`` entry — baseball, basketball, football — have no
    fallback at all, so a relabelling as small as ``MLB`` to ``mlb`` would drop
    every row of that sport into a counted skip while the source kept producing
    rows for the others.  Nothing would fire: ``empty_after_parse`` needs the
    whole source to go quiet.
    """
    lowered = label.strip().casefold()
    for known, canonical in LEAGUE_BY_LABEL.get(sport, {}).items():
        if known.casefold() == lowered:
            return canonical
    if sport is Sport.TENNIS:
        found = _tennis_league(label)
        if found is not None:
            return found
    return CATCH_ALL.get(sport)


def _parse_market(
    *,
    market: Mapping[str, Any],
    market_hash: str,
    best: Mapping[tuple[str, int], _Offer],
    identity_raw: RawResponse,
    truncated: bool,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    market_type = market.get("type")
    rule = MARKET_TYPES.get(
        (fixture.sport, market_type) if isinstance(market_type, int) else (fixture.sport, -1)
    )
    if rule is None:
        outcome.skipped[
            f"market_type_out_of_scope:{fixture.sport.value}:{market_type}"
        ] += 1
        return
    if rule.market not in MARKETS_BY_SPORT.get(fixture.sport, frozenset()):
        outcome.skipped[f"market_out_of_scope:{fixture.sport.value}:{rule.market.value}"] += 1
        return
    if str(market.get("status") or "") != _ACTIVE:
        outcome.skipped[f"market_status:{market.get('status')}"] += 1
        return
    if market.get("isQuarterLineMarket"):
        # A quarter line splits the stake across two neighbouring numbers, which
        # no single ``line`` on a row can represent.
        outcome.skipped["quarter_line_splits_the_stake"] += 1
        return

    line: float | None = None
    if rule.market in MARKETS_REQUIRING_LINE:
        try:
            line = float(market["line"])
        except (KeyError, TypeError, ValueError):
            outcome.reject(
                source,
                "market_without_line",
                f"{rule.market.value} market {market_hash} carries no readable line",
                event_id=fixture.event_id,
            )
            return

    # Outcome one is *SX's* first-named competitor, and "over" for a total; the
    # line the metadata states is from outcome one's perspective.
    #
    # Which of ours that competitor is has to be resolved rather than assumed:
    # for tennis, orient() imposed an ordering by participant key and SX's own
    # ordering disagrees about half the time.
    if rule.market is Market.TOTAL:
        sides = [(1, Selection.OVER, line), (2, Selection.UNDER, line)]
    else:
        one = fixture.our_side(Selection.HOME)
        two = fixture.our_side(Selection.AWAY)
        sides = [
            (1, one, line),
            (2, two, None if line is None else -line),
        ]

    is_alternate = market.get("mainLine") is False
    for index, selection, own_line in sides:
        offer = best.get((market_hash, index))
        if offer is None:
            if truncated:
                # Not the same fact.  This market was asked about in a response
                # that came back at the venue's page cap and was never mentioned
                # in it, so whether anyone is offering this side is unknown —
                # and calling it empty made an API limit look like the venue's
                # liquidity, right down to validation faulting the venue for the
                # market being absent.
                outcome.skipped["orders_response_truncated"] += 1
                continue
            # Nobody is offering this side.  Ordinary on a thin order book, and
            # the whole reason an exchange row is not the same thing as a book's
            # posted price.
            outcome.skipped["no_resting_order_for_outcome"] += 1
            continue
        # An extreme is not corruption on an order-driven venue.  A resting
        # offer at 99.99% or at a tenth of a percent is a real order somebody
        # placed, unlike a sportsbook's posted price where a number outside the
        # band is a units error.  Counted out of scope rather than rejected: a
        # rejection marks the whole source unhealthy for a market working
        # normally, which is what took Smarkets down on its first live run.
        if not is_plausible_decimal_odds(offer.decimal_odds):
            outcome.skipped["price_outside_the_plausible_band"] += 1
            continue
        # This price's own response, not the metadata one.  ``identity_raw``
        # names the ``markets`` response that established the fixture, exactly
        # as Pinnacle separates its matchups response from its markets one.
        price_raw = offer.raw
        # The started-game gate again, against **this price's own** response.
        #
        # The fixture-level check uses the earliest response in the capture,
        # which is the moment the pass began, while the price is read from an
        # ``orders`` response fetched much later — a full pass here is 135
        # requests.  This is the identical defect fixed for Smarkets, one
        # adapter over, and it matters more here because this venue keeps a
        # market ACTIVE in play: 19 of the 99 captured baseball markets had a
        # ``gameTime`` before their own page's ``fetched_at`` and every one of
        # them said ``status: "ACTIVE"``, so the timestamp is the only defence
        # and there is no liveness flag to fall back on.
        if fixture.commence_time <= price_raw.fetched_at:
            outcome.skipped["event_already_started"] += 1
            continue
        try:
            outcome.quotes.append(
                priced_quote(
                    fixture,
                    source=source,
                    raw=price_raw,
                    event_key=event_key,
                    identity_raw_ref=identity_raw.ref,
                    market=rule.market,
                    period=rule.period,
                    selection=selection,
                    line=None if own_line is None else own_line + 0.0,
                    is_alternate=is_alternate,
                    decimal_odds=offer.decimal_odds,
                    american_odds=decimal_to_american(offer.decimal_odds),
                    implied_probability=implied_probability(offer.decimal_odds),
                    source_market_id=market_hash,
                    source_selection_id=str(index),
                    limit_amount=offer.max_stake if offer.max_stake > 0 else None,
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
