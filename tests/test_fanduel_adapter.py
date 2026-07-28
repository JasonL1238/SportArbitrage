"""FanDuel adapter tests, driven by real captured responses.

Every fixture in ``tests/fixtures/raw`` is a verbatim envelope written by
:class:`src.raw_store.RawStore` during a live run on 2026-07-28.  Nothing here is
hand-written JSON: where a test needs a shape the live slate did not contain (an
``ALTERNATE_`` market, a reformatted runner name), it *mutates real bytes* so the
rest of the payload stays exactly as FanDuel sent it.

The negative cases are the point of the file.  A futures container must not
become a fixture, an ``ALTERNATE_`` market must not collide with the primary one,
and replaying two runs of the same endpoint must not double every row.
"""
from __future__ import annotations

import collections
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.normalize import american_to_decimal
from src.raw_store import RawResponse
from src.schema import Market, Period, Quote, Selection, Sport
from src.sources.fanduel import (
    DEFAULT_LEAGUES,
    OUT_OF_SCOPE_MARKETS,
    PAGE_BY_LEAGUE,
    SPORT_PAGE_ENDPOINTS,
    US_GAME_MARKETS,
    FanDuelAdapter,
    classify_out_of_scope,
    event_page_endpoint,
    latest_per_endpoint,
    page_for_endpoint,
    parse_fanduel,
    resolve_market_type,
    soccer_league_for,
    tennis_league_for,
)

# Endpoints whose captures are in the fixture set, one per sport.
MLB_ENDPOINT = "content-managed-page-mlb"
WNBA_ENDPOINT = "content-managed-page-wnba"
NHL_ENDPOINT = "content-managed-page-nhl"
NFL_ENDPOINT = "content-managed-page-nfl"
TENNIS_ENDPOINT = "content-managed-page-tennis"
SOCCER_ENDPOINT = "content-managed-page-soccer"
SOCCER_EVENT_ENDPOINT = "event-page-epl-35737555"


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def by_endpoint(fanduel_raw: list[RawResponse]) -> dict[str, RawResponse]:
    return {raw.endpoint: raw for raw in fanduel_raw}


@pytest.fixture(scope="session")
def parsed(fanduel_raw: list[RawResponse]):
    return parse_fanduel(fanduel_raw)


@pytest.fixture(scope="session")
def quotes(parsed) -> list[Quote]:
    return parsed.quotes


def one(by_endpoint: dict[str, RawResponse], endpoint: str) -> RawResponse:
    assert endpoint in by_endpoint, f"missing fixture for {endpoint}"
    return by_endpoint[endpoint]


def edited(raw: RawResponse, mutate) -> RawResponse:
    """Real captured bytes with one surgical change applied."""
    payload = raw.json()
    mutate(payload)
    return dataclasses.replace(raw, body=json.dumps(payload))


def markets_of(raw: RawResponse) -> dict:
    return raw.json()["attachments"]["markets"]


def events_of(raw: RawResponse) -> dict:
    return raw.json()["attachments"]["events"]


def by_market(quotes: list[Quote]) -> dict[tuple[str, str, str], list[Quote]]:
    grouped: dict[tuple[str, str, str], list[Quote]] = collections.defaultdict(list)
    for quote in quotes:
        grouped[quote.market_key].append(quote)
    return grouped


# ── configuration and the source protocol ────────────────────────────────────


def test_default_configuration_covers_every_registered_league() -> None:
    adapter = FanDuelAdapter()
    try:
        assert adapter.source_key == "fanduel"
        assert adapter.leagues == DEFAULT_LEAGUES
        assert set(adapter.leagues) == set(PAGE_BY_LEAGUE)
        # Six sports, seven pages: the four US leagues each have their own page,
        # tennis and soccer each arrive whole.
        assert adapter.page_keys == ("mlb", "wnba", "nba", "nhl", "nfl", "tennis", "soccer")
    finally:
        adapter.close()


def test_leagues_are_ordered_by_registry_not_by_caller() -> None:
    """Two instances configured with the same set request the same pages in the
    same order, so raw captures are comparable run to run."""
    forward = FanDuelAdapter(leagues=("NFL", "MLB"))
    backward = FanDuelAdapter(leagues=("MLB", "NFL"))
    try:
        assert forward.leagues == backward.leagues == ("MLB", "NFL")
        assert forward.page_keys == backward.page_keys == ("mlb", "nfl")
    finally:
        forward.close()
        backward.close()


def test_unknown_league_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="cannot collect"):
        FanDuelAdapter(leagues=("MLB", "KHL"))


def test_only_the_requested_pages_are_requested() -> None:
    adapter = FanDuelAdapter(leagues=("ATP", "WTA", "ITF"))
    try:
        assert adapter.page_keys == ("tennis",)
    finally:
        adapter.close()


# ── endpoint labels ──────────────────────────────────────────────────────────


def test_endpoint_labels_are_declared_constants_naming_the_league() -> None:
    assert SPORT_PAGE_ENDPOINTS["mlb"] == "content-managed-page-mlb"
    assert SPORT_PAGE_ENDPOINTS["soccer"] == "content-managed-page-soccer"
    for page_key, endpoint in SPORT_PAGE_ENDPOINTS.items():
        assert endpoint.endswith(page_key)
        assert page_for_endpoint(endpoint) is not None


def test_event_page_label_is_deterministic_in_league_and_event() -> None:
    assert event_page_endpoint("EPL", "35737555") == "event-page-epl-35737555"
    assert event_page_endpoint("SOCCER_OTHER", "1") == "event-page-soccer-other-1"
    assert event_page_endpoint("EPL", "35737555") == event_page_endpoint("EPL", "35737555")
    with pytest.raises(KeyError):
        event_page_endpoint("KHL", "1")


def test_event_page_label_round_trips_to_a_page_spec() -> None:
    page = page_for_endpoint("event-page-soccer-other-35864347")
    assert page is not None and page.sport is Sport.SOCCER and page.path == "event-page"
    # Only soccer has a per-event follow-up; a tennis label is not a page.
    assert page_for_endpoint("event-page-atp-1") is None
    assert page_for_endpoint("something-else") is None


def test_an_unreadable_endpoint_is_rejected_not_ignored() -> None:
    raw = RawResponse(
        source="fanduel",
        endpoint="content-managed-page-cricket",
        url="https://example.invalid",
        status_code=200,
        body=json.dumps({"attachments": {"events": {}, "markets": {}}}),
        fetched_at=datetime(2026, 7, 28, tzinfo=UTC),
    )
    outcome = parse_fanduel([raw])
    assert outcome.quotes == []
    assert [r.reason for r in outcome.rejections] == ["unknown_endpoint"]


# ── the market vocabulary is declarative and shared ──────────────────────────


def test_the_three_us_market_types_are_one_table_shared_by_four_pages() -> None:
    for page_key in ("mlb", "wnba", "nba", "nhl", "nfl"):
        page = page_for_endpoint(SPORT_PAGE_ENDPOINTS[page_key])
        assert page is not None
        assert page.markets is US_GAME_MARKETS
    assert set(US_GAME_MARKETS) == {
        "MONEY_LINE",
        "MATCH_HANDICAP_(2-WAY)",
        "TOTAL_POINTS_(OVER/UNDER)",
    }


def test_soccer_fixed_line_market_types_are_resolved_by_pattern() -> None:
    page = page_for_endpoint(SOCCER_EVENT_ENDPOINT)
    assert page is not None
    total, alt = resolve_market_type("OVER_UNDER_25", page)
    assert (total.market, total.period, total.declared_line, alt) == (
        Market.TOTAL,
        Period.FULL_GAME,
        2.5,
        False,
    )
    assert resolve_market_type("OVER_UNDER_05", page)[0].declared_line == 0.5
    spread, _ = resolve_market_type("HOME_TEAM_-1.5_GOALS", page)
    assert (spread.market, spread.declared_line) == (Market.SPREAD, 1.5)
    # The sport page must not resolve them: it never carries them, and pretending
    # otherwise would invent a line for a market that has none there.
    sport_page = page_for_endpoint(SOCCER_ENDPOINT)
    assert resolve_market_type("OVER_UNDER_25", sport_page) is None


def test_league_vocabularies_match_exactly_where_it_matters() -> None:
    assert soccer_league_for("English Premier League") == "EPL"
    assert soccer_league_for("German Bundesliga") == "BUNDESLIGA"
    # A near-miss must not inherit the parent competition's identity.
    assert soccer_league_for("German Bundesliga 2") == "SOCCER_OTHER"
    assert soccer_league_for("Bulgaria - B League") == "SOCCER_OTHER"
    # "challenger" wins over "atp"; "women" wins over "men".
    assert tennis_league_for("Bonn Challenger 2026") == "ATP_CHALLENGER"
    assert tennis_league_for("ATP Challenger Bonn - R1") == "ATP_CHALLENGER"
    assert tennis_league_for("ATP Washington D.C. 2026") == "ATP"
    assert tennis_league_for("WTA Memphis 2026") == "WTA"
    assert tennis_league_for("Women's US Open 2026") == "WTA"
    assert tennis_league_for("Men's US Open 2026") == "ATP"
    assert tennis_league_for("ITF Astana") == "ITF"


# ── every sport produces rows ────────────────────────────────────────────────


def test_every_sport_in_the_fixture_set_produces_rows(quotes: list[Quote]) -> None:
    by_sport = collections.Counter(q.sport for q in quotes)
    for sport in Sport:
        assert by_sport[sport] > 0, f"{sport.value} produced no rows"


def test_core_market_coverage_per_sport(quotes: list[Quote]) -> None:
    """Each sport's headline markets are all present.

    Tennis is moneyline-only by design; the other five carry all three.
    """
    seen: dict[Sport, set[Market]] = collections.defaultdict(set)
    for quote in quotes:
        seen[quote.sport].add(quote.market)
    triple = {Market.MONEYLINE, Market.SPREAD, Market.TOTAL}
    for sport in (Sport.BASEBALL, Sport.BASKETBALL, Sport.HOCKEY, Sport.FOOTBALL, Sport.SOCCER):
        assert seen[sport] == triple, f"{sport.value}: {sorted(m.value for m in seen[sport])}"
    assert seen[Sport.TENNIS] == {Market.MONEYLINE}


def test_only_full_game_windows_are_collected(quotes: list[Quote]) -> None:
    """FanDuel's pages expose no regulation-only or half markets in scope, so a
    row on any other period would mean a first-half market was fused in."""
    assert {q.period for q in quotes} == {Period.FULL_GAME}


# ── purity, determinism, replay ──────────────────────────────────────────────


def test_parse_is_deterministic(fanduel_raw: list[RawResponse]) -> None:
    first = parse_fanduel(fanduel_raw)
    second = parse_fanduel(fanduel_raw)
    assert [q.model_dump() for q in first.quotes] == [q.model_dump() for q in second.quotes]
    assert first.skipped == second.skipped
    assert [(r.reason, r.detail) for r in first.rejections] == [
        (r.reason, r.detail) for r in second.rejections
    ]


def test_parse_does_not_depend_on_input_order(fanduel_raw: list[RawResponse]) -> None:
    forward = parse_fanduel(fanduel_raw)
    reverse = parse_fanduel(list(reversed(fanduel_raw)))
    assert [q.model_dump() for q in forward.quotes] == [q.model_dump() for q in reverse.quotes]


def test_replay_from_the_stored_envelope_matches_the_captured_bytes(
    fanduel_raw: list[RawResponse],
) -> None:
    """A round trip through the on-disk envelope changes nothing.

    ``from_envelope`` also verifies the recorded sha256, so this doubles as proof
    the fixtures are the bytes that were captured and not an edited copy.
    """
    reloaded = [RawResponse.from_envelope(raw.to_envelope()) for raw in fanduel_raw]
    assert [r.sha256 for r in reloaded] == [r.sha256 for r in fanduel_raw]
    assert [q.model_dump() for q in parse_fanduel(reloaded).quotes] == [
        q.model_dump() for q in parse_fanduel(fanduel_raw).quotes
    ]


def test_latest_per_endpoint_keeps_one_response_per_endpoint(
    by_endpoint: dict[str, RawResponse],
) -> None:
    raw = one(by_endpoint, MLB_ENDPOINT)
    later = dataclasses.replace(raw, fetched_at=raw.fetched_at + timedelta(minutes=7))
    assert latest_per_endpoint([raw, later]) == [later]
    assert latest_per_endpoint([later, raw]) == [later]


def test_two_runs_of_the_same_endpoint_do_not_duplicate_rows(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """The regression this adapter was rewritten for.

    Replaying a directory that holds two runs used to emit every row twice, which
    collides on ``dedup_key`` and aborts the insert of the whole run.
    """
    first = one(by_endpoint, MLB_ENDPOINT)
    second = dataclasses.replace(first, fetched_at=first.fetched_at + timedelta(minutes=5))

    single = parse_fanduel([first])
    both = parse_fanduel([first, second])

    assert len(both.quotes) == len(single.quotes)
    assert len({q.dedup_key for q in both.quotes}) == len(both.quotes)
    # The surviving rows come from the *later* capture, not the first one seen.
    assert {q.observed_at for q in both.quotes} == {second.fetched_at}
    assert {q.raw_ref for q in both.quotes} == {second.ref}


def test_no_two_rows_share_a_dedup_key(quotes: list[Quote]) -> None:
    counts = collections.Counter(q.dedup_key for q in quotes)
    assert [key for key, n in counts.items() if n > 1] == []


def test_one_market_key_is_one_market(quotes: list[Quote]) -> None:
    """A ``market_key`` must not fuse two periods, two lines, or both sides of a
    team total under one identity."""
    for key, rows in by_market(quotes).items():
        assert len({(r.market, r.period) for r in rows}) == 1, key
        assert len({r.side for r in rows}) == 1, key
        assert len({None if r.line is None else abs(r.line) for r in rows}) == 1, key
        assert len({r.selection for r in rows}) == len(rows), key


# ── futures must never become fixtures ───────────────────────────────────────


def test_futures_containers_do_not_become_events(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """The NHL page carries four containers, one of which *looks* like a matchup.

    ``"Brady Tkachuk v Matthew Tkachuk"`` is a season-points special between two
    players on a hockey page.  It has two resolvable-looking names and a
    separator, and it must still not become a fixture — the US pages write games
    as ``"Away @ Home"``, and nothing else is a game.
    """
    raw = one(by_endpoint, NHL_ENDPOINT)
    names = {str(e.get("name")) for e in events_of(raw).values()}
    assert {"NHL Specials", "NHL Awards", "NHL Futures", "Brady Tkachuk v Matthew Tkachuk"} <= names

    outcome = parse_fanduel([raw])
    teams = {q.home_team for q in outcome.quotes} | {q.away_team for q in outcome.quotes}
    assert not (teams & names)
    assert all(q.home_participant.startswith("NHL-") for q in outcome.quotes)
    assert all(q.away_participant.startswith("NHL-") for q in outcome.quotes)
    assert outcome.skipped["non_game_event"] == 4


def test_no_participant_is_a_market_or_container_label(quotes: list[Quote]) -> None:
    labels = ("futures", "specials", "awards", "outright", "moneyline", "over ", "under ")
    for quote in quotes:
        for name in (quote.home_team, quote.away_team):
            lowered = name.lower()
            assert not any(lowered.startswith(label) for label in labels), name
        assert quote.home_participant != quote.away_participant


def test_markets_on_out_of_scope_events_are_counted_not_dropped(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """The bug the previous version shipped: 70 of the MLB page's 118 markets
    belonged to futures containers and were dropped with no counter at all."""
    raw = one(by_endpoint, MLB_ENDPOINT)
    markets = markets_of(raw)
    outcome = parse_fanduel([raw])

    scored = {q.source_market_id for q in outcome.quotes}
    accounted = (
        len(scored)
        + outcome.skipped["market_on_non_game_event"]
        + sum(v for k, v in outcome.skipped.items() if k.startswith("out_of_scope_market:"))
        + len(outcome.rejections)
    )
    assert accounted == len(markets)
    assert outcome.skipped["market_on_non_game_event"] == 70
    assert len(scored) == 48


def test_nothing_in_the_fixture_set_is_rejected(parsed) -> None:
    """A rejection means FanDuel offered something in scope that this parser
    could not represent.  On a healthy slate there are none, so any appearing
    here is a real format change rather than noise to be tuned away."""
    assert [(r.reason, r.detail) for r in parsed.rejections] == []


def test_every_skip_reason_is_a_declared_decision(parsed) -> None:
    declared_reasons = set(OUT_OF_SCOPE_MARKETS.values()) | {
        "period_out_of_scope",
        "different_settlement_window",
        "prop",
        "team_or_season_prop",
    }
    for key in parsed.skipped:
        if key.startswith("out_of_scope_market:"):
            assert key.split(":", 1)[1] in declared_reasons, key
        else:
            assert key in {
                "non_game_event",
                "tennis_doubles",
                "runner_without_price",
                "market_on_non_game_event",
                "market_on_doubles_event",
                "already_collected_from_sport_page:WIN-DRAW-WIN",
                # Scope is pregame.  FanDuel keeps an event listed after it starts
                # and its prices then are in-play prices, which are not the same
                # product as a pregame line even though nothing on the row says so.
                "event_already_started",
                "market_on_started_event",
                # A decimal price under 1.01 is "risk two hundred to win one" —
                # a placeholder, not a quote.  Observed at 1.005 on a suspended
                # runner.
                "price_below_plausible_minimum",
            } or key.startswith("league_not_configured:"), key


def test_out_of_scope_classification_never_shadows_a_collected_market() -> None:
    for market_type in US_GAME_MARKETS:
        page = page_for_endpoint(MLB_ENDPOINT)
        assert resolve_market_type(market_type, page) is not None
    assert classify_out_of_scope("OUTRIGHT_BETTING") == "futures"
    assert classify_out_of_scope("TO_QUALIFY_FOR_THE_NEXT_ROUND") == "different_settlement_window"
    assert classify_out_of_scope("1ST_HALF_OVER/UNDER_2.5_GOALS") == "period_out_of_scope"
    assert classify_out_of_scope("ANYTIME_GOALSCORER_INCLUDING_EXTRA_TIME") is not None
    assert classify_out_of_scope("MONEY_LINE") is None


# ── ALTERNATE_ markets ───────────────────────────────────────────────────────


def test_alternate_market_sets_the_flag_and_does_not_collide(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """``ALTERNATE_MONEY_LINE`` differs from ``MONEY_LINE`` in no other field.

    Without ``is_alternate`` the two rows share a ``dedup_key``, and because
    storage enforces that key with a UNIQUE constraint the collision aborts the
    entire run's insert.  The live slate carried no alternate market, so one real
    moneyline is cloned under the alternate type — everything else in the payload
    is exactly as FanDuel sent it.
    """
    raw = one(by_endpoint, MLB_ENDPOINT)

    def clone_as_alternate(payload: dict) -> None:
        markets = payload["attachments"]["markets"]
        market_id, market = next(
            (mid, m) for mid, m in markets.items() if m["marketType"] == "MONEY_LINE"
        )
        clone = json.loads(json.dumps(market))
        clone["marketType"] = "ALTERNATE_MONEY_LINE"
        clone["marketId"] = f"{market_id}-alt"
        markets[clone["marketId"]] = clone

    outcome = parse_fanduel([edited(raw, clone_as_alternate)])
    alternates = [q for q in outcome.quotes if q.is_alternate]
    assert len(alternates) == 2
    assert {q.market for q in alternates} == {Market.MONEYLINE}
    assert {q.period for q in alternates} == {Period.FULL_GAME}
    assert len({q.dedup_key for q in outcome.quotes}) == len(outcome.quotes)
    # The flag is the only thing keeping them apart.
    primaries = [
        q
        for q in outcome.quotes
        if not q.is_alternate
        and q.market is Market.MONEYLINE
        and q.event_key == alternates[0].event_key
    ]
    assert {q.dedup_key[:-1] for q in alternates} == {q.dedup_key[:-1] for q in primaries}


# ── orientation: two separators, opposite meanings ───────────────────────────


def test_us_pages_are_away_first(by_endpoint: dict[str, RawResponse]) -> None:
    """``"Philadelphia Phillies (A Nola) @ Miami Marlins (S Alcantara)"`` — the
    away side is named first, and the probable pitcher is not part of a name."""
    outcome = parse_fanduel([one(by_endpoint, MLB_ENDPOINT)])
    row = next(
        q for q in outcome.quotes if {q.away_participant, q.home_participant} == {"MLB-PHI", "MLB-MIA"}
    )
    assert (row.away_participant, row.home_participant) == ("MLB-PHI", "MLB-MIA")
    assert (row.away_team, row.home_team) == ("Philadelphia Phillies", "Miami Marlins")
    assert row.event_key == "MLB-PHI@MLB-MIA:2026-07-28"


@pytest.mark.parametrize(
    "fixture_name, home, away",
    [
        ("Newcastle v Liverpool", "newcastleunited", "liverpool"),
        ("Fulham v Chelsea", "fulham", "chelsea"),
        ("Freiburg v Werder Bremen", "freiburg", "werderbremen"),
    ],
)
def test_soccer_sport_page_is_home_first(
    by_endpoint: dict[str, RawResponse], fixture_name: str, home: str, away: str
) -> None:
    """FanDuel writes soccer as ``"Home v Away"`` — the opposite of its own US
    pages.  Verified against FanDuel's own ``result.type`` on the three-way
    moneyline and against Pinnacle's explicit ``alignment`` on 74 shared
    fixtures, which agreed 74/74.

    Getting this backwards is silent: every row still validates, but every event
    key is reversed and every handicap sign is flipped, so the slate joins to
    nothing.
    """
    raw = one(by_endpoint, SOCCER_ENDPOINT)
    event_id = next(
        eid for eid, e in events_of(raw).items() if str(e.get("name")) == fixture_name
    )
    outcome = parse_fanduel([raw])
    rows = [q for q in outcome.quotes if q.source_event_id == event_id]
    assert rows, fixture_name
    assert {q.home_participant for q in rows} == {f"SOCCER-{home}"}
    assert {q.away_participant for q in rows} == {f"SOCCER-{away}"}


def test_soccer_home_assignment_agrees_with_the_feeds_own_result_type(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """Checked across the whole slate, not just the pinned fixtures: the runner
    FanDuel marks ``result.type == "HOME"`` on ``WIN-DRAW-WIN`` is the one this
    adapter files as home."""
    raw = one(by_endpoint, SOCCER_ENDPOINT)
    outcome = parse_fanduel([raw])
    home_row = {
        q.source_event_id: q for q in outcome.quotes if q.selection is Selection.HOME
    }
    checked = 0
    for market in markets_of(raw).values():
        if market.get("marketType") != "WIN-DRAW-WIN":
            continue
        row = home_row.get(str(market.get("eventId")))
        if row is None:
            continue
        feed_home = next(
            r["runnerName"] for r in market["runners"] if r["result"].get("type") == "HOME"
        )
        assert row.home_team == feed_home, (row.source_event_id, row.home_team, feed_home)
        checked += 1
    assert checked > 100


# ── tennis ───────────────────────────────────────────────────────────────────


def test_tennis_ordering_is_imposed_not_taken_from_the_book(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """There is no home player, so ``src.events.orient`` orders by participant
    key and every book lands on the same answer without agreeing on anything but
    the two names."""
    outcome = parse_fanduel([one(by_endpoint, TENNIS_ENDPOINT)])
    assert outcome.quotes
    for quote in outcome.quotes:
        assert quote.sport is Sport.TENNIS
        assert quote.away_participant < quote.home_participant
        assert quote.event_key.startswith(f"{quote.away_participant}@{quote.home_participant}:")
        assert quote.market is Market.MONEYLINE
        assert quote.line is None
        # A tennis match cannot end level, so a draw here would be a misparse.
        assert quote.selection in {Selection.HOME, Selection.AWAY}
        assert quote.league in {"ATP", "WTA", "ATP_CHALLENGER", "ITF"}


def test_tennis_selection_follows_the_runner_name_not_its_position(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """FanDuel labels its first tennis runner ``HOME``.  That label is positional
    and meaningless, so the side must come from which player the runner names."""
    raw = one(by_endpoint, TENNIS_ENDPOINT)
    outcome = parse_fanduel([raw])
    rows = {(q.source_event_id, q.selection): q for q in outcome.quotes}
    checked = 0
    for market in markets_of(raw).values():
        if market.get("marketType") != "MATCH_BETTING":
            continue
        for runner in market["runners"]:
            feed_side = runner["result"].get("type")
            if feed_side != "HOME":
                continue
            row = rows.get((str(market["eventId"]), Selection.HOME))
            if row is None:
                continue
            # Where the imposed ordering disagrees with FanDuel's positional
            # label, the price must have moved with the *player*, not the label.
            if row.home_team != runner["runnerName"]:
                assert row.away_team == runner["runnerName"]
                checked += 1
    assert checked > 0, "no fixture exercised the reordering path"


def test_tennis_doubles_are_counted_out_of_scope(
    by_endpoint: dict[str, RawResponse],
) -> None:
    raw = one(by_endpoint, TENNIS_ENDPOINT)
    doubles = [
        e for e in events_of(raw).values() if "/" in str(e.get("name")) and " v " in str(e.get("name"))
    ]
    assert doubles, "fixture has no doubles to skip"
    outcome = parse_fanduel([raw])
    assert outcome.skipped["tennis_doubles"] == len(doubles)
    assert outcome.skipped["market_on_doubles_event"] >= len(doubles)
    slashed = {q for q in outcome.quotes if "/" in q.home_team or "/" in q.away_team}
    assert slashed == set()


# ── soccer ───────────────────────────────────────────────────────────────────


def test_soccer_three_way_moneyline_prices_the_draw(
    by_endpoint: dict[str, RawResponse],
) -> None:
    outcome = parse_fanduel([one(by_endpoint, SOCCER_ENDPOINT)])
    grouped = by_market(outcome.quotes)
    complete = [rows for rows in grouped.values() if len(rows) == 3]
    assert len(complete) > 100
    for rows in complete:
        assert {r.selection for r in rows} == {Selection.HOME, Selection.DRAW, Selection.AWAY}
        assert {r.market for r in rows} == {Market.MONEYLINE}
        assert {r.period for r in rows} == {Period.FULL_GAME}
        assert {r.line for r in rows} == {None}


def test_soccer_event_page_supplies_full_game_totals_and_handicaps(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """FanDuel exposes these only per event, as fixed-line market types with the
    number in the *name* and ``handicap`` left at 0."""
    outcome = parse_fanduel([one(by_endpoint, SOCCER_EVENT_ENDPOINT)])
    assert outcome.quotes
    assert {q.league for q in outcome.quotes} == {"EPL"}
    totals = [q for q in outcome.quotes if q.market is Market.TOTAL]
    spreads = [q for q in outcome.quotes if q.market is Market.SPREAD]
    assert {q.line for q in totals} == {1.5, 2.5, 3.5, 4.5}
    assert {q.line for q in spreads} == {-1.5, 1.5}
    assert all(q.period is Period.FULL_GAME for q in outcome.quotes)
    # The three-way moneyline is not taken from here: the sport page already
    # supplied it, and a second copy would collide on dedup_key.
    assert not [q for q in outcome.quotes if q.market is Market.MONEYLINE]
    assert outcome.skipped["already_collected_from_sport_page:WIN-DRAW-WIN"] == 1


def test_soccer_moneyline_and_event_page_markets_coexist_without_collision(
    by_endpoint: dict[str, RawResponse],
) -> None:
    combined = parse_fanduel(
        [one(by_endpoint, SOCCER_ENDPOINT), one(by_endpoint, SOCCER_EVENT_ENDPOINT)]
    )
    fixture_rows = [q for q in combined.quotes if q.source_event_id == "35737555"]
    assert {q.market for q in fixture_rows} == {Market.MONEYLINE, Market.SPREAD, Market.TOTAL}
    assert len({q.dedup_key for q in fixture_rows}) == len(fixture_rows)
    # One event key, one league, one orientation across both endpoints.
    assert len({(q.event_key, q.league, q.home_participant) for q in fixture_rows}) == 1


def test_a_handicap_runner_naming_a_different_line_is_rejected(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """The line is stated twice — in the market type and in the runner name — and
    they must agree.  A silent disagreement files a price at the wrong number,
    which is indistinguishable downstream from a genuine edge."""
    raw = one(by_endpoint, SOCCER_EVENT_ENDPOINT)

    def corrupt_line(payload: dict) -> None:
        markets = payload["attachments"]["markets"]
        market = next(
            m for m in markets.values() if m["marketType"] == "HOME_TEAM_-1.5_GOALS"
        )
        market["runners"][0]["runnerName"] = market["runners"][0]["runnerName"].replace(
            "1.5 Goals", "2.5 Goals"
        )

    outcome = parse_fanduel([edited(raw, corrupt_line)])
    assert [r.reason for r in outcome.rejections] == ["line_disagrees_with_market_type"]
    assert 2.5 not in {q.line for q in outcome.quotes if q.market is Market.SPREAD}


def test_a_total_runner_that_stops_naming_its_line_is_rejected(
    by_endpoint: dict[str, RawResponse],
) -> None:
    raw = one(by_endpoint, SOCCER_EVENT_ENDPOINT)

    def rename_runner(payload: dict) -> None:
        market = next(
            m for m in payload["attachments"]["markets"].values() if m["marketType"] == "OVER_UNDER_25"
        )
        for runner in market["runners"]:
            runner["runnerName"] = runner["runnerName"].split()[0]

    outcome = parse_fanduel([edited(raw, rename_runner)])
    assert {r.reason for r in outcome.rejections} == {"unparseable_total_runner"}
    assert 2.5 not in {q.line for q in outcome.quotes if q.market is Market.TOTAL}


def test_a_club_whose_name_ends_in_digits_does_not_donate_them_to_the_line(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """``"Schalke 04 -1.5 Goals"`` must parse as -1.5, not as 4 or 04."""
    raw = one(by_endpoint, SOCCER_EVENT_ENDPOINT)

    def rename_home(payload: dict) -> None:
        attachments = payload["attachments"]
        event = next(iter(attachments["events"].values()))
        home_name = event["name"].split(" v ", 1)[0]
        event["name"] = event["name"].replace(home_name, "Schalke 04", 1)
        for market in attachments["markets"].values():
            for runner in market.get("runners") or []:
                if home_name in str(runner.get("runnerName")):
                    runner["runnerName"] = runner["runnerName"].replace(home_name, "Schalke 04")

    outcome = parse_fanduel([edited(raw, rename_home)])
    spreads = [q for q in outcome.quotes if q.market is Market.SPREAD]
    assert spreads
    assert {abs(q.line) for q in spreads} == {1.5}
    # The digits stay in the club's identity and out of the line.
    assert {q.home_participant for q in spreads} == {"SOCCER-schalke04"}


# ── prices and lines ─────────────────────────────────────────────────────────


def test_spreads_mirror_and_totals_share_a_line(quotes: list[Quote]) -> None:
    two_sided = 0
    for rows in by_market(quotes).values():
        if rows[0].market is Market.SPREAD and len(rows) == 2:
            home = next(r for r in rows if r.selection is Selection.HOME)
            away = next(r for r in rows if r.selection is Selection.AWAY)
            assert home.line == pytest.approx(-away.line)
            two_sided += 1
        if rows[0].market is Market.TOTAL:
            assert len({r.line for r in rows}) == 1
    assert two_sided > 50


def test_lines_are_in_the_league_s_own_units(quotes: list[Quote]) -> None:
    from src.leagues import league as get_league

    for quote in quotes:
        if quote.market is not Market.TOTAL:
            continue
        low, high = get_league(quote.league).plausible_total_range
        assert low <= quote.line <= high, (quote.league, quote.line)


def test_a_complete_market_prices_itself_above_break_even(quotes: list[Quote]) -> None:
    """No book prices a market to lose.  Every complete market's implied
    probabilities must sum above 1, and not absurdly above it."""
    checked = 0
    for rows in by_market(quotes).values():
        expected = 3 if (rows[0].market is Market.MONEYLINE and rows[0].settlement.draw_is_priced) else 2
        if len(rows) != expected:
            continue
        total = sum(r.implied_probability for r in rows)
        assert 1.0 < total < 1.25, (rows[0].sport.value, rows[0].market.value, total)
        checked += 1
    assert checked > 400


def test_american_odds_come_from_the_feed_not_from_our_own_arithmetic(
    by_endpoint: dict[str, RawResponse],
) -> None:
    """The feed publishes both formats; taking each from the feed keeps the
    downstream consistency check a check rather than a tautology."""
    raw = one(by_endpoint, WNBA_ENDPOINT)
    outcome = parse_fanduel([raw])
    feed: dict[tuple[str, str], int] = {}
    for market_id, market in markets_of(raw).items():
        for runner in market.get("runners") or []:
            american = (runner.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {}
            if "americanOdds" in american:
                feed[(str(market_id), str(runner.get("selectionId")))] = int(
                    american["americanOdds"]
                )
    checked = 0
    for quote in outcome.quotes:
        key = (quote.source_market_id, quote.source_selection_id)
        if key in feed:
            assert quote.american_odds == feed[key]
            checked += 1
    assert checked == len(outcome.quotes)


def test_all_three_price_formats_are_mutually_consistent(quotes: list[Quote]) -> None:
    for quote in quotes:
        assert quote.implied_probability == pytest.approx(1 / quote.decimal_odds)
        assert american_to_decimal(quote.american_odds) == pytest.approx(
            quote.decimal_odds, rel=0.02
        )


def test_observed_at_and_raw_ref_come_from_the_response_the_price_came_from(
    fanduel_raw: list[RawResponse], quotes: list[Quote]
) -> None:
    refs = {raw.ref: raw.fetched_at for raw in fanduel_raw}
    for quote in quotes:
        assert quote.raw_ref in refs
        assert quote.observed_at == refs[quote.raw_ref]


# ── league filtering ─────────────────────────────────────────────────────────


def test_configured_leagues_bound_what_is_parsed(fanduel_raw: list[RawResponse]) -> None:
    outcome = parse_fanduel(fanduel_raw, leagues=("MLB", "EPL"))
    assert {q.league for q in outcome.quotes} == {"MLB", "EPL"}
    # Everything filtered out is counted, per league, rather than vanishing.
    assert outcome.skipped["league_not_configured:ITF"] > 0
    assert outcome.skipped["league_not_configured:SOCCER_OTHER"] > 0
    assert sum(v for k, v in outcome.skipped.items() if k.startswith("league_not_configured:")) > 100


def test_a_configured_offseason_league_is_a_visible_gap_not_a_silent_one() -> None:
    """The NBA is registered so that "configured but returned nothing" is
    distinguishable from "never asked for"."""
    adapter = FanDuelAdapter(leagues=("NBA",))
    try:
        assert adapter.leagues == ("NBA",)
        assert adapter.page_keys == ("nba",)
    finally:
        adapter.close()
