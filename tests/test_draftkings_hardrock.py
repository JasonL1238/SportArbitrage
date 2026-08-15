"""Offline parse pins for DraftKings and Hard Rock first-party adapters."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.raw_store import RawResponse, RawStore
from src.schema import Market, Selection
from src.sources.caesars import parse_caesars
from src.sources.draftkings import (
    EVENT_GROUPS,
    DraftKingsAdapter,
    _require_game_lines,
    parse_draftkings,
)
from src.sources.guards import FormatChangeError
from src.sources.hardrock import parse_hardrock

RAW = Path(__file__).resolve().parent / "fixtures" / "raw"
LIVE_REGRESSIONS = Path(__file__).resolve().parent / "fixtures" / "live_regressions"


def _load(pattern: str) -> RawResponse:
    path = next(RAW.glob(pattern))
    env = json.loads(path.read_text())
    return RawResponse(
        source=env["source"],
        endpoint=env["endpoint"],
        url=env["url"],
        status_code=env["status_code"],
        body=env["body"],
        fetched_at=datetime.fromisoformat(env["fetched_at"]),
        content_type=env.get("content_type"),
        request_params=env.get("request_params") or {},
        headers=env.get("headers") or {},
    )


def test_draftkings_parses_game_lines_from_fixture() -> None:
    raw = _load("draftkings__*_eventgroup-42648_*.json")
    # Fixture commence times are in August 2026; observe before that.
    raw = RawResponse(
        source=raw.source,
        endpoint=raw.endpoint,
        url=raw.url,
        status_code=raw.status_code,
        body=raw.body,
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        content_type=raw.content_type,
        request_params=raw.request_params,
        headers=raw.headers,
    )
    outcome = parse_draftkings([raw])
    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]
    assert len(outcome.quotes) == 12
    markets = {q.market for q in outcome.quotes}
    assert markets == {Market.MONEYLINE, Market.SPREAD, Market.TOTAL}
    # Away @ Home: BKN @ PHI → home is Philadelphia
    phi = [q for q in outcome.quotes if q.home_participant == "NBA-PHI"]
    assert phi
    assert all(q.league == "NBA" for q in outcome.quotes)


def test_draftkings_parses_current_sportscontent_shape() -> None:
    """The live Illinois page replaced v5 eventgroups with normalized stores."""
    payload = {
        "events": [{
            "id": "34469547", "name": "WAS Nationals @ PHI Phillies",
            "startEventDate": "2026-08-03T22:40:00Z", "status": "NOT_STARTED",
            "participants": [
                {"name": "PHI Phillies", "venueRole": "Home"},
                {"name": "WAS Nationals", "venueRole": "Away"},
            ],
        }],
        "markets": [
            {"id": "ml", "eventId": "34469547", "name": "Moneyline", "tags": ["PrimaryMarket"]},
            {"id": "rl", "eventId": "34469547", "name": "Run Line", "tags": ["PrimaryMarket"]},
            {"id": "to", "eventId": "34469547", "name": "Total", "tags": ["PrimaryMarket"]},
        ],
        "selections": [
            {"id": "a-ml", "marketId": "ml", "label": "WAS Nationals", "displayOdds": {"american": "+144", "decimal": "2.44"}},
            {"id": "h-ml", "marketId": "ml", "label": "PHI Phillies", "displayOdds": {"american": "−175", "decimal": "1.57"}},
            {"id": "a-rl", "marketId": "rl", "label": "WAS Nationals", "points": 1.5, "displayOdds": {"american": "-143", "decimal": "1.69"}},
            {"id": "h-rl", "marketId": "rl", "label": "PHI Phillies", "points": -1.5, "displayOdds": {"american": "+119", "decimal": "2.19"}},
            {"id": "o", "marketId": "to", "label": "Over", "points": 9.5, "displayOdds": {"american": "+102", "decimal": "2.02"}},
            {"id": "u", "marketId": "to", "label": "Under", "points": 9.5, "displayOdds": {"american": "-122", "decimal": "1.81"}},
        ],
    }
    raw = RawResponse(
        source="draftkings", endpoint="sportscontent-84240",
        url="https://sportsbook-nash.draftkings.com/example", status_code=200,
        body=json.dumps(payload), fetched_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        content_type="application/json",
    )
    outcome = parse_draftkings([raw])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE, Market.SPREAD, Market.TOTAL,
    }
    by_market_selection = {(q.market, q.selection): q for q in outcome.quotes}
    assert by_market_selection[Market.SPREAD, Selection.AWAY].line == 1.5
    assert by_market_selection[Market.SPREAD, Selection.HOME].line == -1.5
    assert by_market_selection[Market.TOTAL, Selection.UNDER].line == 9.5


def test_draftkings_takes_true_odds_over_the_printed_decimal() -> None:
    """``trueOdds`` is the price; ``displayOdds.decimal`` is its 2dp rendering.

    The two disagree in the payload itself, and the American value is the
    referee: on the 2026-08-08 Pennsylvania capture ``trueOdds`` agreed with the
    published American price on 122 of 122 selections and the printed decimal on
    67, drifting up to 2.06% — ``−213`` printed as ``1.46`` against a true
    1.46948357.  Preferring the printed one failed the live run with fourteen
    ``odds_format_mismatch`` errors; before validation caught it, the rounding
    — always downward — quietly understated every payout.
    """
    payload = {
        "events": [{
            "id": "1", "name": "WAS Nationals @ PHI Phillies",
            "startEventDate": "2026-08-03T22:40:00Z", "status": "NOT_STARTED",
            "participants": [
                {"name": "PHI Phillies", "venueRole": "Home"},
                {"name": "WAS Nationals", "venueRole": "Away"},
            ],
        }],
        "markets": [
            {"id": "ml", "eventId": "1", "name": "Moneyline", "tags": ["PrimaryMarket"]},
        ],
        "selections": [
            # The live disagreement, verbatim: −213 encodes 1.46948357, printed 1.46.
            {"id": "h", "marketId": "ml", "label": "PHI Phillies", "trueOdds": 1.46948357,
             "displayOdds": {"american": "−213", "decimal": "1.46"}},
            {"id": "a", "marketId": "ml", "label": "WAS Nationals", "trueOdds": 2.31,
             "displayOdds": {"american": "+131", "decimal": "2.31"}},
        ],
    }
    raw = RawResponse(
        source="draftkings", endpoint="sportscontent-84240",
        url="https://sportsbook-nash.draftkings.com/example", status_code=200,
        body=json.dumps(payload), fetched_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        content_type="application/json",
    )
    outcome = parse_draftkings([raw])
    assert not outcome.rejections
    by_sel = {q.selection: q for q in outcome.quotes}
    assert abs(by_sel[Selection.HOME].decimal_odds - 1.46948357) < 1e-9, (
        "the row must carry the price, not the page's rounding of it"
    )
    assert by_sel[Selection.HOME].american_odds == -213
    # And a payload with no trueOdds still parses off the printed value — the
    # older shape this converter was written for.
    for sel in payload["selections"]:
        del sel["trueOdds"]
    raw2 = RawResponse(
        source="draftkings", endpoint="sportscontent-84240",
        url="https://sportsbook-nash.draftkings.com/example", status_code=200,
        body=json.dumps(payload), fetched_at=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
        content_type="application/json",
    )
    fallback = parse_draftkings([raw2])
    assert not fallback.rejections
    assert {q.decimal_odds for q in fallback.quotes} == {1.46, 2.31}


def test_draftkings_parses_the_primary_markets_route() -> None:
    """The live route: a genuine WNBA capture from ``primaryMarkets/v1``.

    Pins the shape the adapter actually fetches, as opposed to the two older
    generations the parser keeps only so their committed captures still replay.
    """
    raw = _load("draftkings__*_sportscontent-94682_*.json")
    early = datetime(2026, 8, 14, tzinfo=timezone.utc)
    raw = RawResponse(
        source=raw.source, endpoint=raw.endpoint, url=raw.url,
        status_code=raw.status_code, body=raw.body, fetched_at=early,
        content_type=raw.content_type, request_params=raw.request_params,
        headers=raw.headers,
    )
    # The request carries no subcategory id: markets are chosen by tag, and
    # events by ``type eq 'Fixture'``.  That is what makes the route immune to
    # the renumbering that broke NFL.
    assert "subCategoryId" not in json.dumps(raw.request_params)
    assert raw.request_params["marketsQuery"].endswith("t eq 'PrimaryMarket')")
    assert "type eq 'Fixture'" in raw.request_params["eventsQuery"]

    outcome = parse_draftkings([raw])
    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]
    assert outcome.quotes
    assert {q.league for q in outcome.quotes} == {"WNBA"}
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE, Market.SPREAD, Market.TOTAL,
    }


def test_draftkings_refuses_a_futures_payload_that_answered_200() -> None:
    """The exact regression: HTTP 200, well formed, and not game lines.

    This body is the real one DraftKings served on 2026-08-14 for the stale NFL
    subcategory ``10500`` — one 38-way "NFL 2026/27 Season" market whose
    participants are US states.  The parser already declined to build rows from
    it; what was missing is that the *scope* counted as healthy, so the run said
    ``draftkings ok=1`` while NFL contributed nothing.  The guard must call it a
    failure.
    """
    raw = _load("draftkings__*_sportscontent-88808-futures_*.json")
    assert raw.status_code == 200, "the point of this fixture is that it is a 200"

    payload = json.loads(raw.body)
    assert len(payload["events"]) == 1
    assert payload["events"][0]["eventParticipantType"] == "MultiTeam"

    with pytest.raises(FormatChangeError) as caught:
        _require_game_lines(raw, "draftkings")
    message = str(caught.value)
    assert "not one is a two-sided fixture" in message
    assert "MultiTeam" in message
    assert "NFL 2026/27 Season" in message

    # And the parser's own behaviour is unchanged: it still builds nothing,
    # rather than inventing rows from a futures market.
    assert not parse_draftkings([raw]).quotes


def test_draftkings_empty_slate_is_not_a_failure() -> None:
    """A league with no games must stay an *empty* scope, not a refusal.

    The distinction is the whole reason the guard returns quietly on an empty
    event list: "no games today" and "this route is broken" produce the same
    zero, and grading the first as a failure would fail every run in an
    off-season.
    """
    raw = RawResponse(
        source="draftkings", endpoint="sportscontent-42648",
        url="https://sportsbook-nash.draftkings.com/example", status_code=200,
        body=json.dumps({"events": [], "markets": [], "selections": []}),
        fetched_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        content_type="application/json",
    )
    _require_game_lines(raw, "draftkings")  # must not raise


def test_draftkings_registers_nfl_preseason_as_the_same_competition() -> None:
    """Two DraftKings leagues, one normalized competition.

    DraftKings shelves *NFL Preseason* (24685) apart from *NFL* (88808).  In
    August the preseason league holds the games other books are pricing, so both
    are collected — but ``leagues`` must still name NFL once, because it is a
    declaration of what the adapter covers rather than of how many requests it
    makes.
    """
    ids = {gid for gid, _sport, league in EVENT_GROUPS if league == "NFL"}
    assert ids == {88808, 24685}

    adapter = DraftKingsAdapter(leagues=["NFL"])
    try:
        assert adapter.leagues == ("NFL",)
        assert len(adapter._scopes) == 2
        assert set(adapter.capabilities()) == {"NFL"}
    finally:
        adapter.close()


def test_draftkings_state_routes_name_the_same_api_generation() -> None:
    """Every state's pinned content route must be the generation the adapter speaks.

    The per-state ``content_base_url`` in :mod:`src.jurisdictions` *overrides* the
    adapter's default, and that override is the only thing pinning a fetch to one
    state's licence.  So the two have to name the same API generation — and for a
    while they did not: the default moved to ``primaryMarkets`` while all four
    state configs still said ``leagueSubcategory``.  Because the per-state value
    wins, every ``--state`` run kept using the retired route.  Nothing failed,
    because both endpoints answered; the run just was not exercising the route
    the code claimed to use.
    """
    from src.jurisdictions import JURISDICTIONS
    from src.sources.draftkings import DEFAULT_CONTENT_BASE_URL

    generation = DEFAULT_CONTENT_BASE_URL.rsplit("/league/", 1)[-1]
    assert generation == "primaryMarkets/v1"

    seen = 0
    for state, jurisdiction in JURISDICTIONS.items():
        route = jurisdiction.routes.get("draftkings")
        if route is None:
            continue
        configured = route.config.get("content_base_url")
        assert configured, f"{state}: DraftKings route states no content_base_url"
        assert configured.rsplit("/league/", 1)[-1] == generation, (
            f"{state}: pinned route is {configured!r}, which is a different API "
            f"generation from the adapter's {generation!r}"
        )
        # And it must still be that state's own shelf, not another's.
        assert f"/sites/US-{state}-SB/" in configured, (
            f"{state}: pinned route does not carry {state}'s own site segment"
        )
        # ``base_url`` was the retired v5 route and is no longer a constructor
        # parameter; leaving it in config would raise TypeError at build time.
        assert "base_url" not in route.config, (
            f"{state}: config still passes base_url, which the adapter dropped"
        )
        seen += 1
    assert seen == 4, f"expected all four jurisdictions to route DraftKings, saw {seen}"


def _keyset_transport(total: int, *, with_sort_order: bool = True):
    """A sportscontent stub serving ``total`` NHL fixtures 100 at a time.

    Honours the ``sortOrder gt N`` predicate the venue's own subscription
    queries carry, which is the grammar the adapter's paging reuses.
    """
    import re as _re

    import httpx

    from src.sources.draftkings import EVENT_PAGE_CAP

    pool = [
        {
            "id": f"e{index}",
            "name": f"Away{index} @ Home{index}",
            "startEventDate": "2026-12-25T18:00:00.0000000Z",
            "status": "NOT_STARTED",
            "participants": [
                {"venueRole": "Away", "name": f"Away{index}"},
                {"venueRole": "Home", "name": f"Home{index}"},
            ],
            **({"sortOrder": 1000 + index} if with_sort_order else {}),
        }
        for index in range(total)
    ]
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params.get("eventsQuery", ""))
        queries.append(query)
        anchor = _re.search(r"sortOrder gt (\d+)", query)
        after = int(anchor.group(1)) if anchor else None
        events = [
            event
            for event in pool
            if after is None or event.get("sortOrder", 0) > after
        ]
        return httpx.Response(
            200,
            json={
                "events": events[:EVENT_PAGE_CAP],
                "markets": [],
                "selections": [],
            },
        )

    return httpx.MockTransport(handler), queries


def test_draftkings_pages_past_a_full_page() -> None:
    """A league deeper than one page is collected whole, not cut at 100.

    Measured on run 16: NFL's slate runs to 2026-12-26 at four other books
    while DraftKings stopped at week 7, because one ``top=100`` request was
    taken as the whole slate.  ``top=300`` is refused (HTTP 400 MRKTBFF-400),
    so the fix is the venue's own keyset grammar, not a bigger page.
    """
    import httpx

    from src.sources.draftkings import EVENT_PAGE_CAP

    total = EVENT_PAGE_CAP * 2 + 5
    transport, queries = _keyset_transport(total)
    adapter = DraftKingsAdapter(
        leagues=["NHL"], client=httpx.Client(transport=transport)
    )
    try:
        raws = adapter.fetch_raw()
    finally:
        adapter.close()

    assert len(queries) == 3
    assert "sortOrder gt" not in queries[0]
    assert "sortOrder gt 1099" in queries[1], queries[1]
    assert "sortOrder gt 1199" in queries[2], queries[2]
    # Each page keeps its own endpoint so ``latest_per_endpoint`` in the parser
    # cannot collapse them into one.
    assert [raw.endpoint for raw in raws] == [
        "sportscontent-42133",
        "sportscontent-42133-p2",
        "sportscontent-42133-p3",
    ]
    tally = adapter.last_fetch
    assert not tally.truncated_scopes, "a scope that finished is not truncated"
    assert tally.scopes_with_data == 1
    from src.sources.draftkings import _event_count

    assert sum(_event_count(raw) for raw in raws) == total


def test_draftkings_reports_a_slate_that_outruns_the_page_budget() -> None:
    import httpx

    from src.sources.draftkings import EVENT_PAGE_CAP, MAX_EVENT_PAGES

    total = EVENT_PAGE_CAP * MAX_EVENT_PAGES + 1
    transport, queries = _keyset_transport(total)
    adapter = DraftKingsAdapter(
        leagues=["NHL"], client=httpx.Client(transport=transport)
    )
    try:
        adapter.fetch_raw()
    finally:
        adapter.close()

    assert len(queries) == MAX_EVENT_PAGES
    truncated = adapter.last_fetch.truncated_scopes
    assert len(truncated) == 1, truncated
    assert "eventgroup:42133" in truncated[0]
    assert "all filled" in truncated[0], truncated[0]


def test_draftkings_stops_when_a_full_page_has_no_cursor() -> None:
    """A full page without ``sortOrder`` cannot be paged past; say so.

    Looping on the same query would hammer the venue with identical requests;
    silently stopping would read as "that is the whole slate".  The right
    answer is one page, reported truncated.
    """
    import httpx

    from src.sources.draftkings import EVENT_PAGE_CAP

    transport, queries = _keyset_transport(EVENT_PAGE_CAP, with_sort_order=False)
    adapter = DraftKingsAdapter(
        leagues=["NHL"], client=httpx.Client(transport=transport)
    )
    try:
        adapter.fetch_raw()
    finally:
        adapter.close()

    assert len(queries) == 1
    truncated = adapter.last_fetch.truncated_scopes
    assert len(truncated) == 1, truncated
    assert "no advancing sortOrder cursor" in truncated[0], truncated[0]


def test_hardrock_joins_root_idx_to_ladder() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    # Observe before the synthetic commence time.
    early = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ladder = RawResponse(
        source=ladder.source, endpoint=ladder.endpoint, url=ladder.url,
        status_code=ladder.status_code, body=ladder.body, fetched_at=early,
        content_type=ladder.content_type, request_params=ladder.request_params,
        headers=ladder.headers,
    )
    events = RawResponse(
        source=events.source, endpoint=events.endpoint, url=events.url,
        status_code=events.status_code, body=events.body, fetched_at=early,
        content_type=events.content_type, request_params=events.request_params,
        headers=events.headers,
    )
    outcome = parse_hardrock([ladder, events])
    assert not outcome.rejections, [(r.reason, r.detail) for r in outcome.rejections]
    assert len(outcome.quotes) == 6
    by_sel = {(q.market, q.selection): q for q in outcome.quotes}
    # fav rootIdx 61 → 1.64516129 (home Red Sox), dog 78 → 2.30 (away Yankees)
    assert abs(by_sel[Market.MONEYLINE, Selection.HOME].decimal_odds - 1.64516129) < 1e-6
    assert abs(by_sel[Market.MONEYLINE, Selection.AWAY].decimal_odds - 2.30) < 1e-6
    assert by_sel[Market.SPREAD, Selection.AWAY].line == 1.5
    assert by_sel[Market.SPREAD, Selection.HOME].line == -1.5
    assert by_sel[Market.TOTAL, Selection.OVER].line == 8.5


def test_hardrock_accepts_current_millisecond_event_times() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    payload = events.json()
    expected = datetime.fromisoformat(
        payload["data"]["betSync"]["events"]["data"][0]["eventTime"].replace(
            "Z", "+00:00"
        )
    )
    payload["data"]["betSync"]["events"]["data"][0]["eventTime"] = (
        expected.timestamp() * 1000
    )
    events = RawResponse(
        source=events.source,
        endpoint=events.endpoint,
        url=events.url,
        status_code=events.status_code,
        body=json.dumps(payload),
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    outcome = parse_hardrock([ladder, events])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6


def test_hardrock_accepts_current_selection_name_lines() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    payload = events.json()
    for market in payload["data"]["betSync"]["events"]["data"][0]["markets"]:
        line = market.pop("line", None)
        if line is None or market["type"].endswith(":ML"):
            continue
        for selection in market["selection"]:
            if market["type"].endswith(":OU"):
                selection["name"] = f"{selection['name']} {line}"
            elif selection["type"] == "AH":
                selection["name"] = f"{selection['name']} +{abs(line)}"
            else:
                selection["name"] = f"{selection['name']} -{abs(line)}"
    events = RawResponse(
        source=events.source,
        endpoint=events.endpoint,
        url=events.url,
        status_code=events.status_code,
        body=json.dumps(payload),
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    outcome = parse_hardrock([ladder, events])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6


def test_current_hardrock_il_capture_replays_offline_without_rejections() -> None:
    raws = list(RawStore(LIVE_REGRESSIONS).iter_responses("hardrock"))
    assert {raw.endpoint for raw in raws} == {"ladder", "tree", "events-BASEBALL"}
    tree = next(raw for raw in raws if raw.endpoint == "tree")
    assert tree.request_params == {"segment": "il"}
    outcome = parse_hardrock(raws)
    assert not outcome.rejections
    assert len(outcome.quotes) >= 100


def test_hardrock_reads_the_vs_template_home_first() -> None:
    """Hard Rock's current titles are ``"Home vs Away"``, not ``"Away @ Home"``.

    The venue changed template mid-season: the committed 2026-07-31 capture reads
    ``"New York Yankees @ Boston Red Sox"`` and this 2026-08-04 one reads
    ``"Astros vs Blue Jays"`` — the other order, with nothing in the payload
    stating orientation.  Read away-first, every Hard Rock row carried an inverted
    event key, formed its own fixture and joined nothing; the 2026-08-11 IL slate
    put it against the other 33 feeds on 25 of 25 shared MLB fixtures.
    """
    raws = list(RawStore(LIVE_REGRESSIONS).iter_responses("hardrock"))
    events = next(raw for raw in raws if raw.endpoint == "events-BASEBALL")
    titles = {
        event["name"]
        for event in events.json()["data"]["betSync"]["events"]["data"]
    }
    assert "Astros vs Blue Jays" in titles, "fixture no longer carries the vs template"

    outcome = parse_hardrock(raws)
    oriented = {
        (quote.away_participant, quote.home_participant) for quote in outcome.quotes
    }
    assert ("MLB-TOR", "MLB-HOU") in oriented
    assert ("MLB-HOU", "MLB-TOR") not in oriented


def test_hardrock_skips_an_event_name_whose_order_it_cannot_read() -> None:
    """An unverified separator is a skip, never a guessed orientation.

    Guessing costs an inverted event key, which is silent: the rows parse, price
    plausibly, and quietly stop joining every other book.  A counted skip is the
    loud version of the same gap.
    """
    raws = list(RawStore(LIVE_REGRESSIONS).iter_responses("hardrock"))
    events = next(raw for raw in raws if raw.endpoint == "events-BASEBALL")
    payload = events.json()
    for event in payload["data"]["betSync"]["events"]["data"]:
        event["name"] = event["name"].replace(" vs ", " - ")
    rewritten = RawResponse(
        source=events.source,
        endpoint=events.endpoint,
        url=events.url,
        status_code=events.status_code,
        body=json.dumps(payload),
        fetched_at=events.fetched_at,
        content_type=events.content_type,
        request_params=events.request_params,
        headers=events.headers,
    )
    others = [raw for raw in raws if raw.endpoint != "events-BASEBALL"]
    outcome = parse_hardrock([*others, rewritten])
    assert not outcome.quotes
    assert not outcome.rejections
    assert outcome.skipped["ambiguous_home_away_order"] > 0


def test_caesars_parses_bar_wrapped_names() -> None:
    """Name-splitting only.  **This does not validate the Caesars route.**

    The fixture it loads is synthetic — placeholder event UUID
    ``a1b2c3d4-…``, market ids ``m-ml``/``m-sp``/``m-tot``, selection ids
    ``s1``-``s6``, textbook 1.91/1.91 prices, against a New Jersey URL — and it
    describes a payload shape with no known live producer: the endpoint the
    adapter asks for has never been observed being requested by the real page
    (see the module docstring of ``src.sources.caesars`` and
    ``docs/evidence/state-routing.md`` § Caesars, 2026-08-15).

    So a green result here means ``_split_name`` handles bar-wrapped names, and
    nothing more.  It is not evidence that the parser matches what Caesars
    serves, and it must not be cited as such when the route is next assessed.
    """
    raw = _load("caesars__*_event_*.json")
    raw = RawResponse(
        source=raw.source, endpoint=raw.endpoint, url=raw.url,
        status_code=raw.status_code, body=raw.body,
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    outcome = parse_caesars([raw])
    assert not outcome.rejections
    assert len(outcome.quotes) == 6
    assert {q.market for q in outcome.quotes} == {
        Market.MONEYLINE, Market.SPREAD, Market.TOTAL,
    }


def test_hardrock_refuses_unknown_root_idx() -> None:
    ladder = _load("hardrock__*_ladder_*.json")
    events = _load("hardrock__*_events-BASEBALL_*.json")
    body = json.loads(events.body)
    body["data"]["betSync"]["events"]["data"][0]["markets"][0]["selection"][0]["rootIdx"] = 99999
    early = datetime(2026, 8, 1, tzinfo=timezone.utc)
    events = RawResponse(
        source=events.source, endpoint=events.endpoint, url=events.url,
        status_code=200, body=json.dumps(body), fetched_at=early,
    )
    ladder = RawResponse(
        source=ladder.source, endpoint=ladder.endpoint, url=ladder.url,
        status_code=ladder.status_code, body=ladder.body, fetched_at=early,
    )
    outcome = parse_hardrock([ladder, events])
    assert any(r.reason == "root_idx_not_on_ladder" for r in outcome.rejections)


# ── Hard Rock's slice was mistaken for the whole scope ────────────────────────


def _sliced_transport(total: int, *, honour_offset: bool = True):
    """A Hard Rock GraphQL stub that serves ``total`` events one slice at a time."""
    import httpx

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/graphql"):
            import json as _json

            body = _json.loads(request.content.decode())
            window = body["variables"]["slice"]
            seen.append(window)
            start = window["from"] if honour_offset else 0
            stop = min(start + (window["to"] - window["from"]), total)
            data = [
                {
                    "id": f"e{index}",
                    "compName": "Denmark - Superliga",
                    "eventTime": 1786000000000,
                    "sport": "SOCCER",
                    "name": "Alpha vs Beta",
                    "displayed": True,
                    "markets": [],
                }
                for index in range(start, max(start, stop))
            ]
            return httpx.Response(
                200,
                json={"data": {"betSync": {"events": {"data": data, "count": total}}}},
            )
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler), seen


def _soccer_only_adapter(transport):
    import httpx

    from src.sources import hardrock

    adapter = hardrock.HardRockAdapter(
        leagues=("SOCCER_OTHER",), client=httpx.Client(transport=transport)
    )
    adapter._scopes = tuple(
        scope for scope in adapter._scopes if scope[0] == hardrock.SOCCER_SPORT_CODE
    )
    return adapter


def test_hardrock_collects_past_its_first_slice() -> None:
    """One slice was taken as the whole scope, and the venue says otherwise.

    ``EVENTS_QUERY`` asks for ``count`` beside ``data`` and Hard Rock answers it;
    nothing read it. Measured on the 2026-08-14 Illinois run, where every scope
    took a single ``{from: 0, to: 80}`` slice:

    ======================  =========  ==========
    scope                   collected  venue says
    ======================  =========  ==========
    ``events-SOCCER``       81         **664**
    ``events-TENNIS``       81         **144**
    ``events-AMERICAN_...`` 81         **150**
    ======================  =========  ==========

    715 fixtures missing from one run, and ``scopes_truncated`` named DraftKings,
    Polymarket and SX Bet — never Hard Rock, because ``tally.produced`` was handed
    the truncated number. DraftKings makes the opposite choice and says why: "a
    silent cap reads as 'that is the whole slate' when it is not."
    """
    from src.sources.hardrock import SLICE_SIZE

    transport, windows = _sliced_transport(total=SLICE_SIZE * 2 + 5)
    adapter = _soccer_only_adapter(transport)
    try:
        adapter.fetch_raw()
    finally:
        adapter.close()

    assert [w["from"] for w in windows] == [0, SLICE_SIZE, SLICE_SIZE * 2]
    tally = adapter.last_fetch
    assert tally.scopes_with_data == 1 and tally.scopes_requested == 1
    assert not tally.truncated_scopes, "a scope that finished is not truncated"


def test_hardrock_reports_a_scope_it_could_not_finish() -> None:
    """The venue's own ``count`` is what makes the shortfall provable."""
    from src.sources.hardrock import MAX_SLICES_PER_SPORT, SLICE_SIZE

    beyond = SLICE_SIZE * (MAX_SLICES_PER_SPORT + 3)
    transport, _ = _sliced_transport(total=beyond)
    adapter = _soccer_only_adapter(transport)
    try:
        adapter.fetch_raw()
    finally:
        adapter.close()

    truncated = adapter.last_fetch.truncated_scopes
    assert len(truncated) == 1, truncated
    assert "events:SOCCER" in truncated[0]
    # The venue's own count is what makes the shortfall provable, so it is named.
    assert f"reports {beyond} event(s)" in truncated[0], truncated[0]


def test_hardrock_stops_if_the_venue_ignores_the_offset() -> None:
    """A venue that serves slice one forever must not be paged forever.

    Nothing here has probed Hard Rock's ``from``; the value is the venue's own
    parameter with a different number in it. If it turns out to be ignored, the
    loop sees the same events again and stops rather than spending twelve
    requests to collect one page repeatedly.
    """
    from src.sources.hardrock import SLICE_SIZE

    transport, windows = _sliced_transport(total=SLICE_SIZE, honour_offset=False)
    adapter = _soccer_only_adapter(transport)
    try:
        adapter.fetch_raw()
    finally:
        adapter.close()

    assert len(windows) == 1, windows
