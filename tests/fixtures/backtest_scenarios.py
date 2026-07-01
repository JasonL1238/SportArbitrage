"""Curated odds scenarios for backtesting arb detection and stake math.

Each scenario describes bookmaker quotes for one event. Expected outcomes are
pre-computed from the best price per outcome across all books.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class BookQuote:
    """One bookmaker's prices for a single market."""

    key: str
    title: str
    outcomes: dict[str, float]  # outcome_name -> decimal odds
    point: Optional[float] = None


@dataclass(frozen=True)
class BacktestScenario:
    """A reproducible odds snapshot with known expected results."""

    name: str
    sport_key: str
    market_key: str
    home: str
    away: str
    books: tuple[BookQuote, ...]
    expect_arb: bool
    min_margin: float = 0.01
    total_stake: float = 100.0
    point: Optional[float] = None
    expected_margin: Optional[float] = None
    expected_best_books: Optional[dict[str, str]] = None  # outcome -> book_key
    tags: tuple[str, ...] = field(default_factory=tuple)


def _margin_from_best(odds: list[float]) -> float:
    return 1 - sum(1 / o for o in odds)


# ---------------------------------------------------------------------------
# Two-way H2H scenarios
# ---------------------------------------------------------------------------

TWO_WAY_SCENARIOS: list[BacktestScenario] = [
    BacktestScenario(
        name="classic_cross_book_4pct",
        sport_key="basketball_nba",
        market_key="h2h",
        home="Lakers",
        away="Celtics",
        books=(
            BookQuote("draftkings", "DraftKings", {"Lakers": 2.20, "Celtics": 1.70}),
            BookQuote("fanduel", "FanDuel", {"Lakers": 1.80, "Celtics": 2.30}),
        ),
        expect_arb=True,
        expected_margin=_margin_from_best([2.20, 2.30]),
        expected_best_books={"Lakers": "draftkings", "Celtics": "fanduel"},
        tags=("two_way", "nba", "classic"),
    ),
    BacktestScenario(
        name="tight_arb_1pct",
        sport_key="basketball_nba",
        market_key="h2h",
        home="Knicks",
        away="Heat",
        books=(
            BookQuote("book_a", "Book A", {"Knicks": 2.02, "Heat": 1.98}),
            BookQuote("book_b", "Book B", {"Knicks": 1.97, "Heat": 2.03}),
        ),
        expect_arb=True,
        min_margin=0.005,
        expected_margin=_margin_from_best([2.02, 2.03]),
        tags=("two_way", "marginal"),
    ),
    BacktestScenario(
        name="no_arb_efficient_market",
        sport_key="basketball_nba",
        market_key="h2h",
        home="Bucks",
        away="Sixers",
        books=(
            BookQuote("book_a", "Book A", {"Bucks": 1.90, "Sixers": 1.90}),
            BookQuote("book_b", "Book B", {"Bucks": 1.85, "Sixers": 1.95}),
            BookQuote("book_c", "Book C", {"Bucks": 1.88, "Sixers": 1.92}),
        ),
        expect_arb=False,
        expected_margin=_margin_from_best([1.90, 1.95]),
        tags=("two_way", "no_arb"),
    ),
    BacktestScenario(
        name="single_book_no_cross_arb",
        sport_key="baseball_mlb",
        market_key="h2h",
        home="Yankees",
        away="Red Sox",
        books=(
            BookQuote("pinnacle", "Pinnacle", {"Yankees": 1.95, "Red Sox": 1.95}),
        ),
        expect_arb=False,
        tags=("two_way", "single_book"),
    ),
    BacktestScenario(
        name="five_book_best_price_hunt",
        sport_key="icehockey_nhl",
        market_key="h2h",
        home="Rangers",
        away="Islanders",
        books=(
            BookQuote("bk1", "Book 1", {"Rangers": 2.05, "Islanders": 1.75}),
            BookQuote("bk2", "Book 2", {"Rangers": 1.90, "Islanders": 1.95}),
            BookQuote("bk3", "Book 3", {"Rangers": 2.15, "Islanders": 1.80}),
            BookQuote("bk4", "Book 4", {"Rangers": 1.85, "Islanders": 2.25}),
            BookQuote("bk5", "Book 5", {"Rangers": 2.00, "Islanders": 2.00}),
        ),
        expect_arb=True,
        expected_margin=_margin_from_best([2.15, 2.25]),
        expected_best_books={"Rangers": "bk3", "Islanders": "bk4"},
        tags=("two_way", "multi_book"),
    ),
    BacktestScenario(
        name="heavy_favorite_underdog_arb",
        sport_key="americanfootball_nfl",
        market_key="h2h",
        home="Chiefs",
        away="Panthers",
        books=(
            BookQuote("book_a", "Book A", {"Chiefs": 1.20, "Panthers": 5.50}),
            BookQuote("book_b", "Book B", {"Chiefs": 1.15, "Panthers": 6.50}),
        ),
        expect_arb=True,
        expected_margin=_margin_from_best([1.20, 6.50]),
        tags=("two_way", "nfl", "lopsided"),
    ),
    BacktestScenario(
        name="break_even_odds",
        sport_key="basketball_nba",
        market_key="h2h",
        home="A",
        away="B",
        books=(
            BookQuote("book_a", "Book A", {"A": 2.00, "B": 2.00}),
            BookQuote("book_b", "Book B", {"A": 2.00, "B": 2.00}),
        ),
        expect_arb=False,
        min_margin=0.001,
        expected_margin=0.0,
        tags=("two_way", "edge"),
    ),
    BacktestScenario(
        name="sub_1pct_arb_filtered",
        sport_key="basketball_nba",
        market_key="h2h",
        home="A",
        away="B",
        books=(
            BookQuote("book_a", "Book A", {"A": 2.005, "B": 1.995}),
            BookQuote("book_b", "Book B", {"A": 1.995, "B": 2.005}),
        ),
        expect_arb=False,
        min_margin=0.01,
        tags=("two_way", "threshold"),
    ),
]

# ---------------------------------------------------------------------------
# Three-way (soccer) scenarios
# ---------------------------------------------------------------------------

THREE_WAY_SCENARIOS: list[BacktestScenario] = [
    BacktestScenario(
        name="epl_three_way_classic",
        sport_key="soccer_epl",
        market_key="h2h",
        home="Liverpool",
        away="Arsenal",
        books=(
            BookQuote("book_a", "Book A", {"Liverpool": 3.50, "Draw": 3.20, "Arsenal": 2.40}),
            BookQuote("book_b", "Book B", {"Liverpool": 3.00, "Draw": 4.00, "Arsenal": 2.60}),
            BookQuote("book_c", "Book C", {"Liverpool": 3.80, "Draw": 3.40, "Arsenal": 2.80}),
        ),
        expect_arb=True,
        expected_margin=_margin_from_best([3.80, 4.00, 2.80]),
        tags=("three_way", "soccer"),
    ),
    BacktestScenario(
        name="la_liga_no_arb",
        sport_key="soccer_spain_la_liga",
        market_key="h2h",
        home="Barcelona",
        away="Real Madrid",
        books=(
            BookQuote("book_a", "Book A", {"Barcelona": 2.50, "Draw": 3.30, "Real Madrid": 2.80}),
            BookQuote("book_b", "Book B", {"Barcelona": 2.45, "Draw": 3.25, "Real Madrid": 2.75}),
        ),
        expect_arb=False,
        tags=("three_way", "soccer", "no_arb"),
    ),
    BacktestScenario(
        name="serie_a_six_books",
        sport_key="soccer_italy_serie_a",
        market_key="h2h",
        home="Juventus",
        away="Milan",
        books=tuple(
            BookQuote(f"bk{i}", f"Book {i}", {
                "Juventus": [2.80, 2.75, 3.10, 2.70, 2.85, 2.78][i],
                "Draw": [3.10, 3.20, 3.25, 3.50, 3.15, 3.30][i],
                "Milan": [2.60, 2.55, 2.65, 2.50, 2.90, 2.58][i],
            })
            for i in range(6)
        ),
        expect_arb=True,
        expected_margin=_margin_from_best([3.10, 3.50, 2.90]),
        tags=("three_way", "multi_book"),
    ),
]

# ---------------------------------------------------------------------------
# Spread scenarios
# ---------------------------------------------------------------------------

SPREAD_SCENARIOS: list[BacktestScenario] = [
    BacktestScenario(
        name="nba_spread_-3.5",
        sport_key="basketball_nba",
        market_key="spreads",
        home="Team A",
        away="Team B",
        point=3.5,
        books=(
            BookQuote("book_a", "Book A", {"Team A": 2.15, "Team B": 1.80}, point=-3.5),
            BookQuote("book_b", "Book B", {"Team A": 1.85, "Team B": 2.10}, point=-3.5),
        ),
        expect_arb=True,
        expected_margin=_margin_from_best([2.15, 2.10]),
        tags=("spread",),
    ),
    BacktestScenario(
        name="nfl_spread_no_arb",
        sport_key="americanfootball_nfl",
        market_key="spreads",
        home="Team A",
        away="Team B",
        point=7.0,
        books=(
            BookQuote("book_a", "Book A", {"Team A": 1.91, "Team B": 1.91}, point=-7.0),
            BookQuote("book_b", "Book B", {"Team A": 1.90, "Team B": 1.92}, point=-7.0),
        ),
        expect_arb=False,
        tags=("spread", "no_arb"),
    ),
    BacktestScenario(
        name="different_lines_no_merge",
        sport_key="basketball_nba",
        market_key="spreads",
        home="Team A",
        away="Team B",
        point=None,  # two separate lines — validated separately
        books=(
            BookQuote("book_a", "Book A", {"Team A": 2.20, "Team B": 1.70}, point=-3.5),
            BookQuote("book_b", "Book B", {"Team A": 1.70, "Team B": 2.20}, point=-4.5),
        ),
        expect_arb=False,
        tags=("spread", "multi_line"),
    ),
]

# ---------------------------------------------------------------------------
# Totals scenarios
# ---------------------------------------------------------------------------

TOTALS_SCENARIOS: list[BacktestScenario] = [
    BacktestScenario(
        name="nba_total_215.5",
        sport_key="basketball_nba",
        market_key="totals",
        home="Team A",
        away="Team B",
        point=215.5,
        books=(
            BookQuote("book_a", "Book A", {"Over": 2.15, "Under": 1.80}, point=215.5),
            BookQuote("book_b", "Book B", {"Over": 1.85, "Under": 2.10}, point=215.5),
        ),
        expect_arb=True,
        expected_margin=_margin_from_best([2.15, 2.10]),
        tags=("totals",),
    ),
    BacktestScenario(
        name="mlb_total_no_arb",
        sport_key="baseball_mlb",
        market_key="totals",
        home="Team A",
        away="Team B",
        point=8.5,
        books=(
            BookQuote("book_a", "Book A", {"Over": 1.90, "Under": 1.90}, point=8.5),
            BookQuote("book_b", "Book B", {"Over": 1.88, "Under": 1.92}, point=8.5),
        ),
        expect_arb=False,
        tags=("totals", "no_arb"),
    ),
]

# ---------------------------------------------------------------------------
# Stake sizing backtests (pure math, no event structure)
# ---------------------------------------------------------------------------

STAKE_BACKTEST_CASES: list[dict] = [
    {"odds": [2.20, 2.30], "stake": 100.0},
    {"odds": [2.20, 2.30], "stake": 50.0},
    {"odds": [2.20, 2.30], "stake": 1000.0},
    {"odds": [2.20, 2.30], "stake": 0.01},
    {"odds": [3.80, 4.00, 2.80], "stake": 500.0},
    {"odds": [1.15, 7.00], "stake": 200.0},
    {"odds": [2.02, 2.03], "stake": 100.0},
    {"odds": [2.15, 2.10], "stake": 250.0},
    {"odds": [1.50, 3.50], "stake": 100.0},
    {"odds": [10.0, 1.12], "stake": 100.0},
]

MARGIN_BACKTEST_CASES: list[dict] = [
    {"odds": [2.20, 2.30], "expect_positive": True},
    {"odds": [1.90, 1.90], "expect_positive": False},
    {"odds": [2.0, 2.0], "expect_zero": True},
    {"odds": [3.80, 4.00, 2.80], "expect_positive": True},
    {"odds": [2.50, 3.00, 2.80], "expect_positive": False},
    {"odds": [1.01, 50.0], "expect_positive": False},
    {"odds": [1.50, 2.50], "expect_positive": False},
    {"odds": [2.10, 2.10], "expect_positive": True},
]

ALL_SCENARIOS: list[BacktestScenario] = (
    TWO_WAY_SCENARIOS
    + THREE_WAY_SCENARIOS
    + SPREAD_SCENARIOS
    + TOTALS_SCENARIOS
)
