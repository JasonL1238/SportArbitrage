"""ProphetX partner-API adapter — credentialed, and **deliberately unregistered**.

ProphetX is a CFTC-regulated peer-to-peer exchange.  Its partner API is not a
public surface: there is no self-serve key, and access is granted per account
by the ProphetX team.  Per the operator's decision this adapter therefore ships
*without* a registry entry — registering it would demand a committed fixture
(``tests/conftest.py`` requires one per registered key) and no sanctioned path
to a genuine capture exists until credentials do.  The key is reserved in
:data:`src.sources.registry.CREDENTIALED_SOURCE_KEYS` so registration is a
one-step decision once keys and a real capture exist.

Credentials are read from the environment only — ``ODDS_PROPHETX_ACCESS_KEY``
and ``ODDS_PROPHETX_SECRET_KEY`` via :mod:`src.settings` — never from
``SourceDescriptor.config`` (it is committed and copied into kwargs) and never
from a query parameter (``_common.py`` stores the full final URL in the
envelope).  Construction succeeds without them; :meth:`fetch_raw` refuses with
:class:`~src.sources.guards.LoginRequiredError` before opening a socket, so a
missing key surfaces in health as ``login_required`` rather than a mystery
zero.

The login exchange is **never captured**.  Its response body carries the
short-lived access token and the refresh token, and a stored envelope is
exactly the place a secret must not be — so the POST goes through the transport
client directly, bypassing :class:`~src.sources._common.SourceClient`'s
capture-everything path, and the token travels afterwards in a per-request
``Authorization`` header, which envelopes do not persist (they store request
*params* and *response* headers only).

API surface, measured from https://docs.prophetx.co on 2026-08-09:

- Production base ``https://cash.api.prophetx.co/partner``; sandbox
  ``https://api.sandbox.prophetx.dev/partner``.  Sandbox and production
  credentials are separate and must not be reused across environments.
- ``POST /auth/login`` with ``{access_key, secret_key}`` returns
  ``data.access_token`` (documented lifetime 10–20 minutes depending on the
  page) and ``data.refresh_token`` (3 days).  One login per collection pass is
  comfortably inside the shortest stated lifetime.
- ``GET /mm/get_tournaments`` → tournaments; ``GET /mm/get_sport_events
  ?tournament_id=`` → events with ``competitors`` (each carrying ``side``),
  ``scheduled`` (ISO 8601) and ``status``; ``GET /mm/get_multiple_markets
  ?event_ids=`` → markets keyed by event id, selections carrying decimal
  ``odds`` and a ``stake`` liquidity figure.
- The reference pages mark ``/mm/get_markets`` and ``/mm/get_multiple_markets``
  **deprecated** without naming a successor.  The first live session must
  re-check that note; if a versioned replacement exists by then, move.

No genuine payload has ever been captured, so the parser below is written to
the *documented* schema and states its assumptions loudly: field spellings the
docs leave ambiguous are resolved defensively, and anything that does not match
is a rejection rather than a guess.  The first genuine capture supersedes this
file's assumptions and must be committed under ``tests/fixtures/raw/`` before
the source can pass the acceptance bar.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Sequence

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
    envelope_source,
    parse_iso_time,
    within_schedule_horizon,
)
from src.sources.base import ParseOutcome
from src.sources.guards import (
    LoginRequiredError,
    SourceError,
    require_mapping,
)

log = logging.getLogger(__name__)

SOURCE_KEY = "prophetx"

#: Production, from the "switch to production" page (2026-08-09).  Explicit on
#: any future descriptor too — the Action Network default-URL omission is not a
#: mistake this codebase repeats.
DEFAULT_BASE_URL = "https://cash.api.prophetx.co/partner"

#: Sandbox, for the first credentialed session: ProphetX issues sandbox keys
#: first, and sandbox bytes are still genuine bytes for parser development —
#: though only a production capture can satisfy the acceptance bar.
SANDBOX_BASE_URL = "https://api.sandbox.prophetx.dev/partner"

#: The leagues this adapter starts with: the closed-roster majors, because
#: participant resolution rides on :func:`src.participants.resolve_roster` and
#: an open-roster league would need per-sport name handling this file does not
#: yet earn.  WNBA is supported but not default — mirror of the registry's
#: other adapters, which default to the slates the pipeline compares most.
SUPPORTED_LEAGUES: frozenset[str] = frozenset({"MLB", "NFL", "NBA", "NHL", "WNBA"})
DEFAULT_LEAGUES: tuple[str, ...] = ("MLB", "NFL", "NBA", "NHL")

#: Events per ``get_multiple_markets`` request.  The parameter is form-style
#: non-exploded (``event_ids=1,2,3``); ten keeps the URL short of any proxy or
#: CDN line limit while still batching a full slate into a handful of calls.
EVENT_IDS_PER_REQUEST = 10

#: Politeness floor between requests to the venue.  The partner docs state no
#: numeric limit; a quarter second matches the shared default and a full pass
#: here is tens of requests, not hundreds.
HOST_INTERVAL = 0.25

_MARKET_TYPES: Mapping[str, Market] = {
    "moneyline": Market.MONEYLINE,
    "spread": Market.SPREAD,
    "total": Market.TOTAL,
}

#: Tokens in a market ``name`` / ``group_name`` that mark a non-full-game
#: window.  The docs do not enumerate the vocabulary, so this list errs toward
#: skipping: a full-game market wrongly skipped is a visible coverage gap,
#: while a first-half market published as full-game corrupts the comparison
#: silently.  The first genuine capture must confirm or replace this list.
_PERIOD_MARKERS: frozenset[str] = frozenset(
    {
        "half",
        "halves",
        "quarter",
        "inning",
        "innings",
        "period",
        "periods",
        "1st",
        "2nd",
        "3rd",
        "4th",
        "first",
        "second",
        "third",
        "fourth",
    }
)


def _credentials() -> tuple[str, str]:
    """The configured key pair, read at call time so tests can monkeypatch
    :mod:`src.settings` without re-importing this module."""
    return (
        settings.PROPHETX_ACCESS_KEY.strip(),
        settings.PROPHETX_SECRET_KEY.strip(),
    )


class ProphetXAdapter:
    """Collects ProphetX's pregame game markets for the configured leagues."""

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
                f"ProphetX support covers the closed-roster majors only; "
                f"league(s) {unknown} are not among {sorted(SUPPORTED_LEAGUES)}"
            )
        if not keys:
            raise ValueError("ProphetXAdapter needs at least one league to collect")
        self._leagues: tuple[str, ...] = tuple(keys)
        self._source_key = source_key
        self.base_url = base_url.rstrip("/")
        # One transport client serves both the uncaptured login POST and the
        # captured market-data GETs, injected or not.  ``SourceClient`` treats
        # an injected client as borrowed, so this adapter owns the close.
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
        """Moneyline, spread and total per league — the exchange's game markets.

        Claimed for both tiers because there is no deferred per-event call to
        narrow; see :meth:`fetch_raw`.
        """
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
        """Tournaments, then each configured league's events and markets.

        *tier* changes nothing: the market batch is already the smallest
        request shape the API offers, so there is no follow-up to defer.

        Refuses before any network I/O when the key pair is not configured —
        an unregistered-by-default source whose first observable act was an
        unauthenticated request against a partner API would be indistinguishable
        from a probe, and the venue has already said access is by arrangement.
        """
        del tier
        access_key, secret_key = _credentials()
        if not access_key or not secret_key:
            raise LoginRequiredError(
                f"{self._source_key}: ODDS_PROPHETX_ACCESS_KEY and "
                "ODDS_PROPHETX_SECRET_KEY are not both set; ProphetX's partner "
                "API has no anonymous surface"
            )
        token = self._login(access_key, secret_key)
        auth = {"Authorization": f"Bearer {token}"}

        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)

        tournaments_raw = self._http.get(
            f"{self.base_url}/mm/get_tournaments",
            endpoint="tournaments",
            headers=auth,
        )
        raws.append(tournaments_raw)
        by_league = _match_tournaments(
            tournaments_raw, self._leagues, source=self._source_key
        )

        for league_key in self._leagues:
            tally.requested(league_key)
            tournament_ids = by_league.get(league_key, ())
            if not tournament_ids:
                # The venue lists no tournament for this league today.  An
                # empty scope, not a failure: offseason leagues disappear from
                # the tournament list entirely.
                tally.produced(league_key, 0)
                continue
            try:
                event_ids = self._fetch_league(
                    league_key, tournament_ids, auth, into=raws
                )
            except SourceError as exc:
                log.warning(
                    "%s: league %s failed: %s", self._source_key, league_key, exc
                )
                tally.failed(league_key, exc)
                continue
            tally.produced(league_key, len(event_ids))
        tally.require_something(what="pregame event")
        return raws

    def _fetch_league(
        self,
        league_key: str,
        tournament_ids: Sequence[int],
        auth: Mapping[str, str],
        *,
        into: list[RawResponse],
    ) -> list[int]:
        """This league's events and market batches, appended as they arrive."""
        event_ids: list[int] = []
        for tournament_id in tournament_ids:
            events_raw = self._http.get(
                f"{self.base_url}/mm/get_sport_events",
                endpoint=f"events:{league_key}:{tournament_id}",
                params={"tournament_id": str(tournament_id)},
                headers=auth,
            )
            into.append(events_raw)
            for entry in _event_entries(events_raw, source=self._source_key):
                event_id = entry.get("event_id")
                if isinstance(event_id, int):
                    event_ids.append(event_id)
        for index in range(0, len(event_ids), EVENT_IDS_PER_REQUEST):
            chunk = event_ids[index : index + EVENT_IDS_PER_REQUEST]
            into.append(
                self._http.get(
                    f"{self.base_url}/mm/get_multiple_markets",
                    endpoint=f"markets:{league_key}:{index // EVENT_IDS_PER_REQUEST:02d}",
                    params={"event_ids": ",".join(str(eid) for eid in chunk)},
                    headers=auth,
                )
            )
        return event_ids

    def _login(self, access_key: str, secret_key: str) -> str:
        """Exchange the key pair for a session token, capturing nothing.

        Deliberately not routed through :class:`SourceClient`: its whole job is
        to persist every response as a :class:`RawResponse`, and this response
        body is the one payload of the session that must never be persisted.
        Failures are folded to :class:`LoginRequiredError` without echoing the
        body, for the same reason.
        """
        try:
            response = self._client.post(
                f"{self.base_url}/auth/login",
                headers={"Content-Type": "application/json"},
                json={"access_key": access_key, "secret_key": secret_key},
            )
        except Exception as exc:  # noqa: BLE001 - transport stacks vary
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise LoginRequiredError(
                f"{self._source_key}:auth: {type(exc).__name__} during login"
            ) from exc
        if response.status_code != 200:
            raise LoginRequiredError(
                f"{self._source_key}:auth: login answered HTTP {response.status_code}"
            )
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise LoginRequiredError(
                f"{self._source_key}:auth: login response is not JSON"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        token = None
        for candidate in (data, payload):
            if isinstance(candidate, dict) and candidate.get("access_token"):
                token = str(candidate["access_token"])
                break
        if not token:
            raise LoginRequiredError(
                f"{self._source_key}:auth: login answered 200 without an access_token"
            )
        return token

    # ── parse ────────────────────────────────────────────────────────────────

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_prophetx(raws)

    def close(self) -> None:
        self._http.close()
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


# ── module-level parsing, deterministic and I/O-free ─────────────────────────


def _data_list(payload: Any, *keys: str) -> list[Any]:
    """The first list found under ``data`` (or the root) at any of *keys*.

    The reference pages show every response wrapped in ``data`` but are not
    consistent about the inner field name, and no genuine capture exists yet to
    settle it.  Looking in the documented places — and only those — keeps this
    defensive without becoming a guess: an unrecognised shape returns empty and
    the caller records the miss.
    """
    for container in (payload.get("data") if isinstance(payload, dict) else None, payload):
        if isinstance(container, list):
            return container
        if isinstance(container, dict):
            for key in keys:
                value = container.get(key)
                if isinstance(value, list):
                    return value
    return []


def _event_entries(raw: RawResponse, *, source: str) -> list[dict[str, Any]]:
    payload = require_mapping(raw.json(), source=source, endpoint=raw.endpoint)
    return [
        entry
        for entry in _data_list(payload, "sport_events", "events")
        if isinstance(entry, dict)
    ]


def _match_tournaments(
    raw: RawResponse, leagues: Sequence[str], *, source: str
) -> dict[str, list[int]]:
    """League key → tournament ids, by whole-token name match.

    Token equality and not substring: ``"WNBA"`` contains ``"NBA"``, so a
    substring rule would file every WNBA tournament under the NBA and publish
    women's-league prices as NBA rows.  Tokenising first makes the two keys
    disjoint — ``"WNBA"`` tokenises to itself, never to ``"NBA"``.
    """
    payload = require_mapping(raw.json(), source=source, endpoint=raw.endpoint)
    wanted = {key.upper() for key in leagues}
    matched: dict[str, list[int]] = {}
    for entry in _data_list(payload, "tournaments"):
        if not isinstance(entry, dict):
            continue
        tournament_id = entry.get("id")
        if not isinstance(tournament_id, int):
            continue
        name = str(entry.get("name") or "")
        tokens = {token.upper() for token in name.replace("-", " ").split()}
        hits = wanted & tokens
        if len(hits) == 1:
            matched.setdefault(next(iter(hits)), []).append(tournament_id)
    return matched


def _looks_full_game(market: Mapping[str, Any]) -> bool:
    text = f"{market.get('name') or ''} {market.get('group_name') or ''}".lower()
    tokens = set(text.replace("-", " ").replace("/", " ").split())
    return not (tokens & _PERIOD_MARKERS)


class _Fixture:
    """One event's resolved identity, shared by every market row under it."""

    __slots__ = (
        "event_id",
        "competition",
        "home",
        "away",
        "commence_time",
        "by_competitor_id",
        "event_key",
        "identity_ref",
    )

    def __init__(
        self,
        event_id: str,
        competition: League,
        home: Participant,
        away: Participant,
        commence_time: Any,
        by_competitor_id: dict[int, Participant],
        identity_ref: str,
    ) -> None:
        self.event_id = event_id
        self.competition = competition
        self.home = home
        self.away = away
        self.commence_time = commence_time
        self.by_competitor_id = by_competitor_id
        self.event_key = ""  # assigned after the doubleheader pass
        self.identity_ref = identity_ref


def parse_prophetx(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Documented-schema parser; every assumption it makes is stated above.

    League identity comes from the **envelope's endpoint label**, exactly as
    the Action Network parser reads its book id: the payload does not name the
    league, and the request that asked for it does.
    """
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)

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
        for entry in _event_entries(raw, source=source):
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

    for raw in raws:
        if not raw.endpoint.startswith("markets:"):
            continue
        payload = require_mapping(raw.json(), source=source, endpoint=raw.endpoint)
        for event_id, markets in _markets_by_event(payload):
            fixture = fixtures.get(event_id)
            if fixture is None:
                outcome.skipped["markets_for_unknown_event"] += 1
                continue
            if fixture.commence_time <= raw.fetched_at:
                # The gate runs against this price's own response: the events
                # call establishing the fixture happened earlier in the pass,
                # and a game can start in between — the Smarkets/SX defect.
                outcome.skipped["event_already_started"] += 1
                continue
            for market in markets:
                if isinstance(market, dict):
                    _parse_market(market, fixture, raw, source=source, outcome=outcome)
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
    event_id = entry.get("event_id")
    if not isinstance(event_id, int):
        outcome.reject(
            source, "missing_event_id", "event entry without an integer event_id"
        )
        return
    competitors = entry.get("competitors")
    if not isinstance(competitors, list) or len(competitors) != 2:
        # Outrights and specials list one competitor or many; game markets
        # list exactly two.  Out of scope rather than broken.
        outcome.skipped["not_a_two_competitor_event"] += 1
        return
    commence = parse_iso_time(entry.get("scheduled"))
    if commence is None:
        outcome.reject(
            source,
            "bad_start_time",
            f"event {event_id}: unparseable scheduled {entry.get('scheduled')!r}",
        )
        return
    if not within_schedule_horizon(commence, raw.fetched_at, competition):
        outcome.skipped["outside_schedule_horizon"] += 1
        return

    resolved: list[tuple[Participant, Mapping[str, Any]]] = []
    for competitor in competitors:
        if not isinstance(competitor, dict):
            resolved = []
            break
        name = competitor.get("display_name") or competitor.get("name")
        participant = resolve_roster(
            name if isinstance(name, str) else None, competition.roster or ""
        )
        if participant is None:
            resolved = []
            break
        resolved.append((participant, competitor))
    if len(resolved) != 2:
        outcome.reject(
            source,
            "unknown_team",
            f"event {event_id}: competitors did not resolve on the "
            f"{competition.roster!r} roster",
            event_id=event_id,
        )
        return

    home_entry = next(
        (
            (participant, competitor)
            for participant, competitor in resolved
            if str(competitor.get("side") or "").lower() == "home"
        ),
        None,
    )
    if home_entry is None:
        outcome.reject(
            source,
            "missing_home_side",
            f"event {event_id}: no competitor carries side=home",
            event_id=event_id,
        )
        return
    away, home = orient(
        resolved[0][0], resolved[1][0], competition, home=home_entry[0]
    )

    by_competitor_id: dict[int, Participant] = {}
    for participant, competitor in resolved:
        competitor_id = competitor.get("id")
        if isinstance(competitor_id, int):
            by_competitor_id[competitor_id] = participant

    key = str(event_id)
    fixtures[key] = _Fixture(
        event_id=key,
        competition=competition,
        home=home,
        away=away,
        commence_time=commence,
        by_competitor_id=by_competitor_id,
        identity_ref=raw.ref,
    )
    per_league[key] = (
        build_event_key(away.key, home.key, commence, competition),
        commence,
    )


def _markets_by_event(payload: Mapping[str, Any]) -> list[tuple[str, list[Any]]]:
    """Normalise both documented market-response shapes to ``(event_id, markets)``.

    ``get_markets`` answers ``data: {event_id, markets: [...]}``;
    ``get_multiple_markets`` answers ``data`` keyed by event id.  Both are
    handled because replay must parse whichever shape a capture holds.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    if "markets" in data and isinstance(data.get("markets"), list):
        return [(str(data.get("event_id")), data["markets"])]
    pairs: list[tuple[str, list[Any]]] = []
    for event_id, markets in data.items():
        if isinstance(markets, list):
            pairs.append((str(event_id), markets))
    return pairs


def _parse_market(
    market: Mapping[str, Any],
    fixture: _Fixture,
    raw: RawResponse,
    *,
    source: str,
    outcome: ParseOutcome,
    is_alternate: bool = False,
) -> None:
    raw_type = str(market.get("type") or "")
    our_market = _MARKET_TYPES.get(raw_type)
    if our_market is None:
        # ``sup_moneyline`` and whatever else the venue adds: counted, not
        # errors — the docs enumerate four types and scope is three of them.
        outcome.skipped[f"market_type_{raw_type or 'missing'}"] += 1
        return
    if not _looks_full_game(market):
        outcome.skipped["non_full_game_market"] += 1
        return

    selections = market.get("selections")
    flat: list[Mapping[str, Any]] = []
    if isinstance(selections, list):
        for row in selections:
            if isinstance(row, dict):
                flat.append(row)
            elif isinstance(row, list):
                flat.extend(entry for entry in row if isinstance(entry, dict))
    for selection in flat:
        _parse_selection(
            selection,
            market,
            our_market,
            fixture,
            raw,
            source=source,
            outcome=outcome,
            is_alternate=is_alternate,
        )

    # ``market_lines`` nests the alternate ladders as further markets of the
    # same shape.  One level of recursion, flagged alternate, so a ladder
    # cannot masquerade as a second main line.
    if not is_alternate:
        lines = market.get("market_lines")
        if isinstance(lines, list):
            for sub in lines:
                if isinstance(sub, dict):
                    _parse_market(
                        sub,
                        fixture,
                        raw,
                        source=source,
                        outcome=outcome,
                        is_alternate=True,
                    )


def _parse_selection(
    selection: Mapping[str, Any],
    market: Mapping[str, Any],
    our_market: Market,
    fixture: _Fixture,
    raw: RawResponse,
    *,
    source: str,
    outcome: ParseOutcome,
    is_alternate: bool,
) -> None:
    odds = selection.get("odds")
    if not isinstance(odds, (int, float)) or isinstance(odds, bool) or odds <= 1.0:
        # An exchange side nobody prices arrives without usable odds; that is
        # thin liquidity, not corruption.
        outcome.skipped["no_price_on_selection"] += 1
        return
    if not is_plausible_decimal_odds(float(odds)):
        outcome.skipped["price_outside_the_plausible_band"] += 1
        return

    name = str(selection.get("display_name") or selection.get("name") or "")
    if our_market is Market.TOTAL:
        lowered = name.lower()
        if "over" in lowered:
            our_selection = Selection.OVER
        elif "under" in lowered:
            our_selection = Selection.UNDER
        else:
            outcome.reject(
                source,
                "unrecognised_total_selection",
                f"{fixture.event_id}: total selection named {name!r}",
                event_id=fixture.event_id,
            )
            return
    else:
        participant = None
        competitor_id = selection.get("competitor_id")
        if isinstance(competitor_id, int):
            participant = fixture.by_competitor_id.get(competitor_id)
        if participant is None:
            participant = resolve_roster(name, fixture.competition.roster or "")
        if participant is None:
            outcome.reject(
                source,
                "unknown_team",
                f"{fixture.event_id}: selection {name!r} resolves to no competitor",
                event_id=fixture.event_id,
            )
            return
        if participant.key == fixture.home.key:
            our_selection = Selection.HOME
        elif participant.key == fixture.away.key:
            our_selection = Selection.AWAY
        else:
            outcome.reject(
                source,
                "selection_not_in_fixture",
                f"{fixture.event_id}: {participant.key} is not a side of this event",
                event_id=fixture.event_id,
            )
            return

    line: float | None = None
    if our_market in (Market.SPREAD, Market.TOTAL):
        for candidate in (selection.get("line"), market.get("line")):
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                line = float(candidate)
                break
        if line is None:
            outcome.reject(
                source,
                "missing_line",
                f"{fixture.event_id}: {our_market.value} selection without a line",
                event_id=fixture.event_id,
            )
            return

    stake = selection.get("stake")
    limit = (
        float(stake)
        if isinstance(stake, (int, float))
        and not isinstance(stake, bool)
        and stake > 0
        else None
    )

    selection_id = selection.get("outcome_id")
    line_id = selection.get("line_id")
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
                market=our_market,
                period=Period.FULL_GAME,
                selection=our_selection,
                line=line,
                is_alternate=is_alternate,
                decimal_odds=float(odds),
                american_odds=decimal_to_american(float(odds)),
                implied_probability=implied_probability(float(odds)),
                source_market_id=str(market.get("id")) if market.get("id") is not None else None,
                source_selection_id=(
                    str(selection_id)
                    if selection_id is not None
                    else (str(line_id) if line_id is not None else None)
                ),
                limit_amount=limit,
                status=QuoteStatus.ACTIVE,
            )
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(
            source,
            "invalid_quote",
            f"{our_market.value}/{our_selection.value} on {fixture.event_id}: {exc}",
            event_id=fixture.event_id,
        )
