from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.models import Bookmaker, BookmakerMarket, Event, Outcome


def _make_event(
    bookmakers: list[Bookmaker],
    sport_key: str = "basketball_nba",
    event_id: str = "evt_1",
    home: str = "Team A",
    away: str = "Team B",
) -> Event:
    return Event(
        id=event_id,
        sport_key=sport_key,
        sport_title="NBA",
        commence_time=datetime.now(UTC) + timedelta(hours=2),
        home_team=home,
        away_team=away,
        bookmakers=bookmakers,
    )


def _bm(key: str, title: str, market_key: str, outcomes: list[Outcome]) -> Bookmaker:
    return Bookmaker(
        key=key,
        title=title,
        markets=[
            BookmakerMarket(
                key=market_key,
                last_update=datetime.now(UTC),
                outcomes=outcomes,
            )
        ],
    )


@pytest.fixture()
def two_way_arb_event() -> Event:
    """Two bookmakers with h2h odds that create a ~4.5% arb."""
    return _make_event(
        bookmakers=[
            _bm("book_a", "Book A", "h2h", [
                Outcome(name="Team A", price=2.20),
                Outcome(name="Team B", price=1.70),
            ]),
            _bm("book_b", "Book B", "h2h", [
                Outcome(name="Team A", price=1.80),
                Outcome(name="Team B", price=2.30),
            ]),
        ],
    )


@pytest.fixture()
def no_arb_event() -> Event:
    """Normal h2h odds — no arbitrage."""
    return _make_event(
        bookmakers=[
            _bm("book_a", "Book A", "h2h", [
                Outcome(name="Team A", price=1.90),
                Outcome(name="Team B", price=1.90),
            ]),
            _bm("book_b", "Book B", "h2h", [
                Outcome(name="Team A", price=1.85),
                Outcome(name="Team B", price=1.95),
            ]),
        ],
    )


@pytest.fixture()
def three_way_arb_event() -> Event:
    """Soccer match with home/draw/away creating a 3-way arb."""
    return _make_event(
        sport_key="soccer_epl",
        home="Liverpool",
        away="Arsenal",
        bookmakers=[
            _bm("book_a", "Book A", "h2h", [
                Outcome(name="Liverpool", price=3.50),
                Outcome(name="Draw", price=3.20),
                Outcome(name="Arsenal", price=2.40),
            ]),
            _bm("book_b", "Book B", "h2h", [
                Outcome(name="Liverpool", price=3.00),
                Outcome(name="Draw", price=4.00),
                Outcome(name="Arsenal", price=2.60),
            ]),
            _bm("book_c", "Book C", "h2h", [
                Outcome(name="Liverpool", price=3.80),
                Outcome(name="Draw", price=3.40),
                Outcome(name="Arsenal", price=2.80),
            ]),
        ],
    )


@pytest.fixture()
def spread_arb_event() -> Event:
    """Spread market with an arb across two books on the same line."""
    return _make_event(
        bookmakers=[
            _bm("book_a", "Book A", "spreads", [
                Outcome(name="Team A", price=2.15, point=-3.5),
                Outcome(name="Team B", price=1.80, point=3.5),
            ]),
            _bm("book_b", "Book B", "spreads", [
                Outcome(name="Team A", price=1.85, point=-3.5),
                Outcome(name="Team B", price=2.10, point=3.5),
            ]),
        ],
    )


def build_event_from_scenario(scenario) -> Event:
    """Convert a BacktestScenario into an Event model."""
    from tests.fixtures.backtest_scenarios import BacktestScenario

    assert isinstance(scenario, BacktestScenario)
    bookmakers: list[Bookmaker] = []
    for bq in scenario.books:
        outcomes = [
            Outcome(name=name, price=odds, point=bq.point)
            for name, odds in bq.outcomes.items()
        ]
        bookmakers.append(
            _bm(bq.key, bq.title, scenario.market_key, outcomes)
        )
    return _make_event(
        bookmakers=bookmakers,
        sport_key=scenario.sport_key,
        home=scenario.home,
        away=scenario.away,
    )


@pytest.fixture()
def totals_arb_event() -> Event:
    """Totals market with an arb on Over/Under 215.5."""
    return _make_event(
        bookmakers=[
            _bm("book_a", "Book A", "totals", [
                Outcome(name="Over", price=2.15, point=215.5),
                Outcome(name="Under", price=1.80, point=215.5),
            ]),
            _bm("book_b", "Book B", "totals", [
                Outcome(name="Over", price=1.85, point=215.5),
                Outcome(name="Under", price=2.10, point=215.5),
            ]),
        ],
    )
