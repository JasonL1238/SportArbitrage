"""Novig exchange adapter — credentialed, and **deliberately unregistered**.

Novig is a peer-to-peer exchange whose developer API is OAuth-gated:
credentials must be requested from Novig before any endpoint answers.  Per the
operator's decision this adapter ships without a registry entry — a registered
key demands a committed fixture and no sanctioned path to a genuine capture
exists until credentials do.  The key is reserved in
:data:`src.sources.registry.CREDENTIALED_SOURCE_KEYS`.

Credentials are ``ODDS_NOVIG_CLIENT_ID`` / ``ODDS_NOVIG_CLIENT_SECRET`` via
:mod:`src.settings` — environment only, never descriptor config, never a query
parameter.  Construction succeeds without them; :meth:`fetch_raw` refuses with
:class:`~src.sources.guards.LoginRequiredError` before opening a socket.  The
token exchange is never captured, for the same reason ProphetX's is not: the
response body is the secret.

API surface, measured from https://docs.novig.com on 2026-08-09:

- Production base ``https://api.novig.us`` (QA: ``https://api-qa.novig.us``).
- ``POST /nbx/v1/auth/emm-token`` with ``grant_type=client_credentials``,
  ``client_id``, ``client_secret`` → bearer token, 30-minute lifetime.  The
  docs show the three fields without naming the encoding; this adapter sends
  the RFC 6749 default (form-encoded) and the first credentialed session must
  confirm it.
- ``GET /nbx/v2/emm/events?league=&status=&limit=&offset=`` → events with a
  UUID ``id``, ``league``, ``scheduledStart`` (ISO 8601),
  ``game.homeTeam.name`` / ``game.awayTeam.name`` and a status enum whose
  pregame value is ``OPEN_PREGAME``.  512 requests/second permitted.
- ``GET /nbx/v2/emm/markets/open?league=&marketType=`` → open markets with
  ``type`` (``MONEY`` / ``SPREAD`` / ``TOTAL`` / …), ``eventId``, ``strike``
  and ``outcomes`` whose ``last`` is the last *trade* — a price history fact,
  **not** a takeable price, so it is never published as one here.
- ``GET /nbx/v2/emm/book/{marketId}?currency=CASH`` → per-outcome ladders of
  resting **bids** in price–time priority, each a decimal probability to three
  places with a ``qty`` in "Minimum Currency Units".  128 requests/second.

The takeable price on this venue is *derived*: only bids rest on the book, so
buying outcome A means matching a resting bid on the complementary outcome B,
and A's price is ``1 − best_bid(B)``.  That derivation is only sound when a
market has exactly two outcomes; multi-way markets are skipped until a genuine
capture shows how the venue represents them.  The fees page defines the bid
``qty`` unit — "API ``qty`` field uses 100 qty = 1 contract", each contract
paying $1.00 — so the stake available at the derived price is
``(qty / 100) × (1 − bid)`` dollars, summed over the resting orders at the
best price (taking more walks the ladder to worse prices).  The same page
settles the fee question for this pipeline's scope: straight-contract fees are
charged **only on fills matched while the event is live** (``OPEN_INGAME``),
taker-side, so a pregame fill carries no fee at all — the eventual
``COMMISSIONS`` entry at registration time must encode pregame-zero rather
than copying the live coefficient.

No genuine payload has been captured, so every shape below is the documented
one, resolved defensively where the docs are silent; the first capture
supersedes it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

from src import leagues as league_registry
from src import settings
from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
from src.normalize import (
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import Participant, resolve_roster
from src.raw_store import RawResponse
from src.schema import Market, Period, Quote, QuoteStatus, Selection
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
    within_schedule_horizon,
)
from src.sources.base import ParseOutcome
from src.sources.guards import (
    CoverageCappedError,
    LoginRequiredError,
    SourceError,
    require_mapping,
)

log = logging.getLogger(__name__)

SOURCE_KEY = "novig"

DEFAULT_BASE_URL = "https://api.novig.us"

#: QA environment, for the first credentialed session; QA credentials are
#: issued separately and QA bytes cannot satisfy the acceptance bar.
QA_BASE_URL = "https://api-qa.novig.us"

TOKEN_PATH = "/nbx/v1/auth/emm-token"

#: Novig's ``league`` parameter uses the same spellings as this repository's
#: canonical keys for every league both sides know, so the mapping is identity
#: — kept as an explicit set so an unsupported key fails at construction
#: rather than as a silent empty scope.
SUPPORTED_LEAGUES: frozenset[str] = frozenset({"MLB", "NFL", "NBA", "NHL", "WNBA"})
DEFAULT_LEAGUES: tuple[str, ...] = ("MLB", "NFL", "NBA", "NHL")

#: Game markets in scope, in the venue's vocabulary.
_MARKET_TYPES: Mapping[str, Market] = {
    "MONEY": Market.MONEYLINE,
    "SPREAD": Market.SPREAD,
    "TOTAL": Market.TOTAL,
}

#: Events page size — the documented maximum, so a slate needs few pages.
EVENTS_PAGE_LIMIT = 100
MAX_EVENT_PAGES = 5

#: Order-book requests per league per pass.  A bound on request volume is
#: politeness and stays; reporting the run as complete afterwards is not — a
#: league that hits this cap files a :class:`CoverageCappedError` truncation
#: so the leftover is visible.  Sized generously: a full MLB day is ~15 games
#: × 3 game markets = 45 books.
MAX_BOOKS_PER_LEAGUE = 60

#: Politeness floor.  The venue permits far more; there is no reason to use it.
HOST_INTERVAL = 0.25


def _credentials() -> tuple[str, str]:
    """Read at call time so tests can monkeypatch :mod:`src.settings`."""
    return (
        settings.NOVIG_CLIENT_ID.strip(),
        settings.NOVIG_CLIENT_SECRET.strip(),
    )


class NovigAdapter:
    """Collects Novig's open pregame game markets for the configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] = DEFAULT_LEAGUES,
        *,
        source_key: str = SOURCE_KEY,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        client: Any | None = None,
    ) -> None:
        keys: list[str] = []
        for key in leagues:
            league_registry.league(key)
            if key not in keys:
                keys.append(key)
        unknown = [key for key in keys if key not in SUPPORTED_LEAGUES]
        if unknown:
            raise ValueError(
                f"Novig support covers the closed-roster majors only; "
                f"league(s) {unknown} are not among {sorted(SUPPORTED_LEAGUES)}"
            )
        if not keys:
            raise ValueError("NovigAdapter needs at least one league to collect")
        self._leagues: tuple[str, ...] = tuple(keys)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        # One transport client for the uncaptured token POST and the captured
        # GETs; SourceClient treats an injected client as borrowed, so this
        # adapter owns the close.
        if client is None:
            from src.sources.transport import build_default_client

            client = build_default_client(timeout=timeout)
        self._client = client
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=HOST_INTERVAL,
        )
        self.last_fetch: ScopeTally | None = None

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        """Moneyline, spread and total per league, both tiers; see fetch_raw."""
        del tier
        return capabilities_from(
            {
                key: frozenset((Market.MONEYLINE, Market.SPREAD, Market.TOTAL))
                for key in self._leagues
            },
            self._leagues,
        )

    # ── fetch ────────────────────────────────────────────────────────────────

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Each league's pregame events, open markets, and order books.

        *tier* changes nothing: the order-book call is not a deferrable
        enrichment here — it is where the only takeable prices live, so a pass
        without it would collect nothing but untakeable last-trade marks.  The
        per-league book cap above is the request-volume bound instead.

        Refuses before any network I/O when the client pair is not configured.
        """
        del tier
        client_id, client_secret = _credentials()
        if not client_id or not client_secret:
            raise LoginRequiredError(
                f"{self._source_key}: ODDS_NOVIG_CLIENT_ID and "
                "ODDS_NOVIG_CLIENT_SECRET are not both set; Novig's developer "
                "API has no anonymous surface"
            )
        token = self._login(client_id, client_secret)
        auth = {"Authorization": f"Bearer {token}"}

        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for league_key in self._leagues:
            tally.requested(league_key)
            try:
                produced = self._fetch_league(league_key, auth, into=raws, tally=tally)
            except SourceError as exc:
                log.warning(
                    "%s: league %s failed: %s", self._source_key, league_key, exc
                )
                tally.failed(league_key, exc)
                continue
            tally.produced(league_key, produced)
        tally.require_something(what="open pregame market")
        return raws

    def _fetch_league(
        self,
        league_key: str,
        auth: Mapping[str, str],
        *,
        into: list[RawResponse],
        tally: ScopeTally,
    ) -> int:
        """Events, open markets, then one book per in-scope market."""
        event_ids: set[str] = set()
        for page in range(MAX_EVENT_PAGES):
            raw = self._http.get(
                f"{self.base_url}/nbx/v2/emm/events",
                endpoint=f"events:{league_key}:{page:02d}",
                params={
                    "league": league_key,
                    "status": "OPEN_PREGAME",
                    "limit": str(EVENTS_PAGE_LIMIT),
                    "offset": str(page * EVENTS_PAGE_LIMIT),
                },
                headers=auth,
            )
            into.append(raw)
            for entry in _entry_list(raw, source=self._source_key):
                identifier = entry.get("id")
                if isinstance(identifier, str):
                    event_ids.add(identifier)
            # Page-full is judged on the payload's own row count, not on how
            # many rows survived the dict filter — a page carrying malformed
            # rows is still a full page, and ending pagination on it would
            # silently drop the slate behind it.
            if _row_count(raw) < EVENTS_PAGE_LIMIT:
                break
        else:
            # Falling off the cap with the last page still full means the
            # venue had more; a bound on request volume is politeness and
            # stays, but reporting the scope as complete afterwards is not.
            tally.truncated(
                league_key,
                CoverageCappedError(
                    f"{self._source_key}: {league_key} still had full event "
                    f"pages when the {MAX_EVENT_PAGES}-page cap was reached; "
                    "the rest were not collected"
                ),
            )

        markets_raw = self._http.get(
            f"{self.base_url}/nbx/v2/emm/markets/open",
            endpoint=f"markets:{league_key}",
            params={"league": league_key},
            headers=auth,
        )
        into.append(markets_raw)

        # Keyed by market id so a duplicated row cannot fetch its book twice.
        in_scope: dict[str, dict[str, Any]] = {}
        for entry in _entry_list(markets_raw, source=self._source_key):
            if (
                str(entry.get("type") or "") in _MARKET_TYPES
                and isinstance(entry.get("id"), str)
                and isinstance(entry.get("eventId"), str)
                and entry.get("eventId") in event_ids
            ):
                in_scope.setdefault(entry["id"], entry)
        # One vanished market must not cost the league: a market listed by
        # ``markets/open`` can be matched out or closed in the seconds before
        # its book call, and the resulting refusal is churn, not a scope
        # failure — the earlier responses in this very list still parse.  A
        # lost book is recorded as a truncation (the scope answered and then
        # came up short), and only a league whose *every* book call failed is
        # re-raised as the league's failure.
        lost: list[str] = []
        attempted = 0
        for index, market_id in enumerate(in_scope):
            if index >= MAX_BOOKS_PER_LEAGUE:
                tally.truncated(
                    league_key,
                    CoverageCappedError(
                        f"{self._source_key}: {league_key} had {len(in_scope)} open "
                        f"game markets and the {MAX_BOOKS_PER_LEAGUE}-book cap was "
                        "reached; the rest were not collected"
                    ),
                )
                break
            attempted += 1
            try:
                into.append(
                    self._http.get(
                        f"{self.base_url}/nbx/v2/emm/book/{market_id}",
                        endpoint=f"book:{league_key}:{market_id}",
                        params={"currency": "CASH"},
                        headers=auth,
                    )
                )
            except SourceError as exc:
                log.warning(
                    "%s: book %s failed: %s", self._source_key, market_id, exc
                )
                lost.append(f"{market_id}: {exc}")
        if lost:
            if attempted == len(lost):
                raise SourceError(
                    f"{self._source_key}: {league_key}: every one of {attempted} "
                    f"order-book request(s) failed; first: {lost[0]}"
                )
            tally.truncated(
                league_key,
                SourceError(
                    f"{len(lost)} of {attempted} order books were refused or "
                    f"vanished mid-pass; first: {lost[0]}"
                ),
            )
        return len(in_scope)

    def _login(self, client_id: str, client_secret: str) -> str:
        """OAuth client-credentials exchange, capturing nothing.

        Bypasses :class:`SourceClient` deliberately — its job is to persist
        every response, and this body is the secret.  Failures fold to
        :class:`LoginRequiredError` without echoing the body.
        """
        try:
            # RFC 6749's default encoding; the docs show the fields without
            # naming one — first credentialed session confirms.  Encoded here
            # rather than passed as a dict because the transport clients
            # disagree about dicts: curl_cffi form-encodes ``data=``, but the
            # Playwright browser client serializes a dict as JSON, which
            # would silently change the wire format under
            # ``ODDS_FETCH_MODE=browser``.  A pre-encoded string with an
            # explicit content type means every client sends the same bytes.
            response = self._client.post(
                f"{self.base_url}{TOKEN_PATH}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=urlencode(
                    {
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    }
                ),
            )
        except Exception as exc:  # noqa: BLE001 - transport stacks vary
            # AssertionError re-raised alongside the interrupt pair: it is how
            # a test proves no socket was opened, and how a programming error
            # announces itself — neither may be laundered into a "credentials
            # missing" diagnosis.
            if isinstance(exc, (KeyboardInterrupt, SystemExit, AssertionError)):
                raise
            raise LoginRequiredError(
                f"{self._source_key}:auth: {type(exc).__name__} during token exchange"
            ) from exc
        if response.status_code != 200:
            raise LoginRequiredError(
                f"{self._source_key}:auth: token endpoint answered HTTP "
                f"{response.status_code}"
            )
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise LoginRequiredError(
                f"{self._source_key}:auth: token response is not JSON"
            ) from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise LoginRequiredError(
                f"{self._source_key}:auth: token endpoint answered 200 without "
                "an access_token"
            )
        return str(token)

    # ── parse ────────────────────────────────────────────────────────────────

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_novig(raws)

    def close(self) -> None:
        self._http.close()
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


# ── module-level parsing, deterministic and I/O-free ─────────────────────────


def _raw_rows(raw: RawResponse) -> list[Any]:
    """The response's entry array as sent, whether bare or wrapped in ``data``.

    The reference pages describe the DTOs without stating the envelope; both
    plausible shapes are read and anything else is an empty list the caller
    accounts for.
    """
    payload = raw.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "events", "markets", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _row_count(raw: RawResponse) -> int:
    try:
        return len(_raw_rows(raw))
    except ValueError:
        return 0


def _entry_list(raw: RawResponse, *, source: str) -> list[dict[str, Any]]:
    """The well-formed entries: :func:`_raw_rows` narrowed to mappings."""
    del source
    return [entry for entry in _raw_rows(raw) if isinstance(entry, dict)]


class _Fixture:
    """One event's resolved identity, shared by every market under it."""

    __slots__ = ("event_id", "competition", "home", "away", "commence_time", "event_key", "identity_ref")

    def __init__(
        self,
        event_id: str,
        competition: League,
        home: Participant,
        away: Participant,
        commence_time: Any,
        identity_ref: str,
    ) -> None:
        self.event_id = event_id
        self.competition = competition
        self.home = home
        self.away = away
        self.commence_time = commence_time
        self.event_key = ""  # assigned after the doubleheader pass
        self.identity_ref = identity_ref


class _OpenMarket:
    """One open market's metadata, awaiting its order book."""

    __slots__ = ("market_id", "our_market", "event_id", "strike", "outcomes", "raw_ref")

    def __init__(
        self,
        market_id: str,
        our_market: Market,
        event_id: str,
        strike: float | None,
        outcomes: list[dict[str, Any]],
        raw_ref: str,
    ) -> None:
        self.market_id = market_id
        self.our_market = our_market
        self.event_id = event_id
        self.strike = strike
        self.outcomes = outcomes
        self.raw_ref = raw_ref


def parse_novig(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Documented-schema parser.  League identity comes from the envelope's
    endpoint label, as with the Action Network and ProphetX parsers."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    # Newest pass, one response per label: ``events:MLB:00`` is a position
    # counter, so a directory holding two runs would otherwise emit both
    # runs' prices — see ``latest_capture``.
    raws = latest_per_endpoint(latest_capture(raws))

    fixtures: dict[str, _Fixture] = {}
    per_league_events: dict[str, dict[str, tuple[str, Any]]] = {}
    for raw in raws:
        if not raw.endpoint.startswith("events:"):
            continue
        league_key = raw.endpoint.split(":", 2)[1]
        try:
            competition = league_registry.league(league_key)
        except KeyError:
            outcome.reject(
                source,
                "unknown_league_label",
                f"endpoint {raw.endpoint!r} names a league the registry lacks",
            )
            continue
        for entry in _entry_list(raw, source=source):
            _accept_event(
                entry,
                competition,
                raw,
                source=source,
                outcome=outcome,
                fixtures=fixtures,
                per_league=per_league_events.setdefault(league_key, {}),
            )

    for league_key, events in per_league_events.items():
        for event_id, final_key in resolve_doubleheaders(events).items():
            fixtures[event_id].event_key = final_key

    markets: dict[str, _OpenMarket] = {}
    for raw in raws:
        if not raw.endpoint.startswith("markets:"):
            continue
        for entry in _entry_list(raw, source=source):
            our_market = _MARKET_TYPES.get(str(entry.get("type") or ""))
            if our_market is None:
                outcome.skipped[
                    f"market_type_{str(entry.get('type') or 'missing')}"
                ] += 1
                continue
            market_id = entry.get("id")
            event_id = entry.get("eventId")
            if not isinstance(market_id, str) or not isinstance(event_id, str):
                outcome.reject(
                    source,
                    "missing_market_identity",
                    f"open market without string id/eventId: {market_id!r}/{event_id!r}",
                )
                continue
            strike = entry.get("strike")
            outcomes_field = entry.get("outcomes")
            markets[market_id] = _OpenMarket(
                market_id=market_id,
                our_market=our_market,
                event_id=event_id,
                strike=(
                    float(strike)
                    if isinstance(strike, (int, float)) and not isinstance(strike, bool)
                    else None
                ),
                outcomes=[
                    entry_
                    for entry_ in (outcomes_field if isinstance(outcomes_field, list) else [])
                    if isinstance(entry_, dict)
                ],
                raw_ref=raw.ref,
            )

    for raw in raws:
        if not raw.endpoint.startswith("book:"):
            continue
        market_id = raw.endpoint.split(":", 2)[2]
        market = markets.get(market_id)
        if market is None:
            outcome.skipped["book_for_unknown_market"] += 1
            continue
        fixture = fixtures.get(market.event_id)
        if fixture is None:
            outcome.skipped["market_for_unknown_event"] += 1
            continue
        if fixture.commence_time <= raw.fetched_at:
            # Gate against this price's own response: the events call ran
            # earlier in the pass and a game can start in between.
            outcome.skipped["event_already_started"] += 1
            continue
        _parse_book(raw, market, fixture, source=source, outcome=outcome)
    # Storage enforces dedup_key with a UNIQUE constraint whose failure aborts
    # the whole insert; every peer parser guards it here and so does this one.
    drop_duplicate_selections(source, outcome)
    return outcome


def _accept_event(
    entry: Mapping[str, Any],
    competition: League,
    raw: RawResponse,
    *,
    source: str,
    outcome: ParseOutcome,
    fixtures: dict[str, _Fixture],
    per_league: dict[str, tuple[str, Any]],
) -> None:
    event_id = entry.get("id")
    if not isinstance(event_id, str):
        outcome.reject(source, "missing_event_id", "event entry without a string id")
        return
    status = str(entry.get("status") or "")
    if status and status != "OPEN_PREGAME":
        # The fetch asks for OPEN_PREGAME; a stored capture may hold more.
        outcome.skipped["event_not_open_pregame"] += 1
        return
    commence = parse_iso_time(entry.get("scheduledStart"))
    if commence is None:
        outcome.reject(
            source,
            "bad_start_time",
            f"event {event_id}: unparseable scheduledStart "
            f"{entry.get('scheduledStart')!r}",
        )
        return
    if not within_schedule_horizon(commence, raw.fetched_at, competition):
        outcome.skipped["outside_schedule_horizon"] += 1
        return

    game = entry.get("game")
    home_name = away_name = None
    if isinstance(game, dict):
        home_team = game.get("homeTeam")
        away_team = game.get("awayTeam")
        if isinstance(home_team, dict):
            home_name = home_team.get("name")
        if isinstance(away_team, dict):
            away_name = away_team.get("name")
    if not isinstance(home_name, str) or not isinstance(away_name, str):
        # Non-game event types carry no ``game`` block; out of scope.
        outcome.skipped["not_a_game_event"] += 1
        return

    roster = competition.roster or ""
    home = resolve_roster(home_name, roster)
    away = resolve_roster(away_name, roster)
    if home is None or away is None:
        outcome.reject(
            source,
            "unknown_team",
            f"event {event_id}: {away_name!r} @ {home_name!r} did not resolve "
            f"on the {roster!r} roster",
            event_id=event_id,
        )
        return
    away, home = orient(away, home, competition, home=home)

    fixtures[event_id] = _Fixture(
        event_id=event_id,
        competition=competition,
        home=home,
        away=away,
        commence_time=commence,
        identity_ref=raw.ref,
    )
    per_league[event_id] = (
        build_event_key(away.key, home.key, commence, competition),
        commence,
    )


def _best_bid(ladder: Mapping[str, Any]) -> tuple[float, float] | None:
    """The best resting CASH bid as ``(price, qty_at_that_price)``, or ``None``.

    Bids arrive in price–time priority, so the first acceptable entry is the
    best; the quantity aggregates every consecutive resting order **at that
    same price**, because that is what can be matched without walking the
    ladder to a worse level.  ``status`` and ``currency`` are filtered
    defensively: the docs show ``RESTING`` and the fetch asks for CASH, but a
    stored capture is under no obligation to be so tidy.
    """
    bids = ladder.get("bids")
    if not isinstance(bids, list):
        return None
    best: float | None = None
    qty_at_best = 0.0
    for bid in bids:
        if not isinstance(bid, dict):
            continue
        status = str(bid.get("status") or "RESTING")
        if status != "RESTING":
            continue
        currency = str(bid.get("currency") or "CASH")
        if currency != "CASH":
            continue
        price = bid.get("price")
        if (
            not isinstance(price, (int, float))
            or isinstance(price, bool)
            or not 0.0 < float(price) < 1.0
        ):
            continue
        if best is None:
            best = float(price)
        elif float(price) != best:
            break
        qty = bid.get("qty")
        if isinstance(qty, (int, float)) and not isinstance(qty, bool) and qty > 0:
            qty_at_best += float(qty)
    if best is None:
        return None
    return best, qty_at_best


def _parse_book(
    raw: RawResponse,
    market: _OpenMarket,
    fixture: _Fixture,
    *,
    source: str,
    outcome: ParseOutcome,
) -> None:
    payload = require_mapping(raw.json(), source=source, endpoint=raw.endpoint)
    ladders = payload.get("outcomeLadders")
    if not isinstance(ladders, list):
        outcome.reject(
            source,
            "missing_outcome_ladders",
            f"book {market.market_id}: no outcomeLadders array",
        )
        return
    by_outcome: dict[str, Mapping[str, Any]] = {}
    for ladder in ladders:
        if isinstance(ladder, dict) and isinstance(ladder.get("outcomeId"), str):
            by_outcome[ladder["outcomeId"]] = ladder

    # The complement derivation is only sound on a two-outcome market: with
    # three or more, "everyone who bid against A" is not one opposite outcome.
    if len(market.outcomes) != 2:
        outcome.skipped["not_a_two_outcome_market"] += 1
        return

    # ...and only when the two outcomes are actually distinct.  The whole
    # 1 − bid(B) rule hinges on B being the *other* side; a payload whose two
    # outcomes share an id would price each side off its own ladder, and a
    # single venue then fabricates an arbitrage all by itself (two 2.5s on
    # one coin: combined implied probability 0.8).  ``status`` and
    # ``currency`` get defensive filters below for untidy captures — the
    # identity the derivation rests on deserves no less.
    first_id, second_id = (
        market.outcomes[0].get("id"),
        market.outcomes[1].get("id"),
    )
    if first_id == second_id:
        outcome.reject(
            source,
            "duplicate_outcome_id",
            f"book {market.market_id}: both outcomes carry id {first_id!r}",
        )
        return

    for this, other in (
        (market.outcomes[0], market.outcomes[1]),
        (market.outcomes[1], market.outcomes[0]),
    ):
        _parse_outcome(
            this,
            other,
            raw,
            market,
            fixture,
            by_outcome,
            source=source,
            outcome=outcome,
        )


def _parse_outcome(
    this: Mapping[str, Any],
    other: Mapping[str, Any],
    raw: RawResponse,
    market: _OpenMarket,
    fixture: _Fixture,
    by_outcome: Mapping[str, Mapping[str, Any]],
    *,
    source: str,
    outcome: ParseOutcome,
) -> None:
    this_id = this.get("id")
    other_id = other.get("id")
    if not isinstance(this_id, str) or not isinstance(other_id, str):
        outcome.reject(
            source,
            "missing_outcome_id",
            f"book {market.market_id}: outcome without a string id",
        )
        return
    other_ladder = by_outcome.get(other_id)
    if other_ladder is None:
        outcome.skipped["no_ladder_for_outcome"] += 1
        return
    best = _best_bid(other_ladder)
    if best is None:
        # Nobody is resting on the other side, so there is nothing to match
        # against.  Ordinary on a thin book, and the reason an exchange row is
        # not a posted price.
        outcome.skipped["no_resting_order_for_outcome"] += 1
        return
    bid, qty_at_best = best
    takeable = 1.0 - bid
    if takeable <= 0.0:
        outcome.skipped["price_outside_the_plausible_band"] += 1
        return
    decimal_odds = 1.0 / takeable
    if not is_plausible_decimal_odds(decimal_odds):
        # A real order somebody placed, however extreme; out of scope rather
        # than rejected — see the SX Bet adapter's identical stance.
        outcome.skipped["price_outside_the_plausible_band"] += 1
        return

    description = str(this.get("description") or "")
    our_selection, line = _resolve_selection(
        description, market, fixture, source=source, outcome=outcome
    )
    if our_selection is None:
        return

    try:
        outcome.quotes.append(
            Quote(
                source=source,
                observed_at=raw.fetched_at,
                raw_ref=raw.ref,
                identity_raw_ref=fixture.identity_ref,
                sport=fixture.competition.sport,
                league=fixture.competition.key,
                event_key=fixture.event_key,
                source_event_id=fixture.event_id,
                home_participant=fixture.home.key,
                away_participant=fixture.away.key,
                home_team=fixture.home.name,
                away_team=fixture.away.name,
                commence_time=fixture.commence_time,
                market=market.our_market,
                period=Period.FULL_GAME,
                selection=our_selection,
                line=line,
                decimal_odds=decimal_odds,
                american_odds=decimal_to_american(decimal_odds),
                implied_probability=implied_probability(decimal_odds),
                source_market_id=market.market_id,
                source_selection_id=this_id,
                # 100 qty = one $1.00-payout contract (fees page), so the
                # dollars stakeable at this price without walking the ladder
                # are contracts × price.  Confirmed against the docs, still to
                # be verified against the first genuine capture.  A positive
                # amount that rounds below one cent becomes None rather than
                # 0.0: the Quote validator refuses a zero limit, and filing a
                # working extreme-price row as invalid_quote would spend a
                # parser-failure signal on a book behaving normally.
                limit_amount=(
                    limit
                    if (limit := round((qty_at_best / 100.0) * takeable, 2)) >= 0.01
                    else None
                ),
                status=QuoteStatus.ACTIVE,
            )
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            source,
            "invalid_quote",
            f"{market.our_market.value}/{our_selection.value} on "
            f"{fixture.event_id}: {exc}",
            event_id=fixture.event_id,
        )


def _resolve_selection(
    description: str,
    market: _OpenMarket,
    fixture: _Fixture,
    *,
    source: str,
    outcome: ParseOutcome,
) -> tuple[Selection | None, float | None]:
    """Which side this outcome is, and at what line.

    Totals read Over/Under plus the market strike.  Moneylines resolve the
    club named in the description.  Spreads additionally demand a **signed**
    number in the description itself: the market-level ``strike`` is one
    unsigned magnitude for two opposite handicaps, and guessing which side is
    the favourite flips the sign of every wrong guess — so a spread outcome
    that does not state its own signed line is a rejection, not a coin flip.
    """
    if market.our_market is Market.TOTAL:
        lowered = description.lower()
        if "over" in lowered:
            selection: Selection | None = Selection.OVER
        elif "under" in lowered:
            selection = Selection.UNDER
        else:
            outcome.reject(
                source,
                "unrecognised_total_selection",
                f"{fixture.event_id}: total outcome named {description!r}",
                event_id=fixture.event_id,
            )
            return None, None
        if market.strike is None:
            outcome.reject(
                source,
                "missing_line",
                f"{fixture.event_id}: total market {market.market_id} has no strike",
                event_id=fixture.event_id,
            )
            return None, None
        return selection, market.strike

    participant = resolve_roster(description, fixture.competition.roster or "")
    if participant is None:
        outcome.reject(
            source,
            "unknown_team",
            f"{fixture.event_id}: outcome {description!r} resolves to no club",
            event_id=fixture.event_id,
        )
        return None, None
    if participant.key == fixture.home.key:
        selection = Selection.HOME
    elif participant.key == fixture.away.key:
        selection = Selection.AWAY
    else:
        outcome.reject(
            source,
            "selection_not_in_fixture",
            f"{fixture.event_id}: {participant.key} is not a side of this event",
            event_id=fixture.event_id,
        )
        return None, None

    if market.our_market is Market.MONEYLINE:
        return selection, None

    signed = _signed_line(description)
    if signed is None:
        outcome.reject(
            source,
            "spread_sign_unresolved",
            f"{fixture.event_id}: spread outcome {description!r} states no "
            "signed line",
            event_id=fixture.event_id,
        )
        return None, None
    return selection, signed


def _signed_line(description: str) -> float | None:
    """The one explicitly signed handicap in the text, e.g. ``"KC -3.5"``.

    Exactly one, or nothing: a description carrying two signed numbers
    ("Phils -110 -1.5") does not say which is the handicap, and taking the
    first — or the last — is a coin flip wearing a rule's clothes.  The
    ambiguous case rejects upstream under ``spread_sign_unresolved``, which
    is the honest reading until a genuine capture shows the venue's format.
    """
    found: list[float] = []
    for token in description.replace("(", " ").replace(")", " ").split():
        if token[:1] in "+-" and len(token) > 1:
            try:
                found.append(float(token))
            except ValueError:
                continue
    return found[0] if len(found) == 1 else None
