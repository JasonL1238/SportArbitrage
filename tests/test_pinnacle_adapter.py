"""Pinnacle adapter regressions, against real captured bytes.

``tests/fixtures/raw/pinnacle__*.json`` are verbatim envelopes captured from
``guest.api.arcadia.pinnacle.com`` on 2026-07-28, one league pair per sport plus
two deliberate trap payloads:

* ``league:NHL:1456`` — Pinnacle's NHL that day was **futures only**: three
  ``type: "moneyline"`` markets whose prices carry ``participantId`` and no
  ``designation``.  Nothing here may become a row.
* ``league:ATP:4356`` — ten real matches and ten ``units: "Games"`` **child**
  matchups naming the same players at the same start time.  Accepting a child
  invents a second match and files a games handicap as a match moneyline.

Everything asserted below was first observed live; the fixtures exist so it stays
asserted without a network.
"""
from __future__ import annotations

import collections
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

import httpx
import pytest

from src import leagues as league_registry
from src.raw_store import RawResponse, RawStore
from src.schema import (
    SELECTIONS_BY_MARKET,
    Market,
    Period,
    Quote,
    QuoteStatus,
    Selection,
    Sport,
)
from src.sources.guards import BlockedError, EmptyResponseError, FormatChangeError
from src.sources.pinnacle import (
    TENNIS_CATCH_ALL,
    DEFAULT_LEAGUES,
    LEAGUE_ROUTES,
    MARKETS_BY_SPORT,
    PERIODS_BY_SPORT,
    SOCCER_CATCH_ALL,
    PinnacleAdapter,
    _fallback_market_id,
    canonical_league_key,
    provenance,
    league_scope,
    parse_pinnacle,
    sport_scope,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw"

#: Scopes present in the captured fixture set.
MLB_SCOPE = league_scope("MLB", 246)
WNBA_SCOPE = league_scope("WNBA", 578)
NHL_SCOPE = league_scope("NHL", 1456)
HOCKEY_FRIENDLY_SCOPE = league_scope("HOCKEY_OTHER", 1602)
NFL_SCOPE = league_scope("NFL", 889)
ATP_SCOPE = league_scope("ATP", 4356)
EPL_SCOPE = league_scope("EPL", 1980)
CUP_SCOPE = league_scope(SOCCER_CATCH_ALL, 2636)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def captures() -> list[RawResponse]:
    store = RawStore(FIXTURE_DIR)
    paths = sorted(FIXTURE_DIR.glob("pinnacle__*.json"))
    assert paths, f"no pinnacle fixtures in {FIXTURE_DIR}"
    return [store.read(path) for path in paths]


def _scope(captures: Sequence[RawResponse], token: str) -> list[RawResponse]:
    raws = [raw for raw in captures if raw.endpoint.endswith(token)]
    assert len(raws) == 2, f"expected a matchups/markets pair for {token}, got {len(raws)}"
    return raws


@pytest.fixture(scope="module")
def parsed(captures: list[RawResponse]):
    outcome = parse_pinnacle(captures)
    assert outcome.quotes, "the captured slate produced no rows at all"
    return outcome


@pytest.fixture(scope="module")
def rows(parsed) -> list[Quote]:
    return parsed.quotes


def _payload(raws: Sequence[RawResponse], kind: str, token: str) -> list[dict[str, Any]]:
    raw = next(r for r in raws if r.endpoint == f"{kind}:{token}")
    return raw.json()


def _by_market(rows: Sequence[Quote]) -> dict[tuple[str, str, str], list[Quote]]:
    grouped: dict[tuple[str, str, str], list[Quote]] = collections.defaultdict(list)
    for row in rows:
        grouped[row.market_key].append(row)
    return grouped


# ── synthetic payload helpers ────────────────────────────────────────────────


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw(endpoint: str, payload: Any, *, at: datetime | None = None) -> RawResponse:
    return RawResponse(
        source="pinnacle",
        endpoint=endpoint,
        url=f"https://guest.api.arcadia.pinnacle.com/0.1/{endpoint}",
        status_code=200,
        body=json.dumps(payload),
        fetched_at=at or datetime(2026, 7, 28, 8, 0, tzinfo=UTC),
        content_type="application/json",
    )


def _upcoming(hours: int = 24) -> str:
    """A kick-off *hours* from whenever the suite runs.

    This defaulted to the literal ``"2026-07-28T23:00:00Z"`` until the wall
    clock reached it, at which point every synthetic matchup was skipped as
    ``event_already_started`` and the fallback test failed for good.  The guard
    compares against ``raw.fetched_at``, and for a mock transport that is the
    real clock — so a fixed future date in a synthetic payload is a timer, not a
    fixture.  Tests that want a started game pass an explicit past ``start``.
    """
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _matchup(
    matchup_id: int,
    *,
    home: str,
    away: str,
    sport_id: int,
    league_id: int,
    league_name: str,
    start: str | None = None,
    parent_id: int | None = None,
    units: str = "Regular",
    kind: str = "matchup",
) -> dict[str, Any]:
    return {
        "id": matchup_id,
        "type": kind,
        "units": units,
        "parentId": parent_id,
        "isLive": False,
        "status": "pending",
        "startTime": start or _upcoming(),
        "league": {"id": league_id, "name": league_name, "sport": {"id": sport_id}},
        "participants": [
            {"alignment": "home", "name": home, "order": 0},
            {"alignment": "away", "name": away, "order": 1},
        ],
    }


def _market(matchup_id: int, **overrides: Any) -> dict[str, Any]:
    market = {
        "matchupId": matchup_id,
        "type": "moneyline",
        "period": 0,
        "key": "s;0;m",
        "isAlternate": False,
        "status": "open",
        "limits": [{"type": "maxRiskStake", "amount": 500}],
        "prices": [
            {"designation": "home", "price": -110},
            {"designation": "away", "price": -110},
        ],
    }
    market.update(overrides)
    return market


def _pair(matchups: list[dict[str, Any]], markets: list[dict[str, Any]], token: str = MLB_SCOPE):
    return [_raw(f"matchups:{token}", matchups), _raw(f"markets:{token}", markets)]


# ── coverage ─────────────────────────────────────────────────────────────────


class TestCapturedCoverage:
    def test_every_captured_scope_is_paired(self, captures: list[RawResponse]) -> None:
        tokens = collections.Counter(raw.endpoint.partition(":")[2] for raw in captures)
        assert set(tokens) == {
            MLB_SCOPE,
            WNBA_SCOPE,
            NHL_SCOPE,
            HOCKEY_FRIENDLY_SCOPE,
            NFL_SCOPE,
            ATP_SCOPE,
            EPL_SCOPE,
            CUP_SCOPE,
        }
        assert set(tokens.values()) == {2}, tokens

    def test_endpoint_labels_name_the_league(self, captures: list[RawResponse]) -> None:
        """The label lands in the stored filename and in every row's raw_ref, so
        two leagues' captures must not be indistinguishable on disk."""
        for raw in captures:
            kind, _, token = raw.endpoint.partition(":")
            assert kind in ("matchups", "markets"), raw.endpoint
            assert token.startswith(("league:", "sport:")), raw.endpoint
            assert token.split(":")[-1].isdigit(), raw.endpoint

    def test_all_six_sports_produce_rows(self, rows) -> None:
        counts = collections.Counter(row.sport for row in rows)
        for sport in Sport:
            assert counts[sport] > 0, f"{sport.value} produced no rows"

    def test_core_markets_are_covered_per_sport(self, rows) -> None:
        """A book that stops returning one of a sport's core markets has almost
        certainly renamed something upstream; the symptom is a row count, so it
        has to be asserted rather than eyeballed."""
        from src.vocab import CORE_MARKETS_BY_SPORT

        seen: dict[Sport, set[Market]] = collections.defaultdict(set)
        for row in rows:
            seen[row.sport].add(row.market)
        for sport, markets in seen.items():
            assert CORE_MARKETS_BY_SPORT[sport] <= markets, sport

    def test_every_row_carries_a_registered_league_of_the_right_sport(self, rows) -> None:
        for row in rows:
            competition = league_registry.league(row.league)
            assert competition.sport is row.sport, (row.league, row.sport)

    def test_no_rejection_is_a_parser_fault(self, parsed) -> None:
        """Rejections mean the book offered something in scope that could not be
        represented, so every one of them has to be accounted for.

        On this capture there is at most one, and it is upstream:
        ``src.participants`` treats ``" and "`` as a doubles-pairing separator, so
        the genuine club "Brighton and Hove Albion" does not resolve.  It stays a
        rejection rather than being worked around here — a name that cannot be
        mapped must never become a passthrough — and is reported to that module's
        owner.
        """
        assert {r.reason for r in parsed.rejections} <= {"unknown_participant"}, [
            (r.reason, r.detail) for r in parsed.rejections[:5]
        ]
        assert len(parsed.rejections) <= 2, [r.detail for r in parsed.rejections]


# ── purity, determinism and replay ───────────────────────────────────────────


class TestParseIsPure:
    def test_parsing_the_same_bytes_twice_is_identical(
        self, captures: list[RawResponse]
    ) -> None:
        first = parse_pinnacle(captures)
        second = parse_pinnacle(captures)
        assert [q.model_dump() for q in first.quotes] == [
            q.model_dump() for q in second.quotes
        ]
        assert first.skipped == second.skipped
        assert [(r.reason, r.detail) for r in first.rejections] == [
            (r.reason, r.detail) for r in second.rejections
        ]

    def test_the_adapter_and_the_function_agree(self, captures, rows) -> None:
        adapter = PinnacleAdapter(request_pause=0.0)
        try:
            assert [q.model_dump() for q in adapter.parse(captures).quotes] == [
                q.model_dump() for q in rows
            ]
        finally:
            adapter.close()

    def test_parse_does_not_depend_on_which_leagues_were_configured(
        self, captures, rows
    ) -> None:
        """Replay must reproduce what the capture produced whatever instance is
        replaying it, so narrowing happens in fetch_raw and never in parse."""
        adapter = PinnacleAdapter(["MLB"], request_pause=0.0)
        try:
            assert len(adapter.parse(captures).quotes) == len(rows)
        finally:
            adapter.close()

    def test_replay_from_disk_reproduces_the_stored_rows(self, rows) -> None:
        """Round-trip through the envelope format, byte for byte."""
        store = RawStore(FIXTURE_DIR)
        reloaded = [
            RawResponse.from_envelope(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(FIXTURE_DIR.glob("pinnacle__*.json"))
        ]
        assert [q.model_dump() for q in parse_pinnacle(reloaded).quotes] == [
            q.model_dump() for q in rows
        ]
        # And the envelopes really are the files on disk.
        assert {raw.sha256 for raw in reloaded} == {
            store.read(path).sha256 for path in sorted(FIXTURE_DIR.glob("pinnacle__*.json"))
        }

    def test_row_counts_are_stable_per_sport(self, rows) -> None:
        """A golden count over fixed bytes: any drift is either a real parser
        change or an accidental one, and both should be looked at.

        Soccer is a lower bound rather than an equality because one EPL fixture
        currently loses its participants to an upstream name-resolution bug (see
        :meth:`TestCapturedCoverage.test_no_rejection_is_a_parser_fault`); fixing
        that adds rows back rather than changing any parsing here.
        """
        counts = collections.Counter(row.sport.value for row in rows)
        assert counts["hockey"] == 34  # 2 friendlies × (period 0 + period 6)
        assert counts["tennis"] == 20  # 10 matches × 2 selections
        assert counts["basketball"] == 170
        assert counts["baseball"] == 803
        assert counts["football"] == 800
        assert counts["soccer"] >= 472
        assert set(counts) == {s.value for s in Sport}


# ── the parentId guard ───────────────────────────────────────────────────────


class TestChildMatchupGuard:
    def test_every_games_child_in_the_tennis_capture_is_skipped(
        self, captures, parsed
    ) -> None:
        matchups = _payload(_scope(captures, ATP_SCOPE), "matchups", ATP_SCOPE)
        children = [m for m in matchups if m.get("parentId") is not None]
        by_units = collections.Counter(m.get("units") for m in children)
        assert by_units, "the tennis capture is supposed to contain child matchups"
        for units, count in by_units.items():
            assert parsed.skipped[f"child_matchup:units:{units}"] >= count

    def test_no_child_participant_ever_became_an_event(self, captures, rows) -> None:
        """A child names the players with a scoring-unit marker — "Greet Minnen
        (Games)" — and ``canonical_participant`` strips parentheticals for
        tennis, so a leaked child resolves to the *same* two players and looks
        like a genuine second match."""
        matchups = _payload(_scope(captures, ATP_SCOPE), "matchups", ATP_SCOPE)
        child_ids = {str(m["id"]) for m in matchups if m.get("parentId") is not None}
        assert child_ids
        assert not (child_ids & {row.source_event_id for row in rows})
        child_names = {
            p["name"]
            for m in matchups
            if m.get("parentId") is not None
            for p in m["participants"]
        }
        assert any("(Games)" in name for name in child_names)
        emitted = {row.home_team for row in rows} | {row.away_team for row in rows}
        assert not (child_names & emitted)
        assert not any("(" in row.home_team or "(" in row.away_team for row in rows)

    def test_tennis_events_equal_the_number_of_root_matchups(
        self, captures, rows
    ) -> None:
        matchups = _payload(_scope(captures, ATP_SCOPE), "matchups", ATP_SCOPE)
        roots = [
            m
            for m in matchups
            if m.get("parentId") is None and m.get("type") == "matchup"
        ]
        tennis = [row for row in rows if row.sport is Sport.TENNIS]
        assert len({row.event_key for row in tennis}) == len(roots)

    def test_a_child_never_fabricates_a_doubleheader(self, rows) -> None:
        """``#2`` on a tennis key would mean two matches between the same two
        players on one day, which does not happen."""
        assert not [
            row.event_key
            for row in rows
            if row.sport is Sport.TENNIS and "#" in row.event_key
        ]

    def test_a_child_matchup_is_skipped_even_when_it_looks_perfect(self) -> None:
        parent = _matchup(
            1,
            home="Ugo Humbert",
            away="Alex Barrena",
            sport_id=33,
            league_id=4356,
            league_name="ATP Washington - R1",
            units="Sets",
        )
        child = _matchup(
            2,
            home="Ugo Humbert (Games)",
            away="Alex Barrena (Games)",
            sport_id=33,
            league_id=4356,
            league_name="ATP Washington - R1",
            parent_id=1,
            units="Games",
        )
        outcome = parse_pinnacle(
            _pair([parent, child], [_market(1), _market(2)], token=ATP_SCOPE)
        )
        assert {row.source_event_id for row in outcome.quotes} == {"1"}
        assert outcome.skipped["child_matchup:units:Games"] == 1
        assert len({row.event_key for row in outcome.quotes}) == 1

    def test_a_special_matchup_is_skipped_with_its_type(self) -> None:
        prop = _matchup(
            9,
            home="Over 1.5",
            away="Under 1.5",
            sport_id=3,
            league_id=246,
            league_name="MLB",
            kind="special",
        )
        outcome = parse_pinnacle(_pair([prop], [_market(9)]))
        assert outcome.quotes == []
        assert outcome.skipped["matchup_type:special"] == 1


# ── the participantId / futures guard ────────────────────────────────────────


class TestParticipantPricedGuard:
    def test_the_nhl_capture_is_futures_only_and_yields_nothing(
        self, captures
    ) -> None:
        raws = _scope(captures, NHL_SCOPE)
        markets = _payload(raws, "markets", NHL_SCOPE)
        futures = [
            m
            for m in markets
            if m.get("prices")
            and all(
                "participantId" in p and "designation" not in p for p in m["prices"]
            )
        ]
        assert len(futures) == 3, "the captured NHL slate is three outrights"
        assert {m["type"] for m in futures} == {"moneyline"}

        outcome = parse_pinnacle(raws)
        assert outcome.quotes == []
        assert outcome.rejections == []
        assert outcome.skipped["participant_priced_market:on_out_of_scope_matchup"] == 3

    def test_a_participant_priced_market_on_a_real_game_never_becomes_a_row(
        self,
    ) -> None:
        """The dangerous case the matchup guard cannot catch.  With no
        designation to map, every price would land on the same selection."""
        game = _matchup(
            1,
            home="Miami Marlins",
            away="Philadelphia Phillies",
            sport_id=3,
            league_id=246,
            league_name="MLB",
        )
        outright = _market(
            1,
            key="s;0;m;outright",
            prices=[
                {"participantId": 11, "price": 353},
                {"participantId": 12, "price": 2773},
            ],
        )
        outcome = parse_pinnacle(_pair([game], [_market(1), outright]))
        assert len(outcome.quotes) == 2  # the genuine moneyline only
        assert outcome.skipped["participant_priced_market:on_game_matchup"] == 1
        assert all(row.source_market_id == "s;0;m" for row in outcome.quotes)

    def test_the_guard_does_not_fire_on_a_normal_market(self, rows) -> None:
        assert all(row.source_selection_id in ("home", "away", "draw", "over", "under") for row in rows)


# ── periods ──────────────────────────────────────────────────────────────────


class TestPeriodsAreKeyedBySport:
    def test_period_six_is_regulation_only_for_hockey(self) -> None:
        """The same number means the fourth quarter in basketball.  A global
        table would file a 60-minute hockey price as a full-game price that
        includes the shootout — a different contract, and one that looks like
        free money when the two are paired."""
        assert PERIODS_BY_SPORT[Sport.HOCKEY][6] is Period.REGULATION
        assert 6 not in PERIODS_BY_SPORT[Sport.BASKETBALL]
        assert 6 not in PERIODS_BY_SPORT[Sport.SOCCER]

    def test_period_one_means_something_different_in_every_sport(self) -> None:
        assert PERIODS_BY_SPORT[Sport.BASEBALL][1] is Period.FIRST_5_INNINGS
        assert 1 not in PERIODS_BY_SPORT[Sport.HOCKEY]  # one 20-minute period
        assert 1 not in PERIODS_BY_SPORT[Sport.SOCCER]  # first half, out of scope
        assert 1 not in PERIODS_BY_SPORT[Sport.TENNIS]  # first set

    def test_zero_is_the_full_contest_everywhere(self) -> None:
        for sport, periods in PERIODS_BY_SPORT.items():
            assert periods[0] is Period.FULL_GAME, sport

    def test_soccer_period_eight_is_never_full_game(self, captures, parsed) -> None:
        """Period 8 appears only on cup ties and UEFA qualifiers, is two-way with
        no draw, and settles on extra time / qualification.  Mapping it to
        FULL_GAME would pair a 120-minute contract against a 90-minute one."""
        markets = _payload(_scope(captures, CUP_SCOPE), "markets", CUP_SCOPE)
        eights = [m for m in markets if m.get("period") == 8]
        assert eights, "the cup capture is supposed to carry a period-8 market"
        assert parsed.skipped["period_out_of_scope:soccer:8"] >= len(eights)

    def test_baseball_keeps_its_partial_game_windows(self, rows) -> None:
        periods = {row.period for row in rows if row.sport is Sport.BASEBALL}
        assert Period.FIRST_5_INNINGS in periods
        assert Period.FIRST_1_INNING in periods

    def test_no_row_lands_on_a_period_the_sport_cannot_settle(self, rows) -> None:
        for row in rows:
            assert row.period in PERIODS_BY_SPORT[row.sport].values(), row


class TestHockeysTwoScoringWindows:
    def test_hockey_rows_come_from_friendlies_and_not_from_the_nhl(
        self, rows
    ) -> None:
        """Pinnacle's NHL is outrights only in the offseason, so its hockey
        contribution is club friendlies — an open-roster competition, which is why
        these clubs must not be resolved against the NHL's closed roster."""
        hockey = [row for row in rows if row.sport is Sport.HOCKEY]
        assert hockey
        assert {row.league for row in hockey} == {"HOCKEY_OTHER"}
        assert all(row.home_participant.startswith("HOCKEY-") for row in hockey)

    def test_full_game_and_regulation_are_different_contracts(
        self, captures
    ) -> None:
        outcome = parse_pinnacle(_scope(captures, HOCKEY_FRIENDLY_SCOPE))
        assert outcome.quotes, "the hockey friendlies capture should price two windows"
        assert outcome.rejections == []

        full = [q for q in outcome.quotes if q.period is Period.FULL_GAME]
        regulation = [q for q in outcome.quotes if q.period is Period.REGULATION]
        assert full and regulation

        # Period 0 includes the shootout, so it cannot be drawn.
        assert {q.selection for q in full} == {Selection.HOME, Selection.AWAY}
        # Period 6 is 60 minutes and is priced three ways.
        assert Selection.DRAW in {q.selection for q in regulation}
        assert all(q.market is Market.MONEYLINE for q in outcome.quotes if q.selection is Selection.DRAW)

        # The two windows never share a market.
        by_period: dict[Period, set[tuple[str, str, str]]] = collections.defaultdict(set)
        for quote in outcome.quotes:
            by_period[quote.period].add(quote.market_key)
        assert not (by_period[Period.FULL_GAME] & by_period[Period.REGULATION])

    def test_the_first_period_is_out_of_scope(self, captures) -> None:
        outcome = parse_pinnacle(_scope(captures, HOCKEY_FRIENDLY_SCOPE))
        assert outcome.skipped["period_out_of_scope:hockey:1"] > 0

    def test_a_draw_on_a_window_that_cannot_be_drawn_is_rejected(self) -> None:
        game = _matchup(
            1,
            home="Boston Bruins",
            away="Toronto Maple Leafs",
            sport_id=19,
            league_id=1456,
            league_name="NHL",
        )
        outcome = parse_pinnacle(
            _pair(
                [game],
                [
                    _market(
                        1,
                        prices=[
                            {"designation": "home", "price": -150},
                            {"designation": "away", "price": 130},
                            {"designation": "draw", "price": 320},
                        ],
                    )
                ],
                token=NHL_SCOPE,
            )
        )
        assert len(outcome.quotes) == 2
        assert [r.reason for r in outcome.rejections] == ["draw_not_priced"]


# ── market scope ─────────────────────────────────────────────────────────────


class TestMarketScope:
    def test_tennis_collects_the_match_winner_and_nothing_else(
        self, captures, parsed, rows
    ) -> None:
        """A tennis root matchup has ``units: "Sets"``, so its ``spread`` is a
        set handicap (±1.5) and its ``total`` is total sets (2.5) — neither is
        the market the other books quote."""
        assert MARKETS_BY_SPORT[Sport.TENNIS] == frozenset({Market.MONEYLINE})
        tennis = [row for row in rows if row.sport is Sport.TENNIS]
        assert tennis
        assert {row.market for row in tennis} == {Market.MONEYLINE}
        assert parsed.skipped["market_out_of_scope:tennis:spread"] > 0
        assert parsed.skipped["market_out_of_scope:tennis:total"] > 0

        raws = _scope(captures, ATP_SCOPE)
        matchups = _payload(raws, "matchups", ATP_SCOPE)
        markets = _payload(raws, "markets", ATP_SCOPE)
        roots = {m["id"] for m in matchups if m.get("parentId") is None}
        children = {m["id"] for m in matchups if m.get("parentId") is not None}

        def totals(ids: set[int]) -> set[float]:
            return {
                p["points"]
                for m in markets
                if m["matchupId"] in ids and m.get("type") == "total" and m.get("period") == 0
                for p in m["prices"]
            }

        # The root's total is total *sets*; the child's is total *games*.  Both
        # arrive as ``type: "total", period: 0`` — the only thing separating a
        # 2.5 from a 21.5 is which matchup the market hangs off.
        assert totals(roots) == {2.5}
        assert min(totals(children)) > 10.0

    def test_tennis_rows_carry_no_line(self, rows) -> None:
        assert all(
            row.line is None for row in rows if row.sport is Sport.TENNIS
        )

    def test_soccer_moneylines_are_three_way(self, rows) -> None:
        soccer_ml = [
            row
            for row in rows
            if row.sport is Sport.SOCCER and row.market is Market.MONEYLINE
        ]
        assert soccer_ml
        assert Selection.DRAW in {row.selection for row in soccer_ml}
        for _, group in _by_market(soccer_ml).items():
            assert {q.selection for q in group} == {
                Selection.HOME,
                Selection.AWAY,
                Selection.DRAW,
            }


# ── market identity ──────────────────────────────────────────────────────────


class TestMarketIdentity:
    def test_a_market_key_never_spans_two_markets(self, rows) -> None:
        for market_key, group in _by_market(rows).items():
            shapes = {(q.market, q.period, q.side, q.is_alternate) for q in group}
            assert len(shapes) == 1, (market_key, shapes)
            magnitudes = {abs(q.line) if q.line is not None else None for q in group}
            assert len(magnitudes) == 1, (market_key, magnitudes)
            assert len({q.event_key for q in group}) == 1, market_key

    def test_no_two_rows_share_a_dedup_key(self, rows) -> None:
        counts = collections.Counter(row.dedup_key for row in rows)
        assert [key for key, n in counts.items() if n > 1] == []

    def test_a_market_captured_twice_is_priced_once(self, captures) -> None:
        """Two captures of the same league in one parse must not collide on
        dedup_key — storage enforces it and a collision aborts the whole run."""
        doubled = list(captures) + [
            _raw(raw.endpoint, raw.json(), at=raw.fetched_at + timedelta(seconds=1))
            for raw in _scope(captures, EPL_SCOPE)
        ]
        outcome = parse_pinnacle(doubled)
        counts = collections.Counter(row.dedup_key for row in outcome.quotes)
        assert [key for key, n in counts.items() if n > 1] == []
        assert outcome.skipped["superseded_response:matchups"] >= 1

    def test_the_fallback_market_id_separates_side_line_and_alternate(self) -> None:
        """The old fallback was ``matchup:type:period``, which fused both teams'
        team totals into one market and every alternate line of a spread into one
        more — so a "complete market" was seven lines deep."""
        base = {
            "matchupId": 1,
            "type": "team_total",
            "period": 0,
            "prices": [
                {"designation": "over", "points": 4.5, "price": -110},
                {"designation": "under", "points": 4.5, "price": -110},
            ],
        }
        home = dict(base, side="home")
        away = dict(base, side="away")
        alternate = dict(home, isAlternate=True)
        other_line = dict(
            home,
            prices=[
                {"designation": "over", "points": 5.5, "price": -110},
                {"designation": "under", "points": 5.5, "price": -110},
            ],
        )
        ids = {
            _fallback_market_id("1", market)
            for market in (home, away, alternate, other_line)
        }
        assert len(ids) == 4, ids

    def test_a_market_without_a_key_still_produces_one_market_per_line(self) -> None:
        game = _matchup(
            1,
            home="Miami Marlins",
            away="Philadelphia Phillies",
            sport_id=3,
            league_id=246,
            league_name="MLB",
        )
        markets = [
            {
                "matchupId": 1,
                "type": "total",
                "period": 0,
                "isAlternate": alternate,
                "prices": [
                    {"designation": "over", "points": points, "price": -105},
                    {"designation": "under", "points": points, "price": -115},
                ],
            }
            for points, alternate in ((8.5, False), (9.0, True))
        ]
        outcome = parse_pinnacle(_pair([game], markets))
        grouped = _by_market(outcome.quotes)
        assert len(grouped) == 2
        for group in grouped.values():
            assert {q.selection for q in group} == {Selection.OVER, Selection.UNDER}
            assert len({q.line for q in group}) == 1


# ── lines and prices ─────────────────────────────────────────────────────────


class TestLinesAndPrices:
    def test_spreads_mirror(self, rows) -> None:
        checked = 0
        for market_key, group in _by_market(rows).items():
            if group[0].market is not Market.SPREAD:
                continue
            home = [q for q in group if q.selection is Selection.HOME]
            away = [q for q in group if q.selection is Selection.AWAY]
            if not (home and away):
                continue
            assert home[0].line == pytest.approx(-away[0].line), market_key
            checked += 1
        assert checked > 100

    def test_over_and_under_share_a_line(self, rows) -> None:
        checked = 0
        for market_key, group in _by_market(rows).items():
            if group[0].market not in (Market.TOTAL, Market.TEAM_TOTAL):
                continue
            assert len({q.line for q in group}) == 1, market_key
            checked += 1
        assert checked > 100

    def test_a_complete_market_never_prices_the_book_to_lose(self, rows) -> None:
        checked = 0
        for market_key, group in _by_market(rows).items():
            if any(q.status is not QuoteStatus.ACTIVE for q in group):
                continue
            market = group[0].market
            present = {q.selection for q in group}
            required = SELECTIONS_BY_MARKET[market]
            if market is Market.MONEYLINE:
                if not {Selection.HOME, Selection.AWAY} <= present:
                    continue
            elif not required <= present:
                continue
            overround = sum(q.implied_probability for q in group)
            assert overround >= 1.0, f"{market_key} sums to {overround:.4f}"
            checked += 1
        assert checked > 500

    def test_prices_are_american_and_points_are_plain(self, rows) -> None:
        """Kambi sends odds and lines in thousandths; Pinnacle does not, and
        applying that scaling here would turn -110 into a nonsense price and an
        8.5 total into 8500."""
        for row in rows:
            assert not (-100 < row.american_odds < 100), row.american_odds
            assert 1.0 < row.decimal_odds <= 1000.0
        for sport, bounds in (
            (Sport.BASEBALL, (0.0, 25.0)),
            (Sport.SOCCER, (0.0, 15.0)),
            (Sport.BASKETBALL, (0.0, 300.0)),
        ):
            lines = [abs(q.line) for q in rows if q.sport is sport and q.line is not None]
            assert lines
            assert max(lines) <= bounds[1], (sport, max(lines))

    def test_the_feeds_own_american_price_is_kept_verbatim(self, captures, rows) -> None:
        markets = _payload(_scope(captures, EPL_SCOPE), "markets", EPL_SCOPE)
        feed = {
            (str(m["matchupId"]), m["key"], p["designation"]): p["price"]
            for m in markets
            if m.get("key") and m.get("prices")
            for p in m["prices"]
            if "designation" in p
        }
        checked = 0
        for row in rows:
            key = (row.source_event_id, row.source_market_id, row.source_selection_id)
            if key in feed:
                assert row.american_odds == feed[key], key
                checked += 1
        assert checked > 100

    def test_the_three_price_formats_agree(self, rows) -> None:
        from src.normalize import american_to_decimal, implied_probability

        for row in rows:
            assert row.decimal_odds == pytest.approx(
                american_to_decimal(row.american_odds), rel=1e-9
            )
            assert row.implied_probability == pytest.approx(
                implied_probability(row.decimal_odds), rel=1e-9
            )

    def test_a_pick_em_handicap_normalizes_negative_zero(self) -> None:
        game = _matchup(
            1,
            home="Miami Marlins",
            away="Philadelphia Phillies",
            sport_id=3,
            league_id=246,
            league_name="MLB",
        )
        market = {
            "matchupId": 1,
            "type": "spread",
            "period": 0,
            "key": "s;0;s;0.0",
            "prices": [
                {"designation": "home", "points": 0.0, "price": -115},
                {"designation": "away", "points": -0.0, "price": -115},
            ],
        }
        outcome = parse_pinnacle(_pair([game], [market]))
        assert {row.line for row in outcome.quotes} == {0.0}
        assert all(f"{row.line:g}" == "0" for row in outcome.quotes)


# ── event identity ───────────────────────────────────────────────────────────


class TestEventIdentity:
    def test_tennis_ordering_is_imposed_not_trusted(self, rows) -> None:
        """There is no home player; each book orders the two names however it
        likes, so the ordering comes from ``src.events.orient``."""
        tennis = [row for row in rows if row.sport is Sport.TENNIS]
        assert tennis
        for row in tennis:
            assert row.away_participant < row.home_participant

    def test_tennis_orientation_ignores_pinnacles_alignment(self) -> None:
        pinnacle_order = _matchup(
            1,
            home="Zeynep Sonmez",
            away="Cadence Brace",
            sport_id=33,
            league_id=5004,
            league_name="WTA Washington - R1",
            units="Sets",
        )
        flipped = _matchup(
            1,
            home="Cadence Brace",
            away="Zeynep Sonmez",
            sport_id=33,
            league_id=5004,
            league_name="WTA Washington - R1",
            units="Sets",
        )
        keys = {
            parse_pinnacle(_pair([m], [_market(1)], token=ATP_SCOPE)).quotes[0].event_key
            for m in (pinnacle_order, flipped)
        }
        assert len(keys) == 1, keys

    def test_team_sports_keep_the_books_home_side(self, rows) -> None:
        mlb = [row for row in rows if row.sport is Sport.BASEBALL]
        assert mlb
        assert all(row.home_participant.startswith("MLB-") for row in mlb)
        assert all(row.away_participant != row.home_participant for row in mlb)

    def test_tennis_tournaments_map_onto_tour_keys(self) -> None:
        cases = {
            "ATP Challenger Bonn - R1": "ATP_CHALLENGER",
            "ATP Los Cabos - R1": "ATP",
            "ATP Los Cabos - Doubles": "ATP",
            "WTA 125K Vancouver - R1": "WTA",
            "WTA Washington - R16": "WTA",
            "ITF Men Pitesti - R1": "ITF",
            "ITF Women Aldershot - R1": "ITF",
        }
        for name, expected in cases.items():
            assert canonical_league_key(Sport.TENNIS, 999_999, name) == expected
        # This asserted ``is None`` — that a tour the table does not name is
        # *discarded*.  It is now the catch-all, as soccer already was and as
        # Matchbook, SX Bet and Smarkets all read it: ``src.leagues`` says
        # ``TENNIS_OTHER`` exists precisely so an adapter need not "either guess
        # a tour or discard the match", and this adapter was discarding.
        for unnamed in (
            "Davis Cup", "United Cup", "Laver Cup", "Billie Jean King Cup",
            "Mens UTR Pro Series, Argentina",
        ):
            assert canonical_league_key(Sport.TENNIS, 999_999, unnamed) == TENNIS_CATCH_ALL

    def test_soccer_falls_back_to_the_catch_all(self) -> None:
        assert canonical_league_key(Sport.SOCCER, 1980, "England - Premier League") == "EPL"
        assert canonical_league_key(Sport.SOCCER, 2636, "UEFA - Super Cup") == SOCCER_CATCH_ALL
        assert (
            canonical_league_key(Sport.SOCCER, 210_712, "Bhutan - Premier League")
            == SOCCER_CATCH_ALL
        )

    def test_soccer_is_never_classified_by_a_name_substring(self) -> None:
        """Pinnacle also lists "Iceland - Premier League", "Brazil - Serie A" and
        "Austria - Bundesliga"; a substring rule files all three under a European
        top flight."""
        for pinnacle_id, name in (
            (2102, "Iceland - Premier League"),
            (1834, "Brazil - Serie A"),
            (1792, "Austria - Bundesliga"),
        ):
            assert canonical_league_key(Sport.SOCCER, pinnacle_id, name) == SOCCER_CATCH_ALL

    def test_an_unknown_sport_is_counted_not_guessed(self) -> None:
        cricket = _matchup(
            1,
            home="India",
            away="Australia",
            sport_id=8,
            league_id=1,
            league_name="Cricket - Test",
        )
        outcome = parse_pinnacle(_pair([cricket], [_market(1)]))
        assert outcome.quotes == []
        assert outcome.skipped["sport_unmapped:8"] == 1

    def test_an_unresolvable_participant_is_rejected(self) -> None:
        futures = _matchup(
            1,
            home="NFL Futures",
            away="AFC Championship Winner",
            sport_id=15,
            league_id=889,
            league_name="NFL",
        )
        outcome = parse_pinnacle(_pair([futures], [_market(1)], token=NFL_SCOPE))
        assert outcome.quotes == []
        assert [r.reason for r in outcome.rejections] == ["unknown_participant"]


# ── provenance ───────────────────────────────────────────────────────────────


class TestProvenance:
    def test_observed_at_is_the_price_responses_fetch_time(
        self, captures, rows
    ) -> None:
        markets_times = {
            raw.fetched_at for raw in captures if raw.endpoint.startswith("markets:")
        }
        for row in rows:
            assert row.observed_at in markets_times, row.observed_at

    def test_observed_at_is_never_a_source_clock(self, rows) -> None:
        for row in rows:
            assert row.observed_at < row.commence_time

    def test_both_refs_point_at_responses_that_were_stored(self, rows, captures) -> None:
        """Provenance must be resolvable by plain membership: the price ref names
        the response `observed_at` came from, and the identity ref names the
        matchups payload that supplied the participants and the start time."""
        stored = {raw.ref for raw in captures}
        for row in rows:
            assert row.raw_ref in stored, row.raw_ref
            assert row.identity_raw_ref in stored, row.identity_raw_ref


    def test_the_price_ref_is_recoverable_as_the_first_half(
        self, captures, rows
    ) -> None:
        by_ref = {raw.ref: raw for raw in captures}
        for row in rows:
            price_ref = row.raw_ref.split("+", 1)[0]
            assert by_ref[price_ref].fetched_at == row.observed_at

    def test_the_two_refs_describe_the_same_scope(self, rows) -> None:
        """The price and the identity must come from the same league's pair, not
        from one league's prices joined onto another league's matchups."""
        for row in rows:
            assert row.identity_raw_ref is not None
            price_scope = row.raw_ref.split("/")[2].removeprefix("markets-")
            identity_scope = row.identity_raw_ref.split("/")[2].removeprefix("matchups-")
            assert price_scope == identity_scope

    def test_provenance_returns_price_then_identity(self) -> None:
        markets = _raw("markets:" + MLB_SCOPE, [])
        matchups = _raw("matchups:" + MLB_SCOPE, [])
        assert provenance(markets, matchups) == (markets.ref, matchups.ref)

    def test_raw_ref_is_single_valued(self, rows) -> None:
        """It names one response, so a check comparing it against the set of
        stored refs works by plain membership.  A compound "a+b" string matched
        nothing and made every row look orphaned."""
        for row in rows:
            assert "+" not in row.raw_ref
            assert "+" not in (row.identity_raw_ref or "")

    def test_limits_are_positive_when_stated(self, rows) -> None:
        for row in rows:
            if row.limit_amount is not None:
                assert row.limit_amount > 0


# ── pairing and failure modes ────────────────────────────────────────────────


class TestResponsePairing:
    def test_a_half_pair_is_rejected_rather_than_silently_empty(
        self, captures
    ) -> None:
        matchups = next(
            raw for raw in _scope(captures, EPL_SCOPE) if raw.endpoint.startswith("matchups:")
        )
        other = _scope(captures, MLB_SCOPE)
        outcome = parse_pinnacle([*other, matchups])
        assert outcome.quotes
        assert [r.reason for r in outcome.rejections] == ["unpaired_response"]

    def test_no_pair_at_all_is_a_format_change(self) -> None:
        with pytest.raises(FormatChangeError):
            parse_pinnacle([_raw("something-else", [])])

    def test_an_unrecognised_response_is_counted(self, captures) -> None:
        # Stamped with the captures' own time: ``parse_pinnacle`` narrows to one
        # collection pass first, so a response from another pass is dropped
        # before anything looks at it — which is the point of that narrowing.
        scoped = _scope(captures, MLB_SCOPE)
        outcome = parse_pinnacle(
            [*scoped, _raw("garbage", [], at=scoped[0].fetched_at)]
        )
        assert outcome.skipped["unrecognised_response:garbage"] == 1

    def test_a_league_index_response_is_counted_not_parsed(self, captures) -> None:
        scoped = _scope(captures, MLB_SCOPE)
        index = _raw(
            f"leagues:{sport_scope(Sport.SOCCER, 29)}",
            [{"id": 1980}],
            at=scoped[0].fetched_at,
        )
        outcome = parse_pinnacle([*scoped, index])
        assert outcome.skipped["league_index_response"] == 1

    def test_responses_from_another_pass_are_dropped_before_anything_reads_them(
        self, captures
    ) -> None:
        """Pinnacle was the one paging source that did not narrow to a single
        collection pass.

        A directory holding yesterday's bytes beside today's contributes a stale
        pair under a token absent from the newest pass, which
        ``_pair_responses`` has no reason to supersede — and it then dragged the
        started-game anchor backwards for *every* scope, re-admitting rows on
        fixtures that had already kicked off and that the stale bytes had nothing
        to do with.
        """
        scoped = _scope(captures, MLB_SCOPE)
        started = min(raw.fetched_at for raw in scoped) + timedelta(hours=6)

        # Yesterday's pair, under a token today's pass does not carry — so
        # ``_pair_responses`` has no reason to supersede it — and describing a
        # fixture that has since started.  Without the narrowing, its timestamp
        # becomes the batch minimum and every started game is admitted as
        # pregame; that is the failure, and it is invisible in the row count
        # alone.
        matchups = next(raw for raw in scoped if raw.endpoint.startswith("matchups"))
        markets = next(raw for raw in scoped if raw.endpoint.startswith("markets"))
        stale_matchups = [
            dict(m, id=900000 + index, startTime=_iso(started))
            for index, m in enumerate(matchups.json())
            if isinstance(m, dict) and m.get("type") == "matchup"
        ]
        assert stale_matchups, "the capture must hold at least one matchup"
        stale_markets = [
            dict(row, matchupId=900000 + index)
            for index, row in enumerate(markets.json())
            if isinstance(row, dict)
        ]
        older = min(raw.fetched_at for raw in scoped) - timedelta(days=1)
        yesterday = [
            _raw("matchups:league:OLD:999", stale_matchups, at=older),
            _raw("markets:league:OLD:999", stale_markets, at=older),
        ]

        with_stale = parse_pinnacle([*scoped, *yesterday])
        assert with_stale.quotes == parse_pinnacle(scoped).quotes
        # Nothing from a fixture that had already begun by capture time.
        assert not [
            q for q in with_stale.quotes
            if q.commence_time <= min(raw.fetched_at for raw in scoped)
        ]


# ── configuration and fetch planning ────────────────────────────────────────


def _transport(recorded: list[str]) -> httpx.MockTransport:
    matchup = _matchup(
        1,
        home="Miami Marlins",
        away="Philadelphia Phillies",
        sport_id=3,
        league_id=246,
        league_name="MLB",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request.url.path)
        if request.url.path.endswith("/markets/straight"):
            return httpx.Response(200, json=[_market(1)])
        return httpx.Response(200, json=[matchup])

    return httpx.MockTransport(handler)


class TestConfiguration:
    def test_the_leagues_property_reports_what_was_configured(self) -> None:
        adapter = PinnacleAdapter(["MLB", "WNBA", "MLB"], request_pause=0.0)
        try:
            assert adapter.leagues == ("MLB", "WNBA")
            assert adapter.sports == (Sport.BASEBALL, Sport.BASKETBALL)
        finally:
            adapter.close()

    def test_the_default_configuration_is_every_registered_league(self) -> None:
        adapter = PinnacleAdapter(request_pause=0.0)
        try:
            assert adapter.leagues == DEFAULT_LEAGUES
            assert len(adapter.sports) == len(Sport)
        finally:
            adapter.close()
        for route in LEAGUE_ROUTES:
            if league_registry.is_known(route.league_key):
                assert route.league_key in DEFAULT_LEAGUES, route

    def test_an_unknown_league_fails_at_construction(self) -> None:
        with pytest.raises(KeyError):
            PinnacleAdapter(["NOT_A_LEAGUE"])

    def test_no_leagues_is_refused(self) -> None:
        with pytest.raises(ValueError):
            PinnacleAdapter([])

    def test_a_narrow_configuration_only_requests_its_own_leagues(self) -> None:
        seen: list[str] = []
        adapter = PinnacleAdapter(
            ["MLB"], client=httpx.Client(transport=_transport(seen)), request_pause=0.0
        )
        try:
            adapter.fetch_raw()
        finally:
            adapter.close()
        assert seen == ["/0.1/leagues/246/matchups", "/0.1/leagues/246/markets/straight"]

    def test_named_soccer_leagues_avoid_the_whole_sport_download(self) -> None:
        """The soccer sport endpoint is 19 MB; two named competitions are two
        cheap league pairs."""
        seen: list[str] = []
        adapter = PinnacleAdapter(
            ["EPL", "MLS"], client=httpx.Client(transport=_transport(seen)), request_pause=0.0
        )
        try:
            adapter.fetch_raw()
        finally:
            adapter.close()
        assert seen == [
            "/0.1/leagues/1980/matchups",
            "/0.1/leagues/1980/markets/straight",
            "/0.1/leagues/2663/matchups",
            "/0.1/leagues/2663/markets/straight",
        ]

    def test_the_soccer_catch_all_uses_one_request_pair_for_the_sport(self) -> None:
        seen: list[str] = []
        adapter = PinnacleAdapter(
            [SOCCER_CATCH_ALL],
            client=httpx.Client(transport=_transport(seen)),
            request_pause=0.0,
        )
        try:
            adapter.fetch_raw()
        finally:
            adapter.close()
        assert seen == ["/0.1/sports/29/matchups", "/0.1/sports/29/markets/straight"]

    def test_tennis_always_uses_the_sport_endpoint(self) -> None:
        """37 tennis leagues had fixtures on one day and their ids are minted per
        tournament, so they cannot be enumerated ahead of time."""
        seen: list[str] = []
        adapter = PinnacleAdapter(
            ["ATP"], client=httpx.Client(transport=_transport(seen)), request_pause=0.0
        )
        try:
            adapter.fetch_raw()
        finally:
            adapter.close()
        assert seen == ["/0.1/sports/33/matchups", "/0.1/sports/33/markets/straight"]

    def test_hockey_asks_for_exactly_the_competitions_configured(self) -> None:
        """Pinnacle's NHL is outrights only in the offseason, so a hockey
        configuration that omits ``HOCKEY_OTHER`` collects no hockey prices at
        all — which the collector reports as a gap against ``leagues``."""
        for configured, expected in (
            (["NHL"], ["/0.1/leagues/1456/matchups"]),
            (["HOCKEY_OTHER"], ["/0.1/leagues/1602/matchups"]),
            (
                ["NHL", "HOCKEY_OTHER"],
                ["/0.1/leagues/1456/matchups", "/0.1/leagues/1602/matchups"],
            ),
        ):
            seen: list[str] = []
            adapter = PinnacleAdapter(
                configured,
                client=httpx.Client(transport=_transport(seen)),
                request_pause=0.0,
            )
            try:
                adapter.fetch_raw()
            finally:
                adapter.close()
            assert [p for p in seen if p.endswith("/matchups")] == expected, configured

    def test_scope_tokens_are_stable(self) -> None:
        assert league_scope("MLB", 246) == "league:MLB:246"
        assert sport_scope(Sport.TENNIS, 33) == "sport:tennis:33"


class TestFetchFailures:
    def test_a_blocked_response_raises_rather_than_returning_nothing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                text="<html><body>Attention Required! Cloudflare</body></html>",
                headers={"content-type": "text/html"},
            )

        adapter = PinnacleAdapter(
            ["MLB"], client=httpx.Client(transport=httpx.MockTransport(handler)), request_pause=0.0
        )
        try:
            with pytest.raises(BlockedError):
                adapter.fetch_raw()
        finally:
            adapter.close()

    def test_an_empty_slate_everywhere_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        adapter = PinnacleAdapter(
            ["MLB"], client=httpx.Client(transport=httpx.MockTransport(handler)), request_pause=0.0
        )
        try:
            with pytest.raises(EmptyResponseError):
                adapter.fetch_raw()
        finally:
            adapter.close()

    def test_one_idle_league_does_not_abort_the_others(self) -> None:
        matchup = _matchup(
            1,
            home="Miami Marlins",
            away="Philadelphia Phillies",
            sport_id=3,
            league_id=246,
            league_name="MLB",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "/leagues/487/" in request.url.path:  # NBA, offseason
                return httpx.Response(200, json=[])
            if request.url.path.endswith("/markets/straight"):
                return httpx.Response(200, json=[_market(1)])
            return httpx.Response(200, json=[matchup])

        adapter = PinnacleAdapter(
            ["MLB", "NBA", "WNBA"],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            request_pause=0.0,
        )
        try:
            raws = adapter.fetch_raw()
        finally:
            adapter.close()
        endpoints = {raw.endpoint for raw in raws}
        assert f"matchups:{MLB_SCOPE}" in endpoints
        assert f"matchups:{WNBA_SCOPE}" in endpoints
        assert not [e for e in endpoints if "487" in e]

    def test_the_sport_endpoint_falls_back_to_leagues(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(path)
            if path.startswith("/0.1/sports/33/matchups"):
                return httpx.Response(401, json={"error": "unauthorized"})
            if path == "/0.1/sports/33/leagues":
                return httpx.Response(
                    200,
                    json=[
                        {"id": 4356, "name": "ATP Washington - R1", "matchupCount": 10},
                        {"id": 9351, "name": "ATP Challenger Liberec - R1", "matchupCount": 12},
                    ],
                )
            if path.endswith("/markets/straight"):
                return httpx.Response(200, json=[_market(1)])
            return httpx.Response(
                200,
                json=[
                    _matchup(
                        1,
                        home="Marcos Giron",
                        away="Cruz Hewitt",
                        sport_id=33,
                        league_id=4356,
                        league_name="ATP Washington - R1",
                        units="Sets",
                    )
                ],
            )

        adapter = PinnacleAdapter(
            ["ATP", "ATP_CHALLENGER"],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            request_pause=0.0,
        )
        try:
            raws = adapter.fetch_raw()
        finally:
            adapter.close()
        assert "/0.1/sports/33/leagues" in calls
        endpoints = {raw.endpoint for raw in raws}
        # The busiest league first, and the index itself is captured.
        assert f"matchups:{league_scope('ATP_CHALLENGER', 9351)}" in endpoints
        assert f"matchups:{league_scope('ATP', 4356)}" in endpoints
        assert any(e.startswith("leagues:") for e in endpoints)
        assert parse_pinnacle(raws).quotes

    def test_the_fallback_is_bounded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.startswith("/0.1/sports/29/matchups"):
                return httpx.Response(500, json={"error": "boom"})
            if path == "/0.1/sports/29/leagues":
                return httpx.Response(
                    200,
                    json=[
                        {"id": 3000 + n, "name": f"Country {n} - League", "matchupCount": 100 - n}
                        for n in range(50)
                    ],
                )
            if path.endswith("/markets/straight"):
                return httpx.Response(200, json=[_market(1)])
            return httpx.Response(
                200,
                json=[
                    _matchup(
                        1,
                        home="Wycombe Wanderers",
                        away="Stevenage",
                        sport_id=29,
                        league_id=3000,
                        league_name="Country 0 - League",
                    )
                ],
            )

        adapter = PinnacleAdapter(
            [SOCCER_CATCH_ALL],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            request_pause=0.0,
            max_fallback_leagues=3,
        )
        try:
            raws = adapter.fetch_raw()
        finally:
            adapter.close()
        matchup_responses = [r for r in raws if r.endpoint.startswith("matchups:")]
        assert len(matchup_responses) == 3


class TestPriceOrientationRegression:
    """A price must belong to the participant it was quoted for.

    Pinnacle tags each price with its *own* ``designation`` ("home"/"away").  For
    a sport with a real home side that label and ours agree.  For tennis there is
    no home player: ``src.events.orient`` imposes an ordering by participant key
    while Pinnacle orders the two names however it likes, and on the captured
    slate the two disagreed on **5 of 8** cross-book matches.

    The rows still looked perfect — correct ``home_team``, plausible price — but
    the price attached to our home player was the other player's.  Tommy Paul came
    out at 4.12 where FanDuel had him at 1.244.  A position built from that pair
    backs the same player twice and reports a guaranteed profit, which is a
    phantom arbitrage in its purest form.
    """

    def test_the_favourite_is_the_same_player_as_at_another_book(self, rows) -> None:
        """The cross-book check that originally exposed this.

        Two books can differ on the size of a price but not on *which* competitor
        is favoured; a mirrored pair means one of them is mislabelled.
        """
        from src.sources.fanduel import FanDuelAdapter

        store = RawStore(FIXTURE_DIR)
        fanduel_raws = [
            store.read(path) for path in sorted(FIXTURE_DIR.glob("fanduel__*.json"))
        ]
        adapter = FanDuelAdapter()
        try:
            fanduel = adapter.parse(fanduel_raws).quotes
        finally:
            adapter.close()

        def moneylines(quotes):
            return {
                (q.event_key, q.selection): q
                for q in quotes
                if q.sport is Sport.TENNIS and q.market is Market.MONEYLINE
            }

        ours, theirs = moneylines(rows), moneylines(fanduel)
        shared = {k[0] for k in ours} & {k[0] for k in theirs}
        assert shared, "no cross-book tennis event to compare — the check is vacuous"

        for event_key in sorted(shared):
            pin_home = ours.get((event_key, Selection.HOME))
            pin_away = ours.get((event_key, Selection.AWAY))
            fd_home = theirs.get((event_key, Selection.HOME))
            fd_away = theirs.get((event_key, Selection.AWAY))
            if not all((pin_home, pin_away, fd_home, fd_away)):
                continue
            assert (pin_home.decimal_odds < pin_away.decimal_odds) == (
                fd_home.decimal_odds < fd_away.decimal_odds
            ), (
                f"{event_key}: prices are mirrored — pinnacle has "
                f"{pin_home.home_team} at {pin_home.decimal_odds:.3f}, fanduel at "
                f"{fd_home.decimal_odds:.3f}"
            )

    def test_the_designation_is_translated_through_our_orientation(self) -> None:
        """The mechanism, isolated from any book's live slate.

        Pinnacle calls Majchrzak home; our key order makes Tommy Paul home.  The
        price Pinnacle quoted for Majchrzak must therefore come back as the *away*
        price, not the home one.
        """
        matchups = [
            _matchup(
                900001,
                home="Kamil Majchrzak",
                away="Tommy Paul",
                sport_id=33,
                league_id=4356,
                league_name="ATP Los Cabos - R1",
            )
        ]
        markets = [
            _market(
                900001,
                prices=[
                    {"designation": "home", "price": 320},   # Pinnacle's home = Majchrzak
                    {"designation": "away", "price": -400},  # Pinnacle's away = Paul
                ],
            )
        ]
        outcome = parse_pinnacle(_pair(matchups, markets, token=ATP_SCOPE))
        assert outcome.rejections == []
        by_selection = {q.selection: q for q in outcome.quotes}

        home_row = by_selection[Selection.HOME]
        away_row = by_selection[Selection.AWAY]
        # Our orientation sorts "TENNIS-kamilmajchrzak" before "TENNIS-paultommy".
        assert home_row.home_team == "Tommy Paul"
        assert away_row.away_team == "Kamil Majchrzak"
        # Paul was the -400 favourite, so the home row must carry his short price.
        assert home_row.american_odds == -400
        assert away_row.american_odds == 320

    def test_a_team_sport_still_trusts_the_books_home_side(self) -> None:
        """The fix must not invert the sports where the book's label is the fact."""
        matchups = [
            _matchup(
                900002,
                home="Cincinnati Reds",
                away="Cleveland Guardians",
                sport_id=3,
                league_id=246,
                league_name="MLB",
            )
        ]
        markets = [
            _market(
                900002,
                prices=[
                    {"designation": "home", "price": -150},
                    {"designation": "away", "price": 130},
                ],
            )
        ]
        outcome = parse_pinnacle(_pair(matchups, markets, token=MLB_SCOPE))
        by_selection = {q.selection: q for q in outcome.quotes}
        assert by_selection[Selection.HOME].home_team == "Cincinnati Reds"
        assert by_selection[Selection.HOME].american_odds == -150
        assert by_selection[Selection.AWAY].american_odds == 130
