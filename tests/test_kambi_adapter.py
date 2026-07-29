"""Regression tests for the BetRivers/Kambi adapter over real captured payloads.

Every expected number here was read off ``tests/fixtures/raw`` — verbatim
envelopes of responses BetRivers actually sent on 2026-07-28 — so a change in
parser behaviour surfaces as a failure rather than as a silent change in what
gets collected.  The fixtures cover all six sports:

===========  =========================  ======  ====
sport        Kambi path                 events  rows
===========  =========================  ======  ====
baseball     baseball/mlb                   16   755
basketball   basketball/wnba                 1    86
hockey       ice_hockey/nhl                  7   153
football     american_football/nfl           8    48
tennis       tennis/atp                      1     2
soccer       football/england/premier_l…     3    63
             + football/brazil
===========  =========================  ======  ====

The baseball capture predates the multi-sport rewrite and still carries the old
league-less endpoint labels (``listview``, ``betoffer-batch-01``), which is
deliberate: it proves a stored capture stays replayable across a parser upgrade.
"""
from __future__ import annotations

import collections
import json
from datetime import timedelta
from typing import Iterable

import httpx
import pytest

from src.leagues import BY_KEY
from src.leagues import league as get_league
from src.raw_store import RawResponse
from src.schema import (
    MARKETS_REQUIRING_LINE,
    Market,
    Period,
    Quote,
    QuoteStatus,
    Selection,
    Sport,
)
from src.sources.base import ParseOutcome
from src.sources.betrivers_kambi import (
    MAX_BETOFFERS_PER_RESPONSE,
    CRITERIA,
    MAIN_LINE_TAG,
    OUT_OF_SCOPE,
    SOURCE_KEY,
    SPORT_PATHS,
    BetRiversKambiAdapter,
    _is_pregame_fixture,
    _resolve_endpoint,
    betoffer_endpoint,
    listview_endpoint,
    parse_kambi,
)
from src.sources.guards import BlockedError, FormatChangeError

#: Rows the fixtures parse to, per sport.  Read off the captures.
EXPECTED_ROWS = {
    Sport.BASEBALL: 755,
    Sport.BASKETBALL: 86,
    Sport.HOCKEY: 153,
    Sport.FOOTBALL: 48,
    Sport.TENNIS: 2,
    Sport.SOCCER: 63,
}

#: Kambi criterion labels for products that are *not* the market they resemble.
#: None of these may ever reach a row.
BANNED_LABELS = frozenset(
    {
        "3-Way Handicap",
        "3-Way Handicap - 1st Half",
        "Corners 3-Way Handicap",
        "Asian Total",
        "Asian Total - 1st Half",
        "Asian Handicap",
        "Asian Handicap - 1st Half",
    }
)


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def parsed(kambi_raw: list[RawResponse]) -> ParseOutcome:
    adapter = BetRiversKambiAdapter()
    try:
        return adapter.parse(kambi_raw)
    finally:
        adapter.close()


@pytest.fixture(scope="module")
def offers_by_id(kambi_raw: list[RawResponse]) -> dict[str, dict]:
    """Every captured bet offer, keyed by its Kambi id.

    Lets a test ask "which market did this row actually come from?" and check
    the answer against the raw label, instead of trusting the parser's own
    classification.
    """
    index: dict[str, dict] = {}
    for raw in kambi_raw:
        kind, _path = _resolve_endpoint(raw.endpoint)
        if kind != "betoffer":
            continue
        for offer in raw.json().get("betOffers") or []:
            index[str(offer.get("id"))] = offer
    return index


def _label_of(offer: dict) -> str:
    return str((offer.get("criterion") or {}).get("englishLabel") or "")


def _market_groups(quotes: Iterable[Quote]) -> dict[tuple, list[Quote]]:
    groups: dict[tuple, list[Quote]] = collections.defaultdict(list)
    for quote in quotes:
        groups[quote.market_key].append(quote)
    return groups


def _rewritten(raw: RawResponse, body: str, **overrides) -> RawResponse:
    fields = dict(
        source=raw.source,
        endpoint=raw.endpoint,
        url=raw.url,
        status_code=raw.status_code,
        body=body,
        fetched_at=raw.fetched_at,
        content_type=raw.content_type,
        request_params=raw.request_params,
    )
    fields.update(overrides)
    return RawResponse(**fields)


# ── coverage: every sport, every market, the right periods ───────────────────


def test_every_sport_produces_rows(parsed: ParseOutcome) -> None:
    """The adapter was MLB-only; the point of the rewrite is the other five."""
    per_sport = collections.Counter(quote.sport for quote in parsed.quotes)
    assert dict(per_sport) == EXPECTED_ROWS
    assert len(parsed.quotes) == sum(EXPECTED_ROWS.values()) == 1107
    assert len(parsed.event_keys) == 36
    assert parsed.rejections == [], [
        (r.reason, r.detail) for r in parsed.rejections[:5]
    ]


def test_each_sport_carries_its_three_core_markets(parsed: ParseOutcome) -> None:
    coverage = collections.defaultdict(set)
    for quote in parsed.quotes:
        coverage[quote.sport].add(quote.market)
    three = {Market.MONEYLINE, Market.SPREAD, Market.TOTAL}
    for sport in (
        Sport.BASEBALL,
        Sport.BASKETBALL,
        Sport.HOCKEY,
        Sport.FOOTBALL,
        Sport.SOCCER,
    ):
        assert three <= coverage[sport], sport
    # Tennis carries the match winner only; games handicaps and total games are
    # a different scoring unit and are counted out of scope.
    assert coverage[Sport.TENNIS] == {Market.MONEYLINE}


def test_periods_collected_are_only_the_ones_in_scope(parsed: ParseOutcome) -> None:
    by_sport = collections.defaultdict(set)
    for quote in parsed.quotes:
        by_sport[quote.sport].add(quote.period)
    assert by_sport[Sport.BASEBALL] == {
        Period.FULL_GAME,
        Period.FIRST_5_INNINGS,
        Period.FIRST_1_INNING,
    }
    # Hockey is the only sport where both settlement windows are collected.
    assert by_sport[Sport.HOCKEY] == {Period.FULL_GAME, Period.REGULATION}
    for sport in (Sport.BASKETBALL, Sport.FOOTBALL, Sport.TENNIS, Sport.SOCCER):
        assert by_sport[sport] == {Period.FULL_GAME}, sport


def test_leagues_are_canonical_registry_keys() -> None:
    adapter = BetRiversKambiAdapter()
    try:
        assert adapter.leagues
        for key in adapter.leagues:
            assert key in BY_KEY, key
    finally:
        adapter.close()
    for entry in SPORT_PATHS.values():
        assert entry.league in BY_KEY, entry
        assert get_league(entry.league).sport is entry.sport, entry


def test_every_row_lands_on_a_league_of_its_own_sport(parsed: ParseOutcome) -> None:
    for quote in parsed.quotes:
        assert get_league(quote.league).sport is quote.sport, quote.league


def test_soccer_competitions_map_to_registered_keys_or_the_catch_all(
    parsed: ParseOutcome,
) -> None:
    soccer = {quote.league for quote in parsed.quotes if quote.sport is Sport.SOCCER}
    # The Premier League capture is a registered competition; the Brazilian
    # second tier is not, and lands on the catch-all rather than being dropped.
    assert soccer == {"EPL", "SOCCER_OTHER"}


# ── the thousandths trap ─────────────────────────────────────────────────────


def test_lines_are_scaled_out_of_thousandths_in_every_sport(
    parsed: ParseOutcome,
) -> None:
    """Kambi sends odds *and lines* in thousandths.  Dividing only the odds
    emitted a total of 8.0 as a line of 8000.  Checked per sport, because a new
    sport is exactly where the divisor gets forgotten."""
    with_lines = [q for q in parsed.quotes if q.line is not None]
    assert with_lines

    per_sport = collections.defaultdict(list)
    for quote in with_lines:
        per_sport[quote.sport].append(quote)
    # Every sport that has line markets at all must have some.
    assert set(per_sport) == {
        Sport.BASEBALL,
        Sport.BASKETBALL,
        Sport.HOCKEY,
        Sport.FOOTBALL,
        Sport.SOCCER,
    }

    for sport, rows in per_sport.items():
        for quote in rows:
            ceiling = get_league(quote.league).plausible_total_range[1]
            assert abs(quote.line) <= ceiling, (sport, quote.line, quote.league)

    totals = collections.defaultdict(list)
    for quote in with_lines:
        if quote.market is Market.TOTAL and quote.period is Period.FULL_GAME:
            totals[quote.sport].append(quote.line)
    # Read off the captures: real full-game totals, not thousandths of them.
    assert (min(totals[Sport.BASEBALL]), max(totals[Sport.BASEBALL])) == (5.5, 12.5)
    assert (min(totals[Sport.HOCKEY]), max(totals[Sport.HOCKEY])) == (5.5, 6.5)
    assert 140.0 <= min(totals[Sport.BASKETBALL]) <= max(totals[Sport.BASKETBALL]) <= 200.0
    assert 30.0 <= min(totals[Sport.FOOTBALL]) <= max(totals[Sport.FOOTBALL]) <= 60.0
    assert 0.5 <= min(totals[Sport.SOCCER]) <= max(totals[Sport.SOCCER]) <= 8.0


def test_a_line_left_in_thousandths_is_rejected_not_emitted(
    kambi_raw: list[RawResponse],
) -> None:
    """The units guard, exercised by multiplying one real line by 1000."""
    betoffer = next(
        raw for raw in kambi_raw if raw.endpoint == "betoffer:ice_hockey/nhl:batch-01"
    )
    payload = betoffer.json()
    touched = 0
    for offer in payload["betOffers"]:
        if _label_of(offer) == "Total Goals - Regular Time":
            for outcome in offer["outcomes"]:
                outcome["line"] = outcome["line"] * 1000
            touched += 1
    assert touched, "fixture no longer carries the market this test perturbs"

    listview = next(
        raw for raw in kambi_raw if raw.endpoint == "listview:ice_hockey/nhl"
    )
    outcome = parse_kambi([listview, _rewritten(betoffer, json.dumps(payload))])
    reasons = collections.Counter(r.reason for r in outcome.rejections)
    assert reasons["line_units_error"] == 2 * touched
    assert not [
        q
        for q in outcome.quotes
        if q.market is Market.TOTAL and q.period is Period.REGULATION
    ]


# ── `closed` is a cutoff timestamp, not a boolean ────────────────────────────


def test_offer_close_time_is_not_read_as_a_suspension_flag(
    parsed: ParseOutcome, kambi_raw: list[RawResponse], offers_by_id: dict[str, dict]
) -> None:
    """904 of 1263 captured offers carry a ``closed`` cutoff.  Reading it as
    truthy marked the entire slate suspended."""
    with_cutoff = [
        q for q in parsed.quotes if offers_by_id[q.source_market_id].get("closed")
    ]
    assert len(with_cutoff) > 500
    assert {q.status for q in parsed.quotes} == {QuoteStatus.ACTIVE}


def test_a_cutoff_already_past_at_observation_is_suspended(
    kambi_raw: list[RawResponse],
) -> None:
    listview = next(
        raw for raw in kambi_raw if raw.endpoint == "listview:american_football/nfl"
    )
    betoffer = next(
        raw
        for raw in kambi_raw
        if raw.endpoint == "betoffer:american_football/nfl:batch-01"
    )
    payload = betoffer.json()
    stale = (betoffer.fetched_at - timedelta(hours=1)).isoformat().replace(
        "+00:00", "Z"
    )
    for offer in payload["betOffers"]:
        offer["closed"] = stale
    outcome = parse_kambi([listview, _rewritten(betoffer, json.dumps(payload))])
    assert outcome.quotes
    assert {q.status for q in outcome.quotes} == {QuoteStatus.SUSPENDED}


# ── the MAIN_LINE / is_alternate gap ─────────────────────────────────────────


def test_main_line_tag_decides_is_alternate(
    parsed: ParseOutcome, offers_by_id: dict[str, dict]
) -> None:
    """``is_alternate`` was never set: all 755 baseball rows came back False
    while one event carried 13 distinct total lines.  ``dedup_key`` separates a
    primary from an alternate at the same number, so the flag is identity."""
    for quote in parsed.quotes:
        tags = offers_by_id[quote.source_market_id].get("tags") or []
        expected = quote.market in MARKETS_REQUIRING_LINE and MAIN_LINE_TAG not in tags
        assert quote.is_alternate is expected, (
            quote.market,
            _label_of(offers_by_id[quote.source_market_id]),
            tags,
        )
    counts = collections.Counter(quote.is_alternate for quote in parsed.quotes)
    assert counts == {True: 674, False: 433}


def test_exactly_one_primary_line_per_market_group(parsed: ParseOutcome) -> None:
    groups: dict[tuple, list[Quote]] = collections.defaultdict(list)
    for quote in parsed.quotes:
        if quote.market in MARKETS_REQUIRING_LINE:
            groups[
                (
                    quote.event_key,
                    quote.market,
                    quote.period,
                    quote.side,
                    quote.selection,
                )
            ].append(quote)
    assert len(groups) == 298
    assert max(len({q.line for q in rows}) for rows in groups.values()) == 21
    for key, rows in groups.items():
        primaries = [row for row in rows if not row.is_alternate]
        assert len(primaries) == 1, (key, [r.line for r in primaries])


def test_a_market_never_carries_two_periods_or_two_lines(
    parsed: ParseOutcome,
) -> None:
    """``market_key`` must identify exactly one market — not several lines, not
    both teams' totals, not two settlement windows fused together."""
    for key, rows in _market_groups(parsed.quotes).items():
        assert len({r.market for r in rows}) == 1, key
        assert len({r.period for r in rows}) == 1, key
        assert len({r.side for r in rows}) == 1, key
        assert len({r.is_alternate for r in rows}) == 1, key
        assert len({r.selection for r in rows}) == len(rows), key
        # One market means one *number*, stated from each row's own side: a
        # spread group is {-1.5, +1.5}, a total group is {8.5}.
        assert len({abs(r.line) if r.line is not None else None for r in rows}) == 1, key


# ── negative tests: products that must never become a row ────────────────────


def test_pitcher_conditional_moneylines_are_not_collected(
    parsed: ParseOutcome, offers_by_id: dict[str, dict]
) -> None:
    """"Match Odds (Aaron Nola must start)" settles differently from the
    moneyline.  Collecting both puts two conflicting prices on one selection,
    which a substring match on "Match Odds" would do."""
    assert parsed.skipped["pitcher_conditional_market"] == 37
    for quote in parsed.quotes:
        label = _label_of(offers_by_id[quote.source_market_id])
        assert "must start" not in label.lower(), label


def test_three_way_handicap_is_never_mapped_to_a_spread(
    parsed: ParseOutcome, offers_by_id: dict[str, dict]
) -> None:
    """``3-Way Handicap`` prices the handicapped draw as a third outcome, so it
    is a different product from a two-way handicap and has no faithful SPREAD
    representation."""
    assert parsed.skipped["soccer_3way_handicap_prices_a_draw"] == 17
    assert (Sport.SOCCER, "3-Way Handicap") in OUT_OF_SCOPE
    assert (Sport.SOCCER, "3-Way Handicap") not in CRITERIA
    for quote in parsed.quotes:
        assert _label_of(offers_by_id[quote.source_market_id]) not in BANNED_LABELS


def test_asian_lines_are_not_merged_into_totals_or_spreads(
    parsed: ParseOutcome, offers_by_id: dict[str, dict]
) -> None:
    """``Asian Total`` and ``Asian Handicap`` quote quarter lines (-0.25, -0.75)
    that split the stake over two neighbouring lines and half-push, and integer
    lines that void.  Neither is representable on a row carrying one ``line``.
    Their half-line subset would additionally collide with ``Handicap`` on
    ``dedup_key`` — both quote -0.5 and +0.5 on the same fixture."""
    assert parsed.skipped["asian_quarter_line_splits_the_stake"] == 65
    for label in ("Asian Total", "Asian Handicap"):
        assert (Sport.SOCCER, label) in OUT_OF_SCOPE
        assert (Sport.SOCCER, label) not in CRITERIA

    soccer_spread_offers = {
        quote.source_market_id
        for quote in parsed.quotes
        if quote.sport is Sport.SOCCER and quote.market is Market.SPREAD
    }
    assert soccer_spread_offers
    assert {
        _label_of(offers_by_id[offer_id]) for offer_id in soccer_spread_offers
    } == {"Handicap"}

    soccer_total_offers = {
        quote.source_market_id
        for quote in parsed.quotes
        if quote.sport is Sport.SOCCER and quote.market is Market.TOTAL
    }
    assert {
        _label_of(offers_by_id[offer_id]) for offer_id in soccer_total_offers
    } == {"Total Goals"}


def test_soccer_spreads_are_quoted_only_at_half_goal_lines(
    parsed: ParseOutcome,
) -> None:
    """Which is *why* ``Handicap`` is the soccer spread: a half-goal line cannot
    push and cannot split, so it is the same contract shape as a run line."""
    lines = {
        quote.line
        for quote in parsed.quotes
        if quote.sport is Sport.SOCCER and quote.market is Market.SPREAD
    }
    assert lines
    for line in lines:
        assert abs(line * 2) % 2 == 1, line


def test_player_and_prop_markets_are_counted_not_collected(
    parsed: ParseOutcome, offers_by_id: dict[str, dict]
) -> None:
    assert parsed.skipped["player_prop"] == 111
    assert parsed.skipped["opta_derived_market"] == 54
    for quote in parsed.quotes:
        offer = offers_by_id[quote.source_market_id]
        assert len(offer.get("outcomes") or []) >= 2, _label_of(offer)


def test_no_dropped_record_is_silent(parsed: ParseOutcome) -> None:
    """Rule: every dropped record is a counted skip or a reject with a stable
    reason.  Stable also means *bounded* — the labels embed player and club
    names, so keying a counter on them would produce thousands of one-off
    entries instead of a coverage signal."""
    # 19, not 20: the American-odds disagreement moved to ``repaired``, because
    # those rows are *published* with a derived price rather than dropped.
    assert len(parsed.skipped) == 19
    assert sum(parsed.skipped.values()) > 700
    for reason in (*parsed.skipped, *parsed.repaired):
        assert reason == reason.strip()
        assert len(reason) < 60, reason
    # And a repair is not a drop: every one of those rows is in the output.
    assert parsed.repaired and not (set(parsed.repaired) & set(parsed.skipped))


def test_futures_containers_are_recognised_before_name_resolution() -> None:
    """The predicate that keeps the July NBA slate — 50 outright containers —
    out of the fixture list.  A container carries no away side."""
    assert _is_pregame_fixture(
        {"state": "NOT_STARTED", "homeName": "CIN Reds", "awayName": "CLE Guardians"}
    )
    assert not _is_pregame_fixture(
        {"state": "NOT_STARTED", "homeName": "NBA Championship 2026/2027"}
    )
    assert not _is_pregame_fixture(
        {"state": "STARTED", "homeName": "Max Basing", "awayName": "Sidharth Rawat"}
    )


# ── sides come from `participant`, not from ordering ─────────────────────────


def test_sides_are_resolved_by_participant_not_outcome_order(
    kambi_raw: list[RawResponse], parsed: ParseOutcome
) -> None:
    """The captured ATP match lists ``OT_TWO`` *before* ``OT_ONE``, so position
    carries no identity — and three-way markets label their outcomes
    ``"1"``/``"X"``/``"2"``, so the label carries none either."""
    betoffer = next(
        raw for raw in kambi_raw if raw.endpoint == "betoffer:tennis/atp:batch-03"
    )
    match_odds = next(
        offer
        for offer in betoffer.json()["betOffers"]
        if _label_of(offer) == "Match Odds"
    )
    order = [outcome["type"] for outcome in match_odds["outcomes"]]
    assert order == ["OT_TWO", "OT_ONE"], "fixture no longer exercises this"
    by_participant = {
        outcome["participant"]: outcome["odds"] / 1000.0
        for outcome in match_odds["outcomes"]
    }

    rows = {
        quote.selection: quote
        for quote in parsed.quotes
        if quote.sport is Sport.TENNIS
    }
    assert rows[Selection.HOME].decimal_odds == by_participant[
        rows[Selection.HOME].home_team
    ]
    assert rows[Selection.AWAY].decimal_odds == by_participant[
        rows[Selection.AWAY].away_team
    ]


def test_three_way_markets_label_outcomes_one_x_two(
    kambi_raw: list[RawResponse],
) -> None:
    betoffer = next(
        raw for raw in kambi_raw if raw.endpoint == "betoffer:ice_hockey/nhl:batch-01"
    )
    regulation = next(
        offer
        for offer in betoffer.json()["betOffers"]
        if _label_of(offer) == "Match Odds - Regular Time"
    )
    assert [o["englishLabel"] for o in regulation["outcomes"]] == ["1", "X", "2"]
    # …and the participant field is what carries the identity.
    assert [o["type"] for o in regulation["outcomes"]] == [
        "OT_ONE",
        "OT_CROSS",
        "OT_TWO",
    ]
    assert regulation["outcomes"][1].get("participant") is None
    assert regulation["outcomes"][0]["participant"]
    assert regulation["outcomes"][2]["participant"]


def test_tennis_orientation_is_this_pipelines_own_not_the_books(
    parsed: ParseOutcome,
) -> None:
    """Tennis has no home player, so orientation is by participant key.  Two
    books that disagree about which name comes first still land on one key."""
    tennis = [q for q in parsed.quotes if q.sport is Sport.TENNIS]
    assert tennis
    for quote in tennis:
        assert get_league(quote.league).has_home_away is False
        assert quote.away_participant < quote.home_participant
        assert quote.event_key.startswith(
            f"{quote.away_participant}@{quote.home_participant}:"
        )


# ── two-sided market shape ───────────────────────────────────────────────────


def test_spread_lines_mirror_and_totals_share_one_line(parsed: ParseOutcome) -> None:
    checked = 0
    for key, rows in _market_groups(parsed.quotes).items():
        if len(rows) != 2:
            continue
        if rows[0].market is Market.SPREAD:
            sides = {row.selection: row.line for row in rows}
            assert sides[Selection.HOME] == pytest.approx(-sides[Selection.AWAY]), key
            checked += 1
        elif rows[0].market in (Market.TOTAL, Market.TEAM_TOTAL):
            assert len({row.line for row in rows}) == 1, key
            assert {row.selection for row in rows} == {
                Selection.OVER,
                Selection.UNDER,
            }, key
            checked += 1
    assert checked > 400


def test_complete_markets_are_priced_in_the_books_favour(
    parsed: ParseOutcome,
) -> None:
    """A complete market's implied probabilities always sum above 1.0.  A sum
    below it means the parser paired prices from different markets or lines."""
    sums = []
    for key, rows in _market_groups(parsed.quotes).items():
        if len(rows) < 2:
            continue
        total = sum(row.implied_probability for row in rows)
        assert 1.0 < total < 1.6, (key, total, [r.decimal_odds for r in rows])
        sums.append(total)
    assert len(sums) > 500
    assert min(sums) > 1.03


def test_hockey_regulation_is_three_way_and_full_game_is_two_way(
    parsed: ParseOutcome,
) -> None:
    """The rule that must not be broken: a 60-minute hockey moneyline is
    three-way because a tie is a real settlement outcome, and the same market
    including the shootout is two-way because it cannot tie.  Pairing one
    against the other looks like a large edge on two fair prices."""
    by_period: dict[Period, list[list[Quote]]] = collections.defaultdict(list)
    for rows in _market_groups(parsed.quotes).values():
        if rows[0].sport is Sport.HOCKEY and rows[0].market is Market.MONEYLINE:
            by_period[rows[0].period].append(rows)

    assert len(by_period[Period.REGULATION]) == 7
    assert len(by_period[Period.FULL_GAME]) == 7
    for rows in by_period[Period.REGULATION]:
        assert {r.selection for r in rows} == {
            Selection.HOME,
            Selection.DRAW,
            Selection.AWAY,
        }
    for rows in by_period[Period.FULL_GAME]:
        assert {r.selection for r in rows} == {Selection.HOME, Selection.AWAY}

    # Same fixtures, different contracts — and never fused into one market.
    regulation = {rows[0].event_key for rows in by_period[Period.REGULATION]}
    full_game = {rows[0].event_key for rows in by_period[Period.FULL_GAME]}
    assert regulation == full_game
    for rows in by_period[Period.REGULATION] + by_period[Period.FULL_GAME]:
        assert len({r.market_key for r in rows}) == 1


def test_a_draw_is_only_emitted_where_the_book_prices_one(
    parsed: ParseOutcome,
) -> None:
    draws = [q for q in parsed.quotes if q.selection is Selection.DRAW]
    assert draws
    assert {(q.sport, q.period) for q in draws} == {
        (Sport.HOCKEY, Period.REGULATION),
        (Sport.SOCCER, Period.FULL_GAME),
        (Sport.BASEBALL, Period.FIRST_1_INNING),
    }


# ── purity, provenance, replay ───────────────────────────────────────────────


def test_parse_is_pure_and_deterministic(kambi_raw: list[RawResponse]) -> None:
    adapter = BetRiversKambiAdapter()
    try:
        first = adapter.parse(kambi_raw)
        second = adapter.parse(kambi_raw)
    finally:
        adapter.close()
    assert [q.model_dump() for q in first.quotes] == [
        q.model_dump() for q in second.quotes
    ]
    assert first.skipped == second.skipped
    assert [(r.reason, r.detail) for r in first.rejections] == [
        (r.reason, r.detail) for r in second.rejections
    ]


def test_row_provenance_points_at_the_response_the_price_came_from(
    parsed: ParseOutcome, kambi_raw: list[RawResponse]
) -> None:
    by_ref = {raw.ref: raw for raw in kambi_raw}
    for quote in parsed.quotes:
        raw = by_ref[quote.raw_ref]
        assert quote.observed_at == raw.fetched_at
        assert quote.source == SOURCE_KEY
        _kind, path = _resolve_endpoint(raw.endpoint)
        assert SPORT_PATHS[path].sport is quote.sport


def test_no_two_rows_share_a_dedup_key(parsed: ParseOutcome) -> None:
    """Storage enforces this with a UNIQUE constraint, so one collision aborts
    the insert of the whole run."""
    keys = collections.Counter(quote.dedup_key for quote in parsed.quotes)
    assert [key for key, n in keys.items() if n > 1] == []
    assert len(keys) == len(parsed.quotes)


def test_replaying_two_runs_yields_one_row_per_selection(
    kambi_raw: list[RawResponse], parsed: ParseOutcome
) -> None:
    """Replay is handed a *directory*, which can hold several runs.  Iterating
    every response duplicated every row — and every duplicate collided on
    ``dedup_key``, which aborted the whole insert."""
    later = [
        _rewritten(raw, raw.body, fetched_at=raw.fetched_at + timedelta(hours=1))
        for raw in kambi_raw
    ]
    outcome = parse_kambi([*kambi_raw, *later])
    assert len(outcome.quotes) == len(parsed.quotes)
    assert {q.dedup_key for q in outcome.quotes} == {
        q.dedup_key for q in parsed.quotes
    }
    # The newer capture is the one that survived.
    assert {q.observed_at for q in outcome.quotes} == {
        raw.fetched_at for raw in later if _resolve_endpoint(raw.endpoint)[0] == "betoffer"
    }


def test_a_shifted_batch_index_is_not_treated_as_a_new_response(
    kambi_raw: list[RawResponse], parsed: ParseOutcome
) -> None:
    """The batch index is a position counter, not an identity: it moves when the
    slate size changes, so ``batch-01`` yesterday and ``batch-01`` today are
    different event sets.  Deduplicating on the label alone therefore is not
    enough, and deriving identity from the index would be worse."""
    later: list[RawResponse] = []
    for raw in kambi_raw:
        kind, path = _resolve_endpoint(raw.endpoint)
        endpoint = (
            betoffer_endpoint(path, 41)
            if kind == "betoffer"
            else listview_endpoint(path)
        )
        later.append(
            _rewritten(
                raw,
                raw.body,
                endpoint=endpoint,
                fetched_at=raw.fetched_at + timedelta(hours=1),
            )
        )
    outcome = parse_kambi([*kambi_raw, *later])
    assert len(outcome.quotes) == len(parsed.quotes)
    assert {q.dedup_key for q in outcome.quotes} == {
        q.dedup_key for q in parsed.quotes
    }
    assert outcome.skipped["betoffer_superseded_by_newer_response"] == 0


def test_stale_baseball_capture_still_replays() -> None:
    """The MLB fixtures were written before the multi-sport rewrite and carry
    league-less endpoint labels.  A parser upgrade must not orphan stored raw
    data."""
    assert _resolve_endpoint("listview") == ("listview", "baseball/mlb")
    assert _resolve_endpoint("betoffer-batch-07") == ("betoffer", "baseball/mlb")


def test_endpoint_labels_name_the_league() -> None:
    for path in SPORT_PATHS:
        assert _resolve_endpoint(listview_endpoint(path)) == ("listview", path)
        assert _resolve_endpoint(betoffer_endpoint(path, 3)) == ("betoffer", path)
    with pytest.raises(FormatChangeError):
        _resolve_endpoint("some-unlabelled-capture")


def test_parse_refuses_betoffers_without_a_slate(
    kambi_raw: list[RawResponse],
) -> None:
    betoffers = [
        raw
        for raw in kambi_raw
        if _resolve_endpoint(raw.endpoint)[0] == "betoffer"
    ]
    with pytest.raises(FormatChangeError):
        parse_kambi(betoffers)


def test_a_truncated_betoffer_response_is_reported_not_silently_partial(
    kambi_raw: list[RawResponse],
) -> None:
    """A capped response must never look like a complete one.

    Checked by comparing the ids asked for against the ids that came back.  The
    guard used to read ``payload["range"]["total"]`` — and ``range`` appears in
    **none** of the captured betoffer responses, whose top-level keys are exactly
    ``betOffers``, ``events`` and ``prePacks``.  It returned ``None``
    unconditionally, so both the fetch-time batch-halving retry and this
    rejection were dead code written against a contract the endpoint does not
    have.  The ``events`` array is real, and in every capture it echoes the
    request exactly.
    """
    listview = next(
        raw for raw in kambi_raw if raw.endpoint == "listview:ice_hockey/nhl"
    )
    betoffer = next(
        raw for raw in kambi_raw if raw.endpoint == "betoffer:ice_hockey/nhl:batch-01"
    )
    asked = str((betoffer.request_params or {}).get("event_ids", "")).split(",")
    assert len(asked) >= 2, "this fixture needs a multi-event batch"

    def without_last_event(offers: int):
        payload = betoffer.json()
        payload["events"] = [
            event for event in payload["events"] if str(event.get("id")) != asked[-1]
        ]
        # Pad the flat offer list to the cap, which is the other half of the
        # signal: a response can only drop events off the end once it is full.
        # Padded with offers the parser has no criterion for, so the count
        # reaches the cap without inventing rows.
        filler = dict(payload["betOffers"][0])
        filler["criterion"] = {"id": -1, "label": "x", "englishLabel": "Not A Market"}
        payload["betOffers"] = payload["betOffers"] + [
            dict(filler, id=-index) for index in range(
                max(offers - len(payload["betOffers"]), 0)
            )
        ]
        return _rewritten(betoffer, json.dumps(payload))

    outcome = parse_kambi([listview, without_last_event(MAX_BETOFFERS_PER_RESPONSE)])
    truncation = [r for r in outcome.rejections if r.reason == "betoffer_response_truncated"]
    assert truncation and asked[-1] in truncation[0].detail

    # A missing event on a response nowhere near the cap is ordinary — an event
    # that closed or started between the listView call and this one — and is
    # counted rather than failing the whole tenant.  Grading it a rejection put
    # 3,455 good rows behind ok=False on a real capture.
    ordinary = parse_kambi([listview, without_last_event(0)])
    assert not ordinary.rejections
    assert ordinary.skipped["event_absent_from_betoffer_response"] == 1

    # The untouched response says nothing at all.
    assert not parse_kambi([listview, betoffer]).rejections


# ── prices ───────────────────────────────────────────────────────────────────


def test_all_three_odds_formats_agree(parsed: ParseOutcome) -> None:
    for quote in parsed.quotes:
        assert quote.implied_probability == pytest.approx(1.0 / quote.decimal_odds)
        payout = quote.decimal_odds - 1.0
        if quote.american_odds > 0:
            from_american = quote.american_odds / 100.0
        else:
            from_american = 100.0 / abs(quote.american_odds)
        assert abs(from_american - payout) / payout <= 0.01, quote


def test_the_feeds_own_american_price_is_preferred_where_it_agrees(
    parsed: ParseOutcome, offers_by_id: dict[str, dict]
) -> None:
    """Kambi publishes both formats.  Deriving one from the other would make the
    cross-format check a tautology — but BetRivers rounds its American value to
    "nice" numbers (1.520 decimal is quoted -195, arithmetically -192), so the
    feed's value is taken only where the two agree, and the discard is counted."""
    verbatim = 0
    for quote in parsed.quotes:
        offer = offers_by_id[quote.source_market_id]
        stated = {
            str(o.get("id")): o.get("oddsAmerican") for o in offer.get("outcomes") or []
        }.get(quote.source_selection_id)
        if stated is not None and int(str(stated).replace("+", "")) == quote.american_odds:
            verbatim += 1
    assert verbatim / len(parsed.quotes) > 0.9
    # ``repaired``, not ``skipped``: these 54 rows *are* published, carrying a
    # derived American price.  Counting them as skips reported them as
    # discarded on a dashboard panel headed "seen but out of scope".
    assert parsed.repaired["feed_american_odds_disagreed_with_decimal"] == 54
    assert "feed_american_odds_disagreed_with_decimal" not in parsed.skipped


# ── fetch guards ─────────────────────────────────────────────────────────────


def test_fetch_raw_raises_on_a_bot_challenge_rather_than_returning_nothing() -> None:
    """Returning ``[]`` would make a Cloudflare interstitial look like an off
    day."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text="<html><body>Attention Required! | Cloudflare</body></html>",
            headers={"content-type": "text/html"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BetRiversKambiAdapter(leagues=["MLB"], client=client)
    try:
        with pytest.raises(BlockedError):
            adapter.fetch_raw()
    finally:
        adapter.close()


def test_unknown_league_is_a_configuration_error() -> None:
    with pytest.raises(KeyError):
        BetRiversKambiAdapter(leagues=["curling/wcf"])


def test_league_keys_and_paths_are_both_accepted() -> None:
    adapter = BetRiversKambiAdapter(leagues=["MLB", "ice_hockey/nhl"])
    try:
        assert adapter.paths == ("baseball/mlb", "ice_hockey/nhl")
        assert adapter.leagues == ("MLB", "NHL")
    finally:
        adapter.close()
