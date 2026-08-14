"""theScore Bet pregame markets, from the anonymous sportsbook GraphQL edge.

Three calls, all anonymous, all plain HTTP — no browser at collection time:

* ``POST /graphql`` ``startup`` — the region this egress reached, and the
  anonymous token every later call must carry.
* ``POST /graphql`` ``page(canonicalUrl:)`` — one per competition, returning the
  ``Section`` whose ``archetype`` is ``COMPETITION_LINES``.
* ``POST /graphql`` ``competitionSection(id:)`` — that section's shelf of
  ``GridMarketCard`` / ``SoccerGridMarketCard``, each an event with its markets.

The token is what makes this work and is easy to mistake for a privacy detail.
Without it every odds-bearing field answers ``403 UNAUTHORIZED``; with it under
the wrong header name it answers ``401``; it is required as
``x-anonymous-authorization: Bearer <token>`` and nothing else.  The client
headers below are the other half — without them ``startup`` itself answers
``"Invalid user agent, app_version: nil platform: nil"``.  Both were measured on
2026-08-13 and are written up in ``docs/evidence/state-routing.md``.

Persisted queries exist (``GET /graphql/persisted_queries/{hash}``) and are
**not** required: the same gate applies to both forms, so this adapter sends its
own documents and avoids a hash that rotates every deploy.  Introspection is
disabled, so every field named here came from a response that returned it.

What makes travelling between states safe is that the licence is resolved
server-side from the request IP and *reported back*: ``currentRegionCode`` is
compared to the routed state on every fetch, so a correct host reached from the
wrong egress refuses rather than pricing another state's board.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.normalize import (
    decimal_to_american,
    implied_probability,
    is_plausible_decimal_odds,
)
from src.participants import canonical_participant, is_pairing
from src.raw_store import RawResponse
from src.schema import Market, Period, QuoteStatus, Selection, Sport
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
    mentions_a_sub_period,
    parse_iso_time,
    priced_quote,
    refuse_one_sided_market,
    within_schedule_horizon,
)
from src.sources.base import ParseOutcome
from src.sources.guards import FormatChangeError, GeoRestrictedError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "thescore"
HOST_INTERVAL = 0.6

#: No default host.  Hard Rock's ``DEFAULT_API_BASE`` names New Jersey, and an
#: adapter that silently falls back to one state's edge is the exact fault the
#: region cross-check below exists to catch — so this one refuses to fetch until
#: :mod:`src.jurisdictions` has routed it.
DEFAULT_API_BASE = ""

#: Sent on every request.  API versioning and routing, not credentials: read off
#: the application bundle (module 98655, and 48041 for the platform constant),
#: where ``NEXT_PUBLIC_BUILD_VARIANT`` is literally ``"espnbet"`` — theScore Bet
#: runs the ESPN Bet codebase, both being Penn Entertainment.
CLIENT_HEADERS: dict[str, str] = {
    "content-type": "application/json",
    "x-platform": "web",
    "x-app-version": "26.16.1",
    "x-app": "espnbet",
    "x-client": "espnbet",
    "origin": "https://sportsbook.thescore.bet",
    "referer": "https://sportsbook.thescore.bet/",
}


@dataclass(frozen=True)
class _Competition:
    """One league, and the page path that serves its lines shelf."""

    league: str
    canonical_url: str

    @property
    def slug(self) -> str:
        """theScore's own competition slug — the last path segment.

        Cross-checked against each event's ``competition.slug`` so a shelf that
        starts carrying a neighbouring competition is visible rather than filed
        under the league this route asked for.
        """
        return self.canonical_url.rsplit("/", 1)[-1]

    @property
    def label(self) -> str:
        """Endpoint label for this competition's lines call.

        Distinct in its *alphanumerics*, because :func:`src.raw_store._slug`
        folds every run of non-alphanumerics to one ``-``: ``lines-la_liga`` and
        ``lines-la-liga`` would name the same stored file.
        """
        return f"lines-{self.league.lower().replace('_', '-')}"


#: Competition → canonical page path.  **Every one of these was asked from an
#: Illinois egress on 2026-08-13 and answered with a ``COMPETITION_LINES``
#: section**; none is inferred from the pattern, because the organization
#: segment does not follow one — MLS sits under ``usa`` while every other
#: American league sits under ``united-states``, and the ``united-states``
#: spelling of the MLS path answers ``200`` with ``page: null``.  A wrong path is
#: therefore silent unless it is checked, which :meth:`_fetch_lines` does.
COMPETITIONS: tuple[_Competition, ...] = (
    _Competition("MLB", "/sport/baseball/organization/united-states/competition/mlb"),
    _Competition("NBA", "/sport/basketball/organization/united-states/competition/nba"),
    _Competition("WNBA", "/sport/basketball/organization/united-states/competition/wnba"),
    _Competition("NFL", "/sport/football/organization/united-states/competition/nfl"),
    _Competition("NHL", "/sport/hockey/organization/united-states/competition/nhl"),
    _Competition("MLS", "/sport/soccer/organization/usa/competition/mls"),
    _Competition("EPL", "/sport/soccer/organization/england/competition/premier-league"),
    _Competition("LA_LIGA", "/sport/soccer/organization/spain/competition/la-liga"),
    _Competition("SERIE_A", "/sport/soccer/organization/italy/competition/serie-a"),
    _Competition("BUNDESLIGA", "/sport/soccer/organization/germany/competition/bundesliga"),
    _Competition("LIGUE_1", "/sport/soccer/organization/france/competition/ligue-1"),
)

_BY_LABEL: dict[str, _Competition] = {comp.label: comp for comp in COMPETITIONS}

#: Markets this venue actually served on 2026-08-13, per sport.
#:
#: Soccer claims the moneyline alone because its shelf carried nothing else —
#: 15 MLS and 10 EPL cards, one ``THREE_WAY_MONEYLINE`` apiece and no total.
#: The claim *narrows* what :func:`src.validation._check_coverage` expects, so
#: under-claiming a market that later appears costs nothing while over-claiming
#: one that never does reports this book as broken every run.
MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.SOCCER: frozenset({Market.MONEYLINE}),
}

#: ``Market.type`` → (market, period), before the sport gate below.
MARKET_TYPES: dict[str, tuple[Market, Period]] = {
    "MONEYLINE": (Market.MONEYLINE, Period.FULL_GAME),
    "SPREAD": (Market.SPREAD, Period.FULL_GAME),
    "TOTAL": (Market.TOTAL, Period.FULL_GAME),
    "THREE_WAY_MONEYLINE": (Market.MONEYLINE, Period.FULL_GAME),
}

#: ``MarketSelection.type`` → which of *the book's* two sides, or an over/under.
#:
#: ``HOME_``/``AWAY_`` are statements about the book's home side, which is ours
#: only where the league has one, so they are read back through
#: :meth:`~src.sources._common.Fixture.our_side` rather than used directly.
SELECTION_SIDES: dict[str, Selection] = {
    "HOME_MONEYLINE": Selection.HOME,
    "AWAY_MONEYLINE": Selection.AWAY,
    "HOME_SPREAD": Selection.HOME,
    "AWAY_SPREAD": Selection.AWAY,
}

STARTUP_QUERY = """
query Startup($connectToken: String!) {
  startup {
    regionalMetadata {
      currentRegionCode
      ipAddressShortRegionCode
      validRegion
    }
    anonymousToken(connectToken: $connectToken)
  }
}
""".strip()
"""Deliberately selects no ``ipAddress``.

``regionalMetadata.ipAddress`` is a real field carrying this machine's exit IP,
and not selecting it keeps the address out of the response altogether — which
demotes :func:`redact_anonymous_token` to defence in depth rather than the only
thing standing between an egress address and a committed fixture."""

COMPETITION_PAGE_QUERY = """
query CompetitionPage($canonicalUrl: String!) {
  page(canonicalUrl: $canonicalUrl) {
    id
    pageChildren {
      __typename
      ... on Section {
        id
        archetype
      }
    }
  }
}
""".strip()

LINES_QUERY = """
query CompetitionLines($id: ID!) {
  competitionSection(id: $id) {
    id
    sectionChildren {
      __typename
      ... on MarketplaceShelf {
        id
        marketplaceShelfChildren {
          __typename
          ... on GridMarketCard {
            id
            event { ...eventFields }
            markets { ...marketFields }
          }
          ... on SoccerGridMarketCard {
            id
            event { ...eventFields }
            markets { ...marketFields }
          }
        }
      }
    }
  }
}

fragment eventFields on EventWrapper {
  fallbackEvent {
    __typename
    ... on StandardEvent {
      id
      status
      startTime
      name
      homeParticipant { id fullName abbreviation }
      awayParticipant { id fullName abbreviation }
      competition { slug name }
    }
  }
}

fragment marketFields on Market {
  id
  name
  type
  status
  selections {
    id
    type
    name { fullName }
    participant { id fullName }
    odds { numeratorLong denominatorLong }
    points { decimalPoints }
  }
}
""".strip()
"""``markets``, not ``pagedMarkets``.

A captured response spells that key ``pagedMarkets``; it is a **response alias**
and the schema field is ``markets``, so querying the alias fails validation.  A
response key is not a schema field."""

_ANONYMOUS_TOKEN = re.compile(r'("anonymousToken"\s*:\s*)"[^"\\]*"')
_REDACTED = '"[redacted]"'


def redact_anonymous_token(body: str) -> str:
    """Replace the anonymous session token in a captured body.

    Narrow on purpose.  ``check_http_response`` is handed ``raw.body``, so a
    filter broad enough to rewrite a block page would turn a refusal into an
    apparently-good ``200`` — this one only rewrites the value that follows one
    exact key, and every other payload passes through byte-identical, which is
    what keeps a committed lines fixture equal to the wire.
    """
    return _ANONYMOUS_TOKEN.sub(rf"\1{_REDACTED}", body)


class TheScoreAdapter:
    """Collect theScore Bet pregame game markets for one routed state."""

    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        proxy_state: str | None = None,
    ) -> None:
        wanted = frozenset(leagues) if leagues is not None else None
        chosen = tuple(
            comp
            for comp in COMPETITIONS
            if wanted is None or comp.league in wanted
        )
        if not chosen:
            raise ValueError(
                f"TheScoreAdapter has no competition for leagues {sorted(wanted or ())!r}"
            )
        self._competitions = chosen
        self._source_key = source_key
        self.api_base = api_base.rstrip("/")
        self.routed_state = proxy_state
        self._token: str | None = None
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=HOST_INTERVAL,
            proxy_state=proxy_state,
            body_filter=self._capture_token,
        )

    def _capture_token(self, body: str) -> str:
        """Keep the anonymous token in memory, and out of the stored bytes.

        The fetch needs the token and the capture must not carry it, and this is
        the one place both are true at once: ``body_filter`` runs before the
        :class:`~src.raw_store.RawResponse` exists, so reading it back off
        ``raw.body`` afterwards is not possible — by design, since that is
        exactly what "stored" means here.
        """
        match = _ANONYMOUS_TOKEN.search(body)
        if match is not None:
            token = body[match.end(1):].split('"')[1]
            if token:
                self._token = token
        return redact_anonymous_token(body)

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        return tuple(comp.league for comp in self._competitions)

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {
                comp.league: MARKETS_BY_SPORT[league_registry.league(comp.league).sport]
                for comp in self._competitions
            },
            self.leagues,
        )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        """Collect every configured competition's lines shelf.

        *tier* is unused: a competition's whole shelf arrives in one call, so
        there is no per-event follow-up to defer and both tiers collect
        identically.
        """
        del tier
        if not self.api_base:
            raise SourceError(
                f"{self._source_key}: no api_base — this adapter is routed per state "
                "by src.jurisdictions and has no default edge to fall back to"
            )
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)

        # Startup is a prerequisite, not a scope with a slate: it succeeds from
        # any egress, so counting it as produced would let require_something
        # pass on a run where every board came back empty.
        raws.append(self._fetch_startup())

        for comp in self._competitions:
            tally.requested(comp.label)
            try:
                raws.extend(self._fetch_lines(comp, tally))
            except SourceError as exc:
                log.info("%s: %s unavailable: %s", self._source_key, comp.league, exc)
                tally.failed(comp.label, exc)

        tally.require_something(what="pregame theScore Bet event")
        return raws

    def _fetch_startup(self) -> RawResponse:
        raw = self._http.post(
            f"{self.api_base}/graphql",
            endpoint="startup",
            json_body={
                "operationName": "Startup",
                "query": STARTUP_QUERY,
                # Client-generated, and the server accepts any opaque id: this
                # names the anonymous session rather than authenticating it.
                "variables": {"connectToken": uuid.uuid4().hex[:26]},
            },
            headers=CLIENT_HEADERS,
        )
        if self._token is None:
            raise FormatChangeError(
                f"{self._source_key}:startup: no anonymousToken in the response — "
                "every odds field is refused without one"
            )
        self._require_expected_region(raw)
        return raw

    def _require_expected_region(self, raw: RawResponse) -> None:
        """Refuse when the edge reports a licence other than the routed one.

        This is the check no route configuration can make.  The host names the
        state and the request carries the egress, and only the *response* says
        which licence actually answered — so a correct host reached from the
        wrong egress is a plausible board of somebody else's prices, returned
        with a ``200``.  It is also why travelling needs no separate work: from
        Pennsylvania the same call answers ``US-PA`` and the PA route passes.
        """
        region = _region_code(raw)
        if self.routed_state is None or region is None:
            return
        expected = f"US-{self.routed_state.upper()}"
        if region.upper() != expected:
            raise GeoRestrictedError(
                f"{self._source_key}:startup: routed to {self.routed_state.upper()} but the "
                f"edge answered {region!r} — this egress is not in the routed state"
            )

    def _fetch_lines(self, comp: _Competition, tally: ScopeTally) -> list[RawResponse]:
        raws = [
            self._http.post(
                f"{self.api_base}/graphql",
                endpoint=f"page-{comp.label.removeprefix('lines-')}",
                json_body={
                    "operationName": "CompetitionPage",
                    "query": COMPETITION_PAGE_QUERY,
                    "variables": {"canonicalUrl": comp.canonical_url},
                },
                headers=self._authorized_headers(),
            )
        ]
        page = _page_children(raws[0])
        if page is None:
            # ``page: null`` on a 200 is what a moved path looks like, and it is
            # the one outcome here that is a fault rather than an empty season.
            raise FormatChangeError(
                f"{self._source_key}:{comp.label}: {comp.canonical_url} returned no page — "
                "the competition path has moved"
            )
        section_id = next(
            (
                child.get("id")
                for child in page
                if child.get("archetype") == "COMPETITION_LINES" and child.get("id")
            ),
            None,
        )
        if section_id is None:
            # An out-of-season competition serves a drawer instead of a lines
            # tab.  That is an empty scope, not a refusal: reading it as one
            # would grade a book broken every summer.
            tally.produced(comp.label, 0)
            return raws

        raws.append(
            self._http.post(
                f"{self.api_base}/graphql",
                endpoint=comp.label,
                json_body={
                    "operationName": "CompetitionLines",
                    "query": LINES_QUERY,
                    "variables": {"id": section_id},
                },
                headers=self._authorized_headers(),
            )
        )
        # A zero-card shelf is counted as an empty scope rather than a refusal,
        # which is safe *here* and would not be on a venue without the region
        # check: the wrong-state failure this normally screens for — a 200 with
        # an empty board — has already been refused by _require_expected_region.
        tally.produced(comp.label, len(list(_cards(raws[-1]))))
        return raws

    def _authorized_headers(self) -> dict[str, str]:
        if self._token is None:  # pragma: no cover - _fetch_startup refuses first
            raise SourceError(f"{self._source_key}: no anonymous token")
        return {
            **CLIENT_HEADERS,
            "x-anonymous-authorization": f"Bearer {self._token}",
        }

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_thescore(raws)

    def close(self) -> None:
        self._http.close()


# ── reading one captured envelope ────────────────────────────────────────────


def _payload(raw: RawResponse) -> Mapping[str, Any]:
    payload = raw.json()
    if not isinstance(payload, dict):
        raise FormatChangeError(f"{raw.source}:{raw.endpoint}: expected an object")
    return payload


def _region_code(raw: RawResponse) -> str | None:
    startup = ((_payload(raw).get("data") or {}).get("startup") or {})
    metadata = startup.get("regionalMetadata")
    if not isinstance(metadata, dict):
        return None
    code = metadata.get("currentRegionCode")
    return code if isinstance(code, str) and code else None


def _page_children(raw: RawResponse) -> list[Mapping[str, Any]] | None:
    page = (_payload(raw).get("data") or {}).get("page")
    if not isinstance(page, dict):
        return None
    children = page.get("pageChildren")
    return [c for c in children if isinstance(c, dict)] if isinstance(children, list) else []


def _cards(raw: RawResponse) -> Iterable[Mapping[str, Any]]:
    """Every market card on a lines envelope's shelves."""
    section = (_payload(raw).get("data") or {}).get("competitionSection")
    if not isinstance(section, dict):
        return
    children = section.get("sectionChildren")
    if not isinstance(children, list):
        return
    for child in children:
        if not isinstance(child, dict):
            continue
        for card in child.get("marketplaceShelfChildren") or []:
            # Every card, including a type this adapter's fragment does not
            # spread — those arrive as a bare ``__typename`` and are counted as
            # a skip by name rather than dropped here without a trace.
            if isinstance(card, dict):
                yield card


# ── parsing ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class _Fixture(Fixture):
    """Adds the two participant ids **the book** used for this event.

    theScore states its selections twice over: once as a ``HOME_``/``AWAY_``
    type, and once as the participant the outcome belongs to — carrying the same
    identifier the event gave its own two competitors.  Keeping both ids here is
    what lets :func:`_selection_for` require the two to agree rather than trust
    the type, which is wrong on at least one real market.

    Ids rather than names because a name is not an identity on this venue: the
    event says "Borussia Monchengladbach" and the selection says
    "Monchengladbach", and on an open-roster competition those normalize to two
    different keys for one club.  An id has no such gap.
    """

    book_home_id: str
    book_away_id: str

    def side_of(self, participant_id: str) -> Selection | None:
        """Which of *the book's* two sides that participant id is, if either."""
        if participant_id and participant_id == self.book_home_id:
            return Selection.HOME
        if participant_id and participant_id == self.book_away_id:
            return Selection.AWAY
        return None


def parse_thescore(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Pure: turn captured lines shelves into normalized rows."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    latest = latest_per_endpoint(latest_capture(raws))

    fixtures: dict[str, Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], Fixture]] = []

    for raw in latest:
        comp = _BY_LABEL.get(raw.endpoint)
        if comp is None:
            continue
        payload = _payload(raw)
        if payload.get("errors"):
            outcome.reject(source, "graphql_errors", str(payload["errors"])[:200])
            continue
        competition = league_registry.league(comp.league)
        for card in _cards(raw):
            fixture = _accept_event(card, comp, competition, source, raw, outcome)
            if fixture is None:
                continue
            fixtures[fixture.event_id] = fixture
            work.append((raw, card, fixture))

    if not fixtures:
        return outcome

    # One global pass, after every competition is accepted: a doubleheader's
    # ordinals must be assigned over the whole slate rather than per envelope.
    resolved = resolve_doubleheaders(
        {eid: (f.base_key, f.commence_time) for eid, f in fixtures.items()}
    )
    for raw, card, fixture in work:
        _emit_markets(card, raw, source, fixture, resolved[fixture.event_id], outcome)

    drop_duplicate_selections(source, outcome)
    return outcome


def _accept_event(
    card: Mapping[str, Any],
    comp: _Competition,
    competition: Any,
    source: str,
    raw: RawResponse,
    outcome: ParseOutcome,
) -> Fixture | None:
    event = ((card.get("event") or {}).get("fallbackEvent") or {})
    if not isinstance(event, dict):
        outcome.skipped["card_without_event"] += 1
        return None
    if event.get("__typename") != "StandardEvent":
        # Outrights and non-fixture cards share the shelf.
        outcome.skipped[f"event_not_standard:{event.get('__typename')}"] += 1
        return None

    event_id = str(event.get("id") or "")
    if not event_id:
        outcome.skipped["missing_event_id"] += 1
        return None

    status = str(event.get("status") or "")
    if status != "PRE_GAME":
        # In-play events sit on the same board as pregame ones.
        outcome.skipped[f"event_status:{status or 'unknown'}"] += 1
        return None

    slug = str(((event.get("competition") or {}).get("slug")) or "")
    if slug and slug != comp.slug:
        # The shelf carried a competition this route did not ask for; filing it
        # under the requested league would mislabel it.
        outcome.skipped[f"competition_off_route:{slug}"] += 1
        return None

    commence_time = parse_iso_time(event.get("startTime"))
    if commence_time is None:
        outcome.reject(
            source, "missing_commence_time", f"event {event_id}", event_id=event_id
        )
        return None
    if commence_time <= raw.fetched_at:
        outcome.skipped["event_already_started"] += 1
        return None
    if not within_schedule_horizon(commence_time, raw.fetched_at, competition):
        outcome.skipped["beyond_schedule_horizon"] += 1
        return None

    home_side_raw = event.get("homeParticipant") or {}
    away_side_raw = event.get("awayParticipant") or {}
    home_name = str(home_side_raw.get("fullName") or "")
    away_name = str(away_side_raw.get("fullName") or "")
    home_id = str(home_side_raw.get("id") or "")
    away_id = str(away_side_raw.get("id") or "")
    if not home_name or not away_name:
        outcome.skipped["missing_home_away"] += 1
        return None
    if not home_id or not away_id or home_id == away_id:
        # Without two distinct ids the second orientation witness cannot answer,
        # and a market's sides would rest on ``type`` alone.
        outcome.reject(
            source,
            "missing_participant_ids",
            f"event {event_id}: home={home_id!r} away={away_id!r}",
            event_id=event_id,
        )
        return None
    if any(is_pairing(name, competition.sport) for name in (home_name, away_name)):
        outcome.skipped["doubles_or_team_pairing"] += 1
        return None

    home = canonical_participant(home_name, competition)
    away = canonical_participant(away_name, competition)
    if home is None or away is None or home.key == away.key:
        outcome.reject(
            source,
            "unresolved_participants",
            f"event {event_id}: {away_name!r}/{home_name!r}",
            event_id=event_id,
        )
        return None

    # theScore *states* which side it calls home, so there is no title order to
    # read and no separator to trust.  It still goes through orient(), which is
    # what keeps a league with no real home side from inheriting the book's.
    away_side, home_side = orient(away, home, competition, home=home)
    return _Fixture(
        event_id=event_id,
        sport=competition.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=home.key,
        book_home_id=home_id,
        book_away_id=away_id,
        commence_time=commence_time,
        base_key=build_event_key(
            away_side.key, home_side.key, commence_time, competition
        ),
    )


def _emit_markets(
    card: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    allowed = MARKETS_BY_SPORT.get(fixture.sport, frozenset())
    for market in card.get("markets") or []:
        if not isinstance(market, dict):
            continue
        first_index = len(outcome.quotes)
        kind = _market_for(market, fixture, allowed, outcome)
        if kind is None:
            continue
        market_kind, period = kind
        for sel in market.get("selections") or []:
            if isinstance(sel, dict):
                _emit_selection(
                    sel, market, market_kind, period, raw, source,
                    fixture, event_key, outcome,
                )
        _require_mirrored_lines(
            outcome, first_index, source, market_kind, fixture, market
        )
        _require_the_draw_leg(
            outcome, first_index, source, market_kind, fixture, market
        )
        refuse_one_sided_market(
            source,
            outcome,
            first_index,
            market_id=market.get("id"),
            event_id=fixture.event_id,
        )


def _market_for(
    market: Mapping[str, Any],
    fixture: Fixture,
    allowed: frozenset[Market],
    outcome: ParseOutcome,
) -> tuple[Market, Period] | None:
    """The two witnesses a market must pass before any of its prices are read.

    ``Market.type`` is the strong signal and it is **wrong on at least one real
    row**: an MLB market typed ``MONEYLINE``, named "Run In The 1st Inning -
    Enhanced Odds", carries selections typed ``AWAY_MONEYLINE`` and
    ``HOME_MONEYLINE`` whose names are "Yes" and "No".  Trusting the type there
    publishes a first-inning proposition as the game moneyline with "Yes" as the
    away team — and nothing downstream catches it, because its implied
    probabilities sum above one, its ``market_key`` differs from the real
    moneyline's, and its selections collide with nothing.  So the label is
    screened for a narrower window as well, and the third witness — that the
    selections name the fixture's own competitors — is applied in
    :func:`_selections_name_the_fixture`.
    """
    status = str(market.get("status") or "")
    if status and status != "OPEN":
        outcome.skipped[f"market_status:{status}"] += 1
        return None

    mtype = str(market.get("type") or "")
    mapped = MARKET_TYPES.get(mtype)
    if mapped is None:
        # ``LIST`` is props and outrights; a null type is a prop whose type the
        # venue did not set.  Neither is a default.
        outcome.skipped[f"market_type_out_of_scope:{mtype or 'null'}"] += 1
        return None
    market_kind, period = mapped

    is_soccer = fixture.sport is Sport.SOCCER
    if is_soccer and mtype == "MONEYLINE":
        # Soccer's three-way is its own type on its own card type, so a two-way
        # moneyline on a draw league is a different product — not a three-way
        # with the draw dropped, but merging the two would be exactly that, and
        # it turns a floor of zero into minus the whole stake.
        outcome.skipped["two_way_moneyline_on_a_draw_league"] += 1
        return None
    if not is_soccer and mtype == "THREE_WAY_MONEYLINE":
        outcome.skipped["three_way_moneyline_off_a_draw_league"] += 1
        return None
    if market_kind not in allowed:
        outcome.skipped[f"market_out_of_scope:{market_kind.value}"] += 1
        return None

    if mentions_a_sub_period(market.get("name")):
        outcome.skipped["market_names_a_sub_period"] += 1
        return None
    return market_kind, period


def _emit_selection(
    sel: Mapping[str, Any],
    market: Mapping[str, Any],
    market_kind: Market,
    period: Period,
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    sel_type = str(sel.get("type") or "")
    selection = _selection_for(sel_type, market_kind, fixture, sel, source, outcome)
    if selection is None:
        return

    decimal = _decimal_odds(sel)
    if decimal is None:
        # A suspended leg carries ``odds: null``; that is a missing price rather
        # than a malformed one.
        outcome.skipped["selection_without_odds"] += 1
        return
    if not is_plausible_decimal_odds(decimal):
        outcome.reject(
            source,
            "implausible_odds",
            f"event {fixture.event_id} {decimal}",
            event_id=fixture.event_id,
        )
        return

    line = _line_for(sel, market_kind)
    if market_kind in (Market.SPREAD, Market.TOTAL) and line is None:
        outcome.reject(
            source,
            "missing_line",
            f"event {fixture.event_id} {market.get('name')!r}",
            event_id=fixture.event_id,
        )
        return

    try:
        outcome.quotes.append(
            priced_quote(
                fixture,
                source=source,
                raw=raw,
                event_key=event_key,
                market=market_kind,
                period=period,
                selection=selection,
                line=line,
                decimal_odds=decimal,
                american_odds=decimal_to_american(decimal),
                implied_probability=implied_probability(decimal),
                status=QuoteStatus.ACTIVE,
                source_market_id=str(market.get("id") or "") or None,
            )
        )
    except (TypeError, ValueError) as exc:
        outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)


def _selection_for(
    sel_type: str,
    market_kind: Market,
    fixture: _Fixture,
    sel: Mapping[str, Any],
    source: str,
    outcome: ParseOutcome,
) -> Selection | None:
    """Which side this selection prices, from two witnesses that must agree.

    ``MarketSelection.type`` is one and ``participant.id`` is the other, and
    they are independent: the first is a label on the outcome, the second is the
    same identifier the event used for its own two competitors.  Requiring
    agreement is what makes this venue's stated orientation trustworthy — and it
    is the third witness against the market whose type says ``MONEYLINE`` and
    whose selections are named "Yes" and "No", because "Yes" is not a competitor
    in this fixture whatever its type claims.
    """
    if market_kind is Market.TOTAL:
        if sel_type == "OVER":
            return Selection.OVER
        if sel_type == "UNDER":
            return Selection.UNDER
        outcome.skipped[f"unknown_total_side:{sel_type or 'null'}"] += 1
        return None

    named = str(((sel.get("participant") or {}).get("id")) or "")
    if sel_type == "DRAW":
        if named:
            outcome.reject(
                source,
                "draw_leg_names_a_competitor",
                f"event {fixture.event_id}: a DRAW selection carries participant {named!r}",
                event_id=fixture.event_id,
            )
            return None
        return Selection.DRAW

    side = SELECTION_SIDES.get(sel_type)
    if side is None:
        outcome.reject(
            source,
            "unresolved_selection",
            f"event {fixture.event_id}: type={sel_type!r} "
            f"name={((sel.get('name') or {}).get('fullName'))!r}",
            event_id=fixture.event_id,
        )
        return None

    stated = fixture.side_of(named)
    if stated is None:
        outcome.reject(
            source,
            "selection_is_not_a_competitor",
            f"event {fixture.event_id}: {sel_type} priced "
            f"{((sel.get('name') or {}).get('fullName'))!r}, whose participant "
            f"{named or 'is absent'} is neither side of this fixture",
            event_id=fixture.event_id,
        )
        return None
    if stated is not side:
        outcome.reject(
            source,
            "orientation_witnesses_disagree",
            f"event {fixture.event_id}: type says {sel_type} and the participant "
            f"says {stated.value}",
            event_id=fixture.event_id,
        )
        return None

    # ``HOME_``/``AWAY_`` name *the book's* sides; our_side restates them
    # against ours, which is the same answer only where the league has a real
    # home side.
    return fixture.our_side(side)


def _decimal_odds(sel: Mapping[str, Any]) -> float | None:
    """``numeratorLong / denominatorLong`` — the exact decimal odds as a rational.

    Never ``formattedOdds``.  That field is exact on every board price measured
    (85 of 85 on 2026-08-13) and is still a display string the venue may change
    without notice — and it already rounds where the venue computes a price
    (``13387/2000 = 6.6935`` shown as ``+569``).
    """
    odds = sel.get("odds")
    if not isinstance(odds, dict):
        return None
    try:
        numerator = float(odds["numeratorLong"])
        denominator = float(odds["denominatorLong"])
    except (KeyError, TypeError, ValueError):
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def _line_for(sel: Mapping[str, Any], market_kind: Market) -> float | None:
    """The handicap, already signed per selection by the venue.

    Unlike Hard Rock, this adapter applies no sign convention of its own:
    ``decimalPoints`` arrives ``-3.5`` on the favourite and ``+3.5`` on the
    underdog, and identical on both legs of a total.  That is asserted rather
    than assumed — see :func:`_require_mirrored_lines`.
    """
    if market_kind is Market.MONEYLINE:
        return None
    points = sel.get("points")
    if not isinstance(points, dict):
        return None
    try:
        return float(points["decimalPoints"])
    except (KeyError, TypeError, ValueError):
        return None


def _require_mirrored_lines(
    outcome: ParseOutcome,
    first_index: int,
    source: str,
    market_kind: Market,
    fixture: Fixture,
    market: Mapping[str, Any],
) -> None:
    """Refuse a handicap whose two legs do not mirror, rather than publishing it.

    The venue signs each leg itself, so this adapter never computes the mirror —
    which means nothing here would notice if the two legs stopped agreeing.  A
    spread whose sides read ``-3.5`` and ``+2.5`` is two different bets wearing
    one market's name, and against another book's honest pair it reads as an
    edge that is not there.
    """
    if market_kind is Market.MONEYLINE:
        return
    produced = outcome.quotes[first_index:]
    lines = {quote.selection: quote.line for quote in produced}
    home, away = lines.get(Selection.HOME), lines.get(Selection.AWAY)
    over, under = lines.get(Selection.OVER), lines.get(Selection.UNDER)
    if market_kind is Market.SPREAD and home is not None and away is not None:
        mirrored = home == -away
    elif market_kind is Market.TOTAL and over is not None and under is not None:
        mirrored = over == under
    else:
        return
    if mirrored:
        return
    outcome.reject(
        source,
        "handicap_legs_do_not_mirror",
        f"event {fixture.event_id}: market {market.get('name')!r} priced "
        f"{sorted((str(k.value), v) for k, v in lines.items())}",
        event_id=fixture.event_id,
    )
    del outcome.quotes[first_index:]


def _require_the_draw_leg(
    outcome: ParseOutcome,
    first_index: int,
    source: str,
    market_kind: Market,
    fixture: _Fixture,
    market: Mapping[str, Any],
) -> None:
    """Refuse a three-way moneyline that lost its draw, rather than shipping two legs.

    This is the one incompleteness that is not merely a missing row.  A soccer
    moneyline priced on two outcomes is *byte-identical* to a genuine two-way
    market, so a dropped draw does not look like a gap — it looks like a
    complete market whose two prices happen to leave 30% of the probability
    unbacked, and staking both legs against another book turns a floor of zero
    into minus the whole stake.  Hard Rock refuses the same shape from the other
    direction, by never merging ``FT:ML`` into ``FT:1X2``.

    Two-sided markets are deliberately not held to this: a suspended leg there
    leaves a single honest price that pairs against another book, and dropping
    it would cost coverage to prevent nothing.
    """
    if market_kind is not Market.MONEYLINE or fixture.sport is not Sport.SOCCER:
        return
    produced = outcome.quotes[first_index:]
    if not produced or any(q.selection is Selection.DRAW for q in produced):
        return
    outcome.reject(
        source,
        "three_way_moneyline_without_a_draw",
        f"event {fixture.event_id}: market {market.get('name')!r} produced "
        f"{sorted(q.selection.value for q in produced)} and no draw",
        event_id=fixture.event_id,
    )
    del outcome.quotes[first_index:]


__all__ = [
    "SOURCE_KEY",
    "TheScoreAdapter",
    "parse_thescore",
    "redact_anonymous_token",
]
