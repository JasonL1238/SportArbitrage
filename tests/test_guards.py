"""Tests for executable-opportunity guards: pre-match, distinct books, complete outcomes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.arb import (
    _expected_outcomes,
    _has_distinct_books,
    filter_started_events,
    find_arbs,
)
from src.models import BestOutcome, Bookmaker, BookmakerMarket, Event, Outcome
from tests.conftest import _bm, _make_event


class TestFilterStartedEvents:
    def test_future_event_kept(self):
        event = Event(
            id="e1", sport_key="basketball_nba", sport_title="NBA",
            commence_time=datetime.now(UTC) + timedelta(hours=2),
            home_team="A", away_team="B",
        )
        result = filter_started_events([event])
        assert len(result) == 1

    def test_past_event_removed(self):
        event = Event(
            id="e1", sport_key="basketball_nba", sport_title="NBA",
            commence_time=datetime.now(UTC) - timedelta(hours=1),
            home_team="A", away_team="B",
        )
        result = filter_started_events([event])
        assert len(result) == 0

    def test_mixed_events(self):
        future = Event(
            id="e1", sport_key="basketball_nba", sport_title="NBA",
            commence_time=datetime.now(UTC) + timedelta(hours=2),
            home_team="A", away_team="B",
        )
        past = Event(
            id="e2", sport_key="basketball_nba", sport_title="NBA",
            commence_time=datetime.now(UTC) - timedelta(minutes=30),
            home_team="C", away_team="D",
        )
        result = filter_started_events([future, past])
        assert len(result) == 1
        assert result[0].id == "e1"

    def test_empty_list(self):
        assert filter_started_events([]) == []


class TestDistinctBooksGuard:
    def test_same_book_both_legs_rejected(self):
        event = _make_event(
            bookmakers=[
                _bm("winner", "Winner", "h2h", [
                    Outcome(name="Team A", price=2.50),
                    Outcome(name="Team B", price=2.50),
                ]),
                _bm("loser", "Loser", "h2h", [
                    Outcome(name="Team A", price=1.50),
                    Outcome(name="Team B", price=1.50),
                ]),
            ],
        )
        without_guard = find_arbs([event], min_margin=0.0, require_distinct_books=False)
        assert len(without_guard) >= 1
        assert all(
            b.bookmaker_key == "winner" for b in without_guard[0].best_outcomes
        )

        with_guard = find_arbs([event], min_margin=0.0, require_distinct_books=True)
        assert len(with_guard) == 0

    def test_cross_book_arb_kept(self):
        event = _make_event(
            bookmakers=[
                _bm("book_a", "A", "h2h", [
                    Outcome(name="Team A", price=2.20),
                    Outcome(name="Team B", price=1.70),
                ]),
                _bm("book_b", "B", "h2h", [
                    Outcome(name="Team A", price=1.80),
                    Outcome(name="Team B", price=2.30),
                ]),
            ],
        )
        results = find_arbs([event], min_margin=0.01, require_distinct_books=True)
        assert len(results) == 1
        books = {b.bookmaker_key for b in results[0].best_outcomes}
        assert len(books) == 2

    def test_has_distinct_books_helper(self):
        same = [
            BestOutcome(outcome_name="A", bookmaker_key="bk1", bookmaker_title="1", decimal_odds=2.0),
            BestOutcome(outcome_name="B", bookmaker_key="bk1", bookmaker_title="1", decimal_odds=2.0),
        ]
        assert _has_distinct_books(same) is False

        diff = [
            BestOutcome(outcome_name="A", bookmaker_key="bk1", bookmaker_title="1", decimal_odds=2.0),
            BestOutcome(outcome_name="B", bookmaker_key="bk2", bookmaker_title="2", decimal_odds=2.0),
        ]
        assert _has_distinct_books(diff) is True


class TestCompleteOutcomesGuard:
    def test_incomplete_h2h_filtered(self):
        event = _make_event(
            bookmakers=[
                _bm("book_a", "A", "h2h", [
                    Outcome(name="Team A", price=2.50),
                ]),
            ],
        )
        without = find_arbs([event], min_margin=-1.0, require_complete_outcomes=False)
        with_guard = find_arbs([event], min_margin=-1.0, require_complete_outcomes=True)
        assert len(without) >= 1
        assert len(with_guard) == 0

    def test_complete_h2h_kept(self):
        event = _make_event(
            bookmakers=[
                _bm("book_a", "A", "h2h", [
                    Outcome(name="Team A", price=2.20),
                    Outcome(name="Team B", price=2.30),
                ]),
            ],
        )
        results = find_arbs([event], min_margin=0.0, require_complete_outcomes=True)
        assert len(results) >= 1
        assert results[0].outcome_count == 2

    def test_soccer_h2h_expects_three(self):
        event = _make_event(
            sport_key="soccer_epl",
            home="Liverpool",
            away="Arsenal",
            bookmakers=[
                _bm("book_a", "A", "h2h", [
                    Outcome(name="Liverpool", price=3.50),
                    Outcome(name="Arsenal", price=2.80),
                ]),
            ],
        )
        results = find_arbs([event], min_margin=-1.0, require_complete_outcomes=True)
        assert len(results) == 0

    def test_soccer_h2h_with_draw_kept(self):
        event = _make_event(
            sport_key="soccer_epl",
            home="Liverpool",
            away="Arsenal",
            bookmakers=[
                _bm("book_a", "A", "h2h", [
                    Outcome(name="Liverpool", price=3.50),
                    Outcome(name="Draw", price=4.00),
                    Outcome(name="Arsenal", price=2.80),
                ]),
            ],
        )
        results = find_arbs([event], min_margin=-1.0, require_complete_outcomes=True)
        assert len(results) >= 1
        assert results[0].outcome_count == 3


class TestExpectedOutcomes:
    def test_h2h_default(self):
        assert _expected_outcomes("h2h", "basketball_nba") == 2

    def test_h2h_soccer(self):
        assert _expected_outcomes("h2h", "soccer_epl") == 3

    def test_spreads(self):
        assert _expected_outcomes("spreads", "basketball_nba") == 2

    def test_totals(self):
        assert _expected_outcomes("totals", "baseball_mlb") == 2

    def test_unknown_market(self):
        assert _expected_outcomes("player_props", "any") is None


class TestCombinedGuards:
    def test_all_guards_together(self):
        event = _make_event(
            bookmakers=[
                _bm("book_a", "A", "h2h", [
                    Outcome(name="Team A", price=2.20),
                    Outcome(name="Team B", price=1.70),
                ]),
                _bm("book_b", "B", "h2h", [
                    Outcome(name="Team A", price=1.80),
                    Outcome(name="Team B", price=2.30),
                ]),
            ],
        )
        results = find_arbs(
            [event],
            min_margin=0.01,
            require_distinct_books=True,
            require_complete_outcomes=True,
        )
        assert len(results) == 1
        assert results[0].outcome_count == 2
        assert len({b.bookmaker_key for b in results[0].best_outcomes}) == 2

    def test_backward_compat_defaults(self):
        """Guards are off by default — existing callers are unaffected."""
        event = _make_event(
            bookmakers=[
                _bm("sole", "Sole", "h2h", [
                    Outcome(name="Team A", price=2.50),
                    Outcome(name="Team B", price=2.50),
                ]),
            ],
        )
        results = find_arbs([event], min_margin=0.0)
        assert len(results) >= 1
