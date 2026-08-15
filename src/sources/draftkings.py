"""DraftKings US sportsbook pregame markets, from the public sportsbook APIs.

Two older payload generations remain supported **for replay only**, because
captures of both are committed: ``api/v5/eventgroups/{id}`` and the
``leagueSubcategory/v1`` shape that replaced it.  Neither is fetched any more.

Live Illinois pages read ``api/sportscontent/.../primaryMarkets/v1``, and that
is the only route this adapter opens.  It takes a league id and two constant
queries — ``type eq 'Fixture'`` for events and the ``PrimaryMarket`` tag for
markets — so there is no per-league subcategory id to keep current.  That
matters: subcategory ids move, and the one pinned for NFL (``10500``) had gone
stale in a way that returned HTTP 200 carrying a 38-way season futures market
("NFL 2026/27 Season", participants *California*, *Maryland*, *Texas*…) instead
of game lines.  The parser correctly refused to build rows from it, the scope
counted as healthy, and NFL silently contributed nothing for a whole run.
``type eq 'Fixture'`` excludes that class of event structurally, and
:func:`_require_game_lines` refuses it rather than reporting success if it ever
returns anyway.  No account is involved.

The Akamai edge used to reject a plain HTTP client even from Illinois, and that
is no longer true of this route: on 2026-08-14 every league in
:data:`EVENT_GROUPS` answered a plain client from the operator's own Illinois
egress, with no browser involved.  The fallback below is kept rather than
removed — the block was real, it was route-specific, and nothing says it will
not return — but it is now the exception rather than the path.  When no client
was injected, a refusal retries through the project's browser transport after
seeding the public league page.  Parsing stays pure and supports all three
generations of captured bytes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from src import leagues as league_registry
from src.events import build_event_key, orient, resolve_doubleheaders
from src.leagues import League
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
    latest_per_endpoint,
    parse_iso_time,
    priced_quote,
)
from src.sources.base import ParseOutcome
from src.sources.guards import CoverageCappedError, FormatChangeError, SourceError

log = logging.getLogger(__name__)

SOURCE_KEY = "draftkings"
#: The state's own content host.  This default is Illinois; a state-scoped run
#: gets its own through ``SourceDescriptor.config`` in :mod:`src.jurisdictions`,
#: and that override is the *only* thing pinning a fetch to one state's licence —
#: so the path here and the path there must name the same API generation.  They
#: did not, briefly: this constant moved to ``primaryMarkets`` while the four
#: per-state configs still said ``leagueSubcategory``, and because the per-state
#: value wins, every ``--state`` run kept using the retired route while the
#: unscoped adapter used the new one.  Both answered, so nothing failed.
DEFAULT_CONTENT_BASE_URL = (
    "https://sportsbook-nash.draftkings.com/sites/US-IL-SB/api/"
    "sportscontent/controldata/league/primaryMarkets/v1"
)
DEFAULT_ORIGIN = "https://sportsbook.draftkings.com/"
HOST_INTERVAL = 0.5

#: League id → (sport, league key).  One request covers the whole league.
#:
#: These are DraftKings *league* ids, and every one of them was confirmed
#: against the route table the public NFL page embeds for itself
#: (``{"route":"/sport/3/league/88808","seoRoute":"/leagues/football/nfl"}``),
#: so none is guessed.
#:
#: ``24685`` is a second football entry and not a duplicate: DraftKings splits
#: *NFL Preseason* from *NFL* as separate leagues, and in August the preseason
#: league is the one holding games that other books are pricing tonight, while
#: ``88808`` starts in September.  Both normalize to the ``NFL`` league key —
#: they are the same competition to everything downstream, and the split is a
#: DraftKings shelving decision rather than a fact about the sport.
EVENT_GROUPS: tuple[tuple[int, Sport, str], ...] = (
    (84240, Sport.BASEBALL, "MLB"),
    (94682, Sport.BASKETBALL, "WNBA"),
    (42648, Sport.BASKETBALL, "NBA"),
    (88808, Sport.FOOTBALL, "NFL"),
    (24685, Sport.FOOTBALL, "NFL"),
    (42133, Sport.HOCKEY, "NHL"),
    (40253, Sport.SOCCER, "EPL"),
)

#: League id → public page path, used only for the ``Referer`` a browser sends
#: and as the seed URL when the plain client is turned away.  A league missing
#: here still collects; it just sends the bare origin.
PAGE_PATHS: dict[int, str] = {
    84240: "baseball/mlb",
    94682: "basketball/wnba",
    42648: "basketball/nba",
    88808: "football/nfl",
    24685: "football/nfl-preseason",
    42133: "hockey/nhl",
    40253: "soccer/england---premier-league",
}

#: Most events one request will return.  The page asks for 20; 100 is served and
#: ``300`` is refused with ``HTTP 400 MRKTBFF-400``, so this is the venue's own
#: ceiling rather than a number chosen here.  A league that fills a page is
#: paged past it — see :data:`MAX_EVENT_PAGES` — because a silent cap reads as
#: "that is the whole slate" when it is not.
EVENT_PAGE_CAP = 100

#: Full pages fetched per league before stopping.  Paging reuses the venue's
#: own grammar: the page's ``subscriptionPartials`` echo shows DraftKings' own
#: client issuing ``sortOrder gt N`` range predicates against this same
#: endpoint (captured 2026-08-14, ``sportscontent-88808``), so a follow-up page
#: asks for exactly what the site asks for, anchored past the last event
#: already held.  ``top`` itself stays at :data:`EVENT_PAGE_CAP` — see above.
#: 4 × 100 events outruns any configured league's real pregame slate; a league
#: that fills every page is reported truncated rather than assumed complete.
MAX_EVENT_PAGES = 4

MARKETS_BY_SPORT: dict[Sport, frozenset[Market]] = {
    Sport.BASEBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.BASKETBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.FOOTBALL: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    Sport.HOCKEY: frozenset({Market.MONEYLINE, Market.SPREAD, Market.TOTAL}),
    # The ``primaryMarkets`` route serves what the league page's board shows.
    # For the US sports that is moneyline, spread and total; for soccer the
    # board is the moneyline alone — run 16's stored EPL page carries ten
    # ``Moneyline`` markets and nothing else.  Claiming totals here (a
    # holdover from the retired v5 route, which did serve them) made
    # ``core_market_absent`` grade this route's normal answer an ERROR.
    Sport.SOCCER: frozenset({Market.MONEYLINE}),
}

#: DK market ``label`` → (Market, Period).  Only full-game Game Lines.
MARKET_LABELS: dict[str, tuple[Market, Period]] = {
    "moneyline": (Market.MONEYLINE, Period.FULL_GAME),
    "spread": (Market.SPREAD, Period.FULL_GAME),
    "run line": (Market.SPREAD, Period.FULL_GAME),
    "puck line": (Market.SPREAD, Period.FULL_GAME),
    "total": (Market.TOTAL, Period.FULL_GAME),
    "total points": (Market.TOTAL, Period.FULL_GAME),
    "total goals": (Market.TOTAL, Period.FULL_GAME),
    "total runs": (Market.TOTAL, Period.FULL_GAME),
}

#: Event statuses that mean the game is no longer a pregame offer.
LIVE_STATES = frozenset({
    "STARTED", "IN_PROGRESS", "LIVE", "FINAL", "ENDED", "CLOSED", "POSTPONED",
})


@dataclass(frozen=True)
class _GroupScope:
    event_group_id: int
    sport: Sport
    league: str


class DraftKingsAdapter:
    """Collects DraftKings Illinois pregame game markets for configured leagues."""

    def __init__(
        self,
        leagues: Sequence[str] | None = None,
        *,
        source_key: str = SOURCE_KEY,
        content_base_url: str = DEFAULT_CONTENT_BASE_URL,
        timeout: float = 25.0,
        client: httpx.Client | None = None,
        proxy_state: str | None = None,
    ) -> None:
        wanted = (
            frozenset(leagues)
            if leagues is not None
            else frozenset(league for _, _, league in EVENT_GROUPS)
        )
        scopes = [
            _GroupScope(gid, sport, league)
            for gid, sport, league in EVENT_GROUPS
            if league in wanted
        ]
        if not scopes:
            raise ValueError(
                f"DraftKingsAdapter has no event group for leagues {sorted(wanted)!r}"
            )
        self._scopes = tuple(scopes)
        self._wanted = wanted
        self._source_key = source_key
        self.content_base_url = content_base_url.rstrip("/")
        self.proxy_state = proxy_state
        self._allow_browser_fallback = client is None
        self._browser_http: SourceClient | None = None
        self._http = SourceClient(
            source_key,
            timeout=timeout,
            client=client,
            host_interval=HOST_INTERVAL,
            proxy_state=proxy_state,
        )

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def leagues(self) -> tuple[str, ...]:
        # ``dict.fromkeys`` rather than ``set``: two scopes can share a league
        # key — NFL and NFL Preseason are separate DraftKings leagues and one
        # normalized competition — and this is a declaration of what the adapter
        # collects, which must name each league once and in a stable order.
        return tuple(
            dict.fromkeys(
                scope.league for scope in self._scopes if scope.league in self._wanted
            )
        )

    def capabilities(self, *, tier: Tier = Tier.FULL) -> dict[str, frozenset[Market]]:
        del tier
        return capabilities_from(
            {
                scope.league: MARKETS_BY_SPORT[scope.sport]
                for scope in self._scopes
                if scope.league in self._wanted
            },
            self.leagues,
        )

    def fetch_raw(self, *, tier: Tier = Tier.FULL) -> list[RawResponse]:
        del tier
        raws: list[RawResponse] = []
        tally = self.last_fetch = ScopeTally(self._source_key)
        for scope in self._scopes:
            label = f"eventgroup:{scope.event_group_id}"
            tally.requested(label)
            collected = 0
            after: int | None = None
            failed = False
            capped: str | None = None
            for page_index in range(MAX_EVENT_PAGES):
                try:
                    raw = self._fetch_scope(scope, page=page_index, after=after)
                    _require_game_lines(raw, self._source_key)
                except SourceError as exc:
                    log.info(
                        "%s: event group %s unavailable: %s",
                        self._source_key, scope.event_group_id, exc,
                    )
                    if collected == 0:
                        tally.failed(label, exc)
                        failed = True
                    else:
                        # Pages already paid for stay; a later page failing is
                        # a short slate, not a dead scope.
                        capped = (
                            f"the page past sortOrder {after} failed after "
                            f"{collected} event(s) were collected"
                        )
                    break
                raws.append(raw)
                count = _event_count(raw)
                collected += count
                if count < EVENT_PAGE_CAP:
                    break
                cursor = _max_sort_order(raw)
                if cursor is None or (after is not None and cursor <= after):
                    # A full page whose events carry no advancing cursor cannot
                    # be paged past; say what was lost instead of looping.
                    capped = (
                        f"a full page of {EVENT_PAGE_CAP} events carries no "
                        f"advancing sortOrder cursor, so the rest was not "
                        f"collected"
                    )
                    break
                after = cursor
            else:
                capped = (
                    f"{MAX_EVENT_PAGES} pages of {EVENT_PAGE_CAP} events all "
                    f"filled, so this league's slate is cut off at {collected} "
                    f"and the rest was not collected"
                )
            if failed:
                continue
            tally.produced(label, collected)
            if capped:
                tally.truncated(
                    label,
                    CoverageCappedError(f"{self._source_key}:{label}: {capped}"),
                )
        tally.require_something(what="pregame eventgroup")
        return raws

    def _fetch_scope(
        self, scope: _GroupScope, *, page: int = 0, after: int | None = None
    ) -> RawResponse:
        page_path = PAGE_PATHS.get(scope.event_group_id)
        page_url = f"{DEFAULT_ORIGIN}leagues/{page_path}" if page_path else DEFAULT_ORIGIN
        # The page's own provider parameters, with only ``top`` raised from the
        # 20 a screen needs to the 100 the endpoint will serve, and — past the
        # first page — the same ``sortOrder gt`` anchor the site's own
        # subscription queries carry.
        events_query = (
            f"$filter=leagueId eq '{scope.event_group_id}' AND type eq 'Fixture'"
        )
        if after is not None:
            events_query += f" and sortOrder gt {after}"
        endpoint = f"sportscontent-{scope.event_group_id}"
        if page:
            endpoint = f"{endpoint}-p{page + 1}"
        params = {
            "eventsQuery": events_query,
            "marketsQuery": "$filter=tags/any(t: t eq 'PrimaryMarket')",
            "top": str(EVENT_PAGE_CAP),
            "include": "Events",
            "entity": "events",
            "isBatchable": "true",
        }
        headers = {"Origin": DEFAULT_ORIGIN.rstrip("/"), "Referer": page_url}
        # One request, two possible clients.  Spelling the call out twice let the
        # plain and browser paths drift, which is the failure mode that matters
        # here: the fallback exists to send *the same* request through a
        # different transport, and a fallback that quietly asks for something
        # else is worse than no fallback.
        def fetch(http: SourceClient) -> RawResponse:
            return http.get(
                f"{self.content_base_url}/markets",
                endpoint=endpoint,
                params=params,
                headers=headers,
            )

        try:
            return fetch(self._http)
        except SourceError:
            if not self._allow_browser_fallback:
                raise
        if self._browser_http is None:
            from src.sources.browser import build_browser_client

            self._browser_http = SourceClient(
                self._source_key,
                client=build_browser_client(
                    timeout=25.0,
                    seed_url=page_url,
                    state=self.proxy_state,
                ),
                host_interval=HOST_INTERVAL,
            )
        return fetch(self._browser_http)

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        return parse_draftkings(raws)

    def close(self) -> None:
        self._http.close()
        if self._browser_http is not None:
            self._browser_http.close()


def _require_game_lines(raw: RawResponse, source: str) -> None:
    """Refuse a 200 that carries something other than this league's game lines.

    The failure this exists for produced no error anywhere: a stale subcategory
    id answered ``200`` with one 38-way "NFL 2026/27 Season" futures market, the
    parser declined to build rows from it because no participant pairing could
    be made, and the scope was filed as *produced* — one event, healthy.  The
    run reported ``draftkings ok=1`` while NFL contributed nothing.

    So "answered" is not enough; the answer has to be the kind of thing that was
    asked for.  A league with genuinely no games is a different case and must
    stay an *empty* scope rather than a failure, which is why an empty event
    list returns quietly here and :meth:`ScopeTally.produced` grades it.
    """
    try:
        payload = raw.json()
    except ValueError as exc:
        raise FormatChangeError(f"{source}:{raw.endpoint}: body is not JSON") from exc
    if not isinstance(payload, dict):
        return  # the parser raises on this with a better message
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return  # no slate is not a defect; ``produced`` files it as empty

    fixtures = [
        event
        for event in events
        if isinstance(event, Mapping)
        and {
            str(p.get("venueRole") or "")
            for p in event.get("participants") or []
            if isinstance(p, Mapping)
        }
        >= {"Home", "Away"}
    ]
    if not fixtures:
        shapes = sorted(
            {
                str(event.get("eventParticipantType") or "?")
                for event in events
                if isinstance(event, Mapping)
            }
        )
        names = [
            str(event.get("name")) for event in events[:3] if isinstance(event, Mapping)
        ]
        raise FormatChangeError(
            f"{source}:{raw.endpoint}: {len(events)} event(s) and not one is a "
            f"two-sided fixture (participant shapes: {', '.join(shapes)}; "
            f"e.g. {', '.join(repr(n) for n in names)}) — this route is answering "
            f"with something other than game lines"
        )

    markets = payload.get("markets")
    if isinstance(markets, list) and markets:
        named = {
            str(market.get("name") or "").casefold()
            for market in markets
            if isinstance(market, Mapping)
        }
        if not (named & set(MARKET_LABELS)):
            raise FormatChangeError(
                f"{source}:{raw.endpoint}: no market maps to a game line "
                f"(saw {', '.join(sorted(named)) or 'none'}) — this route is "
                f"answering with something other than game lines"
            )


def _event_count(raw: RawResponse) -> int:
    try:
        payload = raw.json()
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    events = payload.get("events")
    if not isinstance(events, list):
        events = (payload.get("eventGroup") or {}).get("events")
    return len(events) if isinstance(events, list) else 0


def _max_sort_order(raw: RawResponse) -> int | None:
    """The keyset cursor: the largest ``sortOrder`` among a page's events.

    Only the current ``sportscontent`` shape carries it; a page without one
    cannot be paged past, and the caller reports that instead of guessing.
    """
    try:
        payload = raw.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    orders = [
        event.get("sortOrder")
        for event in events
        if isinstance(event, Mapping) and isinstance(event.get("sortOrder"), int)
    ]
    return max(orders) if orders else None


def parse_draftkings(raws: Sequence[RawResponse]) -> ParseOutcome:
    """Pure: no network, no clock, no filesystem.  Source from the envelope."""
    outcome = ParseOutcome()
    source = envelope_source(raws, fallback=SOURCE_KEY)
    fixtures: dict[str, Fixture] = {}
    work: list[tuple[RawResponse, Mapping[str, Any], Fixture, list[Mapping[str, Any]]]] = []

    for raw in latest_per_endpoint(raws):
        if not raw.endpoint.startswith(("eventgroup", "sportscontent")):
            continue
        payload = raw.json()
        if not isinstance(payload, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: expected object")
        group = payload.get("eventGroup")
        if group is None and raw.endpoint.startswith("sportscontent"):
            group = _eventgroup_from_sportscontent(payload, raw.endpoint)
        if not isinstance(group, dict):
            raise FormatChangeError(f"{source}:{raw.endpoint}: missing eventGroup")

        league = _league_for_group(group, raw.endpoint)
        if league is None:
            outcome.skipped["eventgroup_out_of_scope"] += 1
            continue
        competition = league_registry.league(league)

        by_id: dict[str, Mapping[str, Any]] = {}
        for event in group.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("eventId") or "")
            if event_id:
                by_id[event_id] = event

        offers_by_event = _game_line_offers(group)
        for event_id, markets in offers_by_event.items():
            event = by_id.get(event_id)
            if event is None:
                outcome.skipped["offer_without_event"] += 1
                continue
            fixture = _accept_event(event, competition, source, raw.fetched_at, outcome)
            if fixture is None:
                continue
            fixtures[event_id] = fixture
            work.append((raw, event, fixture, markets))

    if not fixtures:
        return outcome

    resolved = resolve_doubleheaders(
        {eid: (f.base_key, f.commence_time) for eid, f in fixtures.items()}
    )
    for raw, _event, fixture, markets in work:
        event_key = resolved[fixture.event_id]
        for market in markets:
            _emit_market(market, raw, source, fixture, event_key, outcome)

    drop_duplicate_selections(source, outcome)
    return outcome


def _eventgroup_from_sportscontent(
    payload: Mapping[str, Any], endpoint: str
) -> Mapping[str, Any]:
    """Translate the current normalized DK store into the replay-stable shape."""
    # ``sportscontent-88808`` and its keyset pages ``sportscontent-88808-p2``…
    # both name the league between the route and any page suffix.
    match = re.search(r"sportscontent-(\d+)", endpoint)
    if match is None:
        raise FormatChangeError(f"draftkings:{endpoint}: missing league id")
    group_id = int(match.group(1))
    events = payload.get("events")
    markets = payload.get("markets")
    selections = payload.get("selections")
    if not all(isinstance(rows, list) for rows in (events, markets, selections)):
        raise FormatChangeError(
            f"draftkings:{endpoint}: expected events/markets/selections lists"
        )

    converted_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        participants = event.get("participants") or []
        away = next(
            (p for p in participants if isinstance(p, Mapping) and p.get("venueRole") == "Away"),
            {},
        )
        home = next(
            (p for p in participants if isinstance(p, Mapping) and p.get("venueRole") == "Home"),
            {},
        )
        converted_events.append(
            {
                "eventId": event.get("id"),
                "name": event.get("name"),
                "teamName1": away.get("name"),
                "teamName2": home.get("name"),
                "startDate": event.get("startEventDate"),
                "eventStatus": {"state": event.get("status")},
            }
        )

    by_market: dict[str, list[Mapping[str, Any]]] = {}
    for selection in selections:
        if isinstance(selection, Mapping) and selection.get("marketId"):
            by_market.setdefault(str(selection["marketId"]), []).append(selection)
    converted_markets: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, Mapping):
            continue
        tags = set(market.get("tags") or [])
        converted_outcomes = []
        for selection in by_market.get(str(market.get("id") or ""), []):
            display = selection.get("displayOdds") or {}
            american = str(display.get("american") or "").replace("−", "-")
            # ``trueOdds`` is the price; ``displayOdds.decimal`` is what the web
            # page prints, rounded to two places.  Taking the printed one first
            # recorded a different number from the one the same row's American
            # value encodes: measured on the 2026-08-08 Pennsylvania capture,
            # ``trueOdds`` agrees with the published American price on **122 of
            # 122** selections and ``displayOdds.decimal`` on only about two-thirds
            # of them (66-68 by metric), drifting up to
            # 2.06% — ``-213`` printed as ``1.46`` against a true 1.46948357.
            #
            # Validation caught it as ``odds_format_mismatch`` on 14 markets and
            # failed the run, which is the only reason this was ever visible: the
            # rounding is always *downward*, so it understates the payout, and an
            # understated payout is an arbitrage that quietly does not get
            # reported rather than one that wrongly does.
            #
            # ``is not None`` rather than ``or``, because ``trueOdds`` is a float
            # and 0.0 would fall through to the display string instead of being
            # refused by the plausibility check below.
            true_odds = selection.get("trueOdds")
            converted_outcomes.append(
                {
                    "id": selection.get("id"),
                    "label": selection.get("label"),
                    "oddsDecimal": (
                        true_odds if true_odds is not None else display.get("decimal")
                    ),
                    "oddsAmerican": american,
                    "line": selection.get("points"),
                    "hidden": False,
                }
            )
        converted_markets.append(
            {
                "id": market.get("id"),
                "eventId": market.get("eventId"),
                "label": market.get("name"),
                "isSuspended": False,
                "isOpen": True,
                "main": "PrimaryMarket" in tags,
                "outcomes": converted_outcomes,
            }
        )

    return {
        "eventGroupId": group_id,
        "events": converted_events,
        "offerCategories": [
            {
                "name": "Game Lines",
                "offerSubcategoryDescriptors": [
                    {
                        "name": "Game",
                        "offerSubcategory": {"offers": [converted_markets]},
                    }
                ],
            }
        ],
    }


def _league_for_group(group: Mapping[str, Any], endpoint: str) -> str | None:
    gid = group.get("eventGroupId")
    try:
        gid_int = int(gid)
    except (TypeError, ValueError):
        gid_int = None
    for eg_id, _sport, league in EVENT_GROUPS:
        if gid_int == eg_id or endpoint.endswith(str(eg_id)):
            return league
    name = str(group.get("name") or "").strip().upper()
    for _eg_id, _sport, league in EVENT_GROUPS:
        if name == league or name.replace(" ", "_") == league:
            return league
    return None


def _game_line_offers(group: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Collect open Game-line markets keyed by event id."""
    by_event: dict[str, list[Mapping[str, Any]]] = {}
    for category in group.get("offerCategories") or []:
        if not isinstance(category, dict):
            continue
        if str(category.get("name") or "").casefold() not in {"game lines", "game line"}:
            continue
        for desc in category.get("offerSubcategoryDescriptors") or []:
            if not isinstance(desc, dict):
                continue
            sub_name = str(desc.get("name") or "").casefold()
            if sub_name not in {"game", "games"}:
                continue
            sub = desc.get("offerSubcategory") or {}
            if not isinstance(sub, dict):
                continue
            for bundle in sub.get("offers") or []:
                if not isinstance(bundle, list):
                    continue
                for market in bundle:
                    if not isinstance(market, dict):
                        continue
                    if market.get("isSuspended") or not market.get("isOpen", True):
                        continue
                    if not market.get("main", True):
                        continue
                    event_id = str(market.get("eventId") or "")
                    if not event_id:
                        continue
                    by_event.setdefault(event_id, []).append(market)
    return by_event


def _accept_event(
    event: Mapping[str, Any],
    competition: League,
    source: str,
    captured_at: datetime,
    outcome: ParseOutcome,
) -> Fixture | None:
    event_id = str(event.get("eventId") or "")
    if not event_id:
        outcome.skipped["missing_event_id"] += 1
        return None

    status = event.get("eventStatus") or {}
    state = str(status.get("state") or "").upper()
    if state in LIVE_STATES:
        outcome.skipped[f"status:{state or 'unknown'}"] += 1
        return None

    # DK names Away @ Home; teamName1 is away, teamName2 is home.
    away_name = str(event.get("teamName1") or "").strip()
    home_name = str(event.get("teamName2") or "").strip()
    if not home_name or not away_name:
        # Fall back to splitting the event name.
        name = str(event.get("name") or "")
        if " @ " in name:
            away_name, home_name = (part.strip() for part in name.split(" @ ", 1))
        elif " vs " in name.casefold():
            # soccer-style; order is not reliable — reject rather than guess home.
            outcome.skipped["ambiguous_home_away"] += 1
            return None
        else:
            outcome.skipped["missing_home_away"] += 1
            return None

    if any(is_pairing(part, competition.sport) for part in (home_name, away_name)):
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

    commence_time = parse_iso_time(event.get("startDate"))
    if commence_time is None:
        outcome.reject(source, "missing_commence_time", f"event {event_id}", event_id=event_id)
        return None
    if commence_time <= captured_at:
        outcome.skipped["event_already_started"] += 1
        return None

    away_side, home_side = orient(
        away, home, competition, home=home if competition.has_home_away else None
    )
    return Fixture(
        event_id=event_id,
        sport=competition.sport,
        competition=competition,
        home=home_side,
        away=away_side,
        book_home_key=home.key,
        commence_time=commence_time,
        base_key=build_event_key(away_side.key, home_side.key, commence_time, competition),
    )


def _emit_market(
    market: Mapping[str, Any],
    raw: RawResponse,
    source: str,
    fixture: Fixture,
    event_key: str,
    outcome: ParseOutcome,
) -> None:
    label = str(market.get("label") or "").strip().casefold()
    mapped = MARKET_LABELS.get(label)
    if mapped is None:
        outcome.skipped["market_out_of_scope"] += 1
        return
    market_kind, period = mapped
    if market_kind not in MARKETS_BY_SPORT.get(fixture.sport, frozenset()):
        outcome.skipped["market_out_of_scope"] += 1
        return

    for outcome_row in market.get("outcomes") or []:
        if not isinstance(outcome_row, dict):
            continue
        if outcome_row.get("hidden"):
            outcome.skipped["outcome_hidden"] += 1
            continue
        selection = _selection_for(outcome_row, market_kind, fixture, outcome, source)
        if selection is None:
            continue
        try:
            decimal = float(outcome_row["oddsDecimal"])
        except (KeyError, TypeError, ValueError):
            outcome.reject(
                source,
                "missing_odds",
                f"event {fixture.event_id} {label}",
                event_id=fixture.event_id,
            )
            continue
        if not is_plausible_decimal_odds(decimal):
            outcome.reject(
                source,
                "implausible_odds",
                f"event {fixture.event_id} {label} {decimal}",
                event_id=fixture.event_id,
            )
            continue

        line = outcome_row.get("line")
        line_f: float | None
        if market_kind in (Market.SPREAD, Market.TOTAL):
            try:
                line_f = float(line)
            except (TypeError, ValueError):
                outcome.reject(
                    source,
                    "missing_line",
                    f"event {fixture.event_id} {label}",
                    event_id=fixture.event_id,
                )
                continue
        else:
            line_f = None

        american = decimal_to_american(decimal)
        try:
            american_pub = int(str(outcome_row.get("oddsAmerican") or american))
        except ValueError:
            american_pub = american

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
                    line=line_f,
                    decimal_odds=decimal,
                    american_odds=american_pub,
                    implied_probability=implied_probability(decimal),
                    status=QuoteStatus.ACTIVE,
                )
            )
        except (TypeError, ValueError) as exc:
            outcome.reject(source, "invalid_quote", str(exc), event_id=fixture.event_id)


def _selection_for(
    outcome_row: Mapping[str, Any],
    market_kind: Market,
    fixture: Fixture,
    outcome: ParseOutcome,
    source: str,
) -> Selection | None:
    label = str(outcome_row.get("label") or "").strip()
    if not label:
        outcome.skipped["missing_outcome_label"] += 1
        return None
    folded = label.casefold()
    if market_kind is Market.TOTAL:
        if folded.startswith("over"):
            return Selection.OVER
        if folded.startswith("under"):
            return Selection.UNDER
        outcome.skipped["unknown_total_side"] += 1
        return None

    # Moneyline / spread: match participant by label.
    participant = canonical_participant(label, fixture.competition)
    if participant is None:
        outcome.reject(
            source,
            "unresolved_selection",
            f"event {fixture.event_id}: {label!r}",
            event_id=fixture.event_id,
        )
        return None
    if participant.key == fixture.home.key:
        return Selection.HOME
    if participant.key == fixture.away.key:
        return Selection.AWAY
    if folded in {"draw", "tie", "x"}:
        return Selection.DRAW
    outcome.reject(
        source,
        "selection_not_on_fixture",
        f"event {fixture.event_id}: {label!r}",
        event_id=fixture.event_id,
    )
    return None


__all__ = [
    "DraftKingsAdapter",
    "EVENT_GROUPS",
    "SOURCE_KEY",
    "parse_draftkings",
]
