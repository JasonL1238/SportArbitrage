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
  ``odds`` and a ``stake`` number whose semantics the reference does not
  state (available-to-match or already-matched?), so it is not published as
  a limit.
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
    drop_duplicate_selections,
    drop_same_side_pairs,
    envelope_source,
    latest_capture,
    latest_per_endpoint,
    market_label_text,
    mentions_a_sub_period,
    parse_iso_time,
    resolve_over_under,
    signed_handicap,
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

# The sub-period vocabulary lives in ``_common.PERIOD_MARKERS``, shared with
# the sibling exchange adapter: two private copies drift, and this pair proved
# it — one guarded the window and the other did not.


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
        # ``dict.fromkeys`` and not a list: an event listed by two matched
        # tournaments would otherwise fetch its market batch twice, and two
        # batches parse into colliding dedup keys — which the storage layer's
        # UNIQUE constraint answers by aborting the whole run's insert.
        seen_event_ids: dict[int, None] = {}
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
                    seen_event_ids[event_id] = None
        event_ids = list(seen_event_ids)
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
            # AssertionError is re-raised alongside the interrupt pair: it is
            # how a test proves no socket was opened, and how a programming
            # error announces itself — neither may be laundered into a
            # "credentials missing" diagnosis.
            if isinstance(exc, (KeyboardInterrupt, SystemExit, AssertionError)):
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
    """League key → tournament ids, by **exact name equality** only.

    A tournament matches when its whole name, case-folded, equals the league
    key (``"NBA"``) or the league's display name (``"National Basketball
    Association"``).  Not containment, not tokens: ``"NBA Summer League"``
    and ``"NBA G League"`` both *contain* the token ``NBA`` and are not the
    NBA — Summer League fields the real franchises and G League affiliates
    carry parent nicknames ("Maine Celtics" resolves to BOS), so an admitted
    development tournament can mint the same event key as a real same-night
    fixture and feed its prices into the cross-book comparison as NBA legs.
    A spelling this rule misses shows up as a visibly empty scope, which is
    the recoverable direction; the first credentialed capture settles the
    venue's actual vocabulary.
    """
    payload = require_mapping(raw.json(), source=source, endpoint=raw.endpoint)
    wanted: dict[str, str] = {}
    for key in leagues:
        upper = key.upper()
        wanted[upper] = upper
        wanted[league_registry.league(key).name.upper()] = upper
    matched: dict[str, list[int]] = {}
    for entry in _data_list(payload, "tournaments"):
        if not isinstance(entry, dict):
            continue
        tournament_id = entry.get("id")
        if not isinstance(tournament_id, int):
            continue
        name = " ".join(str(entry.get("name") or "").split()).upper()
        league_key = wanted.get(name)
        if league_key is not None:
            bucket = matched.setdefault(league_key, [])
            # One id can arrive under both accepted spellings ("NBA" and the
            # display name); fetching it twice wastes a request per duplicate.
            if tournament_id not in bucket:
                bucket.append(tournament_id)
    return matched


def _spread_lines_are_sign_opposed(
    flat: Sequence[Mapping[str, Any]], market: Mapping[str, Any]
) -> bool:
    """Exactly two selections whose own lines sum to zero — and agree with
    the market's own line when it states one.

    Zero sums pass, because a pick'em is a real and common market and is the
    one line with no sign left to resolve.  Rejecting it cost more than the
    market: ``SourceHealth.ok`` is false while any rejection stands, so one PK
    game on the slate graded ProphetX unhealthy every pass until tip-off — a
    parser-failure signal spent on a venue behaving normally.

    But ``0`` is also what a missing numeric field defaults to, and a pair of
    defaulted zeros sums to zero just as neatly as a pick'em does.  What tells
    them apart is the market's *own* ``line``: a real PK market states 0 or
    states nothing, while a market that says 1.5 and hands over two zeroed
    selections is contradicting itself, and publishing that pair prices a
    handicap bet as a pick'em — a 25% phantom arbitrage in the measured case.
    So the magnitudes must agree with the market's when it states one.  This
    is the corroboration the sibling adapter gets from ``strike``; the field
    was here all along and only the totals path was reading it.
    """
    if len(flat) != 2:
        return False
    lines = []
    for selection in flat:
        line = selection.get("line")
        if not isinstance(line, (int, float)) or isinstance(line, bool):
            return False
        lines.append(float(line))
    if abs(lines[0] + lines[1]) > 1e-9:
        return False
    stated = market.get("line")
    if isinstance(stated, (int, float)) and not isinstance(stated, bool) and stated:
        # ``and stated``: a market-level zero is the same defaulted field this
        # guard exists to distrust, so treating it as an authoritative claim of
        # "pick'em" would reject every genuine handicap the venue sends —
        # every spread market lost, and the source graded unhealthy on every
        # pass, which is the regression round 3 fixed arriving from the other
        # side.  A stated *non-zero* line is a real claim and must agree.
        return abs(abs(lines[0]) - abs(float(stated))) < 1e-9
    if not any(lines):
        # Two zeroed selections under a market that states nothing.  If the
        # selections' own names spell a handicap, the payload is telling us
        # the line in the only place it does and the zeros are defaults, not
        # a pick'em: publishing them prices a handicap bet as a coin flip.
        named = [signed_handicap(_selection_label(entry)) for entry in flat]
        if any(value for value in named):
            return False
    return True


def _selection_label(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('display_name') or ''} {entry.get('name') or ''}".strip()


def _looks_full_game(market: Mapping[str, Any]) -> bool:
    return not mentions_a_sub_period(market_label_text(market))


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
    # Newest pass, one response per label: the endpoint scheme carries batch
    # counters (``markets:MLB:00``), so a directory holding two runs would
    # otherwise emit yesterday's price beside today's — see ``latest_capture``.
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
                outcome.skipped["market_on_out_of_scope_event"] += 1
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
    drop_same_side_pairs(source, outcome)
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
        outcome.skipped["non_game_event"] += 1
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
        outcome.skipped["event_beyond_the_leagues_schedule_horizon"] += 1
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
        outcome.skipped[f"market_type_out_of_scope:{raw_type or 'missing'}"] += 1
        return
    selections = market.get("selections")
    flat: list[Mapping[str, Any]] = []
    if isinstance(selections, list):
        for row in selections:
            if isinstance(row, dict):
                flat.append(row)
            elif isinstance(row, list):
                flat.extend(entry for entry in row if isinstance(entry, dict))

    # Screen the selections' own text too, not only the market's.  A venue
    # that says "Total" at the market and "1st Half Over 4.5" at the
    # selection is naming the window in the only place it names it, and
    # reading just the market published two full-game totals at 4.5 that
    # join real full-game rows while pricing a different bet.
    if not _looks_full_game(market) or mentions_a_sub_period(
        *(entry.get("name") for entry in flat),
        *(entry.get("display_name") for entry in flat),
    ):
        outcome.skipped["period_out_of_scope"] += 1
        return

    if our_market is Market.SPREAD and not _spread_lines_are_sign_opposed(flat, market):
        # The whole-market invariant, not a per-selection one: a numeric line
        # is not evidence of a *signed* line.  Two selections both carrying
        # ``+1.5`` are the round-1 sign ambiguity moved one level down, and
        # only the pair can reveal it — a real handicap market's two sides
        # sum to zero.  Anything else (one side, no lines, same sign) is
        # rejected whole rather than published half-verified.
        outcome.reject(
            source,
            "spread_sign_unresolved",
            f"{fixture.event_id}: spread market {market.get('id')} does not "
            "present two sign-opposed selection lines",
            event_id=fixture.event_id,
        )
    else:
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
        outcome.skipped["contract_without_a_takeable_offer"] += 1
        return
    if not is_plausible_decimal_odds(float(odds)):
        outcome.skipped["price_outside_the_plausible_band"] += 1
        return

    name = str(selection.get("display_name") or selection.get("name") or "")
    if our_market is Market.TOTAL:
        resolved_side = resolve_over_under(name)
        if resolved_side is not None:
            our_selection = resolved_side
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
    if our_market is Market.TOTAL:
        # A total's strike is shared and sign-free, so the market-level line
        # is a legitimate fallback for a selection that omits its own.
        for candidate in (selection.get("line"), market.get("line")):
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                line = float(candidate)
                break
        if line is None:
            outcome.reject(
                source,
                "missing_line",
                f"{fixture.event_id}: total selection without a line",
                event_id=fixture.event_id,
            )
            return
    elif our_market is Market.SPREAD:
        # No market-level fallback here, ever: the market's one number is an
        # unsigned magnitude serving two opposite handicaps, and stamping it
        # on both sides fabricates the sign of one of them — the phantom-arb
        # class the Novig parser refuses under the same reason code.  A
        # spread selection prices *its own* signed line or it is rejected.
        candidate = selection.get("line")
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            line = float(candidate)
        else:
            outcome.reject(
                source,
                "spread_sign_unresolved",
                f"{fixture.event_id}: spread selection {name!r} carries no "
                "signed line of its own",
                event_id=fixture.event_id,
            )
            return

    # ``stake`` is documented as a number and nothing more — whether it is
    # money still available to match or volume already matched, and in what
    # currency, the reference does not say.  Publishing it as a stake ceiling
    # under the wrong reading would overstate every ProphetX limit, so no
    # ``limit_amount`` is published until the first credentialed session
    # settles the semantics (recorded in SOURCE_FEASIBILITY).
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
                limit_amount=None,
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
