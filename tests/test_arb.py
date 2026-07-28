"""Tests for arbitrage detection.

The captured slate contains no arbitrage — three correctly-priced books rarely
produce one, in any sport — so the positive cases here are built from
**synthetic quotes** constructed to hit specific traps.  They are labelled as
synthetic throughout and are never presented as observed prices; the real
captured fixtures are exercised separately at the bottom of this file, where the
expected answer is "no opportunities".

Every expected number below is derived by hand in the test itself, so a change
in the engine's arithmetic fails here rather than silently changing what gets
reported as risk-free money.

Most of what is being tested is a *sport* fact rather than an arithmetic one.  The
same two prices on the same two-way moneyline are a complete risk-free position
in baseball, a position with a zero floor in the NFL (a tie voids both legs), and
a refusal in soccer (the draw leg is missing and nothing on the row says whether
it voids or loses).  Those three readings differ by the whole bankroll, so each
one has its own test.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.arb import (
    IMPLAUSIBLE_MARGIN,
    LINE_HALF,
    LINE_QUARTER,
    LINE_UNSUPPORTED,
    LINE_WHOLE,
    REFUSE_MARGIN,
    arb_margin,
    arb_roi,
    best_prices,
    canonical_line,
    contract_shape,
    find_opportunities,
    group_key,
    line_granularity,
    settlement_outcomes,
    stake_candidates,
    stake_split,
)
from src.schema import Market, Period, QuoteStatus, Quote, Selection, Side, Sport
from tests.conftest import make_quote

# The synthetic fixtures are shared with the validation tests rather than
# duplicated, so "a clean NFL game" means one thing across the suite.
from tests.test_validation import (
    MLB_GAME,
    NFL_GAME,
    NHL_GAME,
    SOCCER_GAME,
    TENNIS_MATCH,
    WNBA_GAME,
    Fixture,
    _quote,
)

# ── pure arithmetic ──────────────────────────────────────────────────────────


class TestMargin:
    def test_two_equal_prices_above_evens_is_an_arb(self) -> None:
        # 1/2.5 + 1/2.5 = 0.8, so 20% of the payout is kept.
        assert arb_margin([2.5, 2.5]) == pytest.approx(0.2)

    def test_a_fair_market_has_zero_margin(self) -> None:
        # 1/3.0 + 1/1.5 = 0.3333 + 0.6667 = 1.0 exactly.
        assert arb_margin([3.0, 1.5]) == pytest.approx(0.0, abs=1e-12)

    def test_a_juiced_market_has_negative_margin(self) -> None:
        # -110 both sides: 1/1.909 * 2 = 1.0476.
        assert arb_margin([1 + 100 / 110, 1 + 100 / 110]) == pytest.approx(-0.047619, abs=1e-6)

    def test_three_way_margin(self) -> None:
        assert arb_margin([4.0, 4.0, 4.0]) == pytest.approx(0.25)

    def test_margin_and_roi_are_not_the_same_number(self) -> None:
        """Confusing the two overstates the edge; ROI is always the larger."""
        odds = [2.10, 2.10]
        margin = arb_margin(odds)  # 1 - 0.952381 = 0.047619
        roi = arb_roi(odds)  # 1/0.952381 - 1 = 0.05
        assert margin == pytest.approx(0.047619, abs=1e-6)
        assert roi == pytest.approx(0.05, abs=1e-9)
        assert roi > margin

    @pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -2.0])
    def test_odds_at_or_below_evens_are_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError):
            arb_margin([2.0, bad])

    def test_empty_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            arb_margin([])


class TestStakeSplit:
    def test_equal_odds_split_evenly(self) -> None:
        assert stake_split([2.5, 2.5], 100.0) == pytest.approx([50.0, 50.0])

    def test_payouts_are_equal_whatever_the_odds(self) -> None:
        odds = [2.10, 2.05, 7.0]
        stakes = stake_split(odds, 250.0)
        payouts = [stake * odd for stake, odd in zip(stakes, odds)]
        assert sum(stakes) == pytest.approx(250.0)
        assert payouts[0] == pytest.approx(payouts[1])
        assert payouts[1] == pytest.approx(payouts[2])

    def test_hand_computed_split(self) -> None:
        # 1/2.0 = 0.5, 1/3.0 = 0.3333; S = 0.83333.
        # stake_1 = 100 * 0.5/0.83333 = 60, stake_2 = 100 * 0.33333/0.83333 = 40.
        assert stake_split([2.0, 3.0], 100.0) == pytest.approx([60.0, 40.0])

    def test_zero_or_negative_bankroll_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            stake_split([2.5, 2.5], 0.0)


# ── line orientation and granularity ─────────────────────────────────────────


class TestCanonicalLine:
    def test_spread_is_expressed_from_the_home_perspective(self) -> None:
        home = make_quote(market=Market.SPREAD, selection=Selection.HOME, line=-1.5)
        away = make_quote(market=Market.SPREAD, selection=Selection.AWAY, line=1.5)
        assert canonical_line(home) == -1.5
        assert canonical_line(away) == -1.5

    def test_mirrored_spreads_land_in_one_group(self) -> None:
        home = make_quote(market=Market.SPREAD, selection=Selection.HOME, line=-1.5)
        away = make_quote(market=Market.SPREAD, selection=Selection.AWAY, line=1.5)
        assert group_key(home) == group_key(away)

    def test_unmirrored_spreads_stay_apart(self) -> None:
        """Home -1.5 against away +2.5 is a middle, not an arbitrage, and the
        two must never be grouped as opposite halves of one market."""
        home = make_quote(market=Market.SPREAD, selection=Selection.HOME, line=-1.5)
        away = make_quote(market=Market.SPREAD, selection=Selection.AWAY, line=2.5)
        assert group_key(home) != group_key(away)

    def test_totals_share_their_line_directly(self) -> None:
        over = make_quote(market=Market.TOTAL, selection=Selection.OVER, line=8.5)
        under = make_quote(market=Market.TOTAL, selection=Selection.UNDER, line=8.5)
        assert canonical_line(over) == canonical_line(under) == 8.5
        assert group_key(over) == group_key(under)

    def test_different_totals_stay_apart(self) -> None:
        over = make_quote(market=Market.TOTAL, selection=Selection.OVER, line=8.5)
        under = make_quote(market=Market.TOTAL, selection=Selection.UNDER, line=9.5)
        assert group_key(over) != group_key(under)

    def test_team_totals_are_separated_by_side(self) -> None:
        home_side = make_quote(
            market=Market.TEAM_TOTAL, selection=Selection.OVER, line=4.5, side=Side.HOME
        )
        away_side = make_quote(
            market=Market.TEAM_TOTAL, selection=Selection.UNDER, line=4.5, side=Side.AWAY
        )
        assert group_key(home_side) != group_key(away_side)

    def test_the_same_number_in_two_sports_is_never_one_group(self) -> None:
        """An event key is built from namespaced participant keys, so a baseball
        8.5 and a basketball 8.5 cannot collide even though the number matches."""
        baseball = _quote(
            MLB_GAME, "book_a", market=Market.TOTAL, selection=Selection.OVER, line=8.5
        )
        basketball = _quote(
            WNBA_GAME, "book_a", market=Market.TOTAL, selection=Selection.OVER, line=8.5
        )
        assert group_key(baseball) != group_key(basketball)


class TestLineGranularity:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (0.0, LINE_WHOLE),
            (1.0, LINE_WHOLE),
            (-2.0, LINE_WHOLE),
            (9.0, LINE_WHOLE),
            (8.5, LINE_HALF),
            (-1.5, LINE_HALF),
            (2.75, LINE_QUARTER),
            (3.25, LINE_QUARTER),
            (-0.25, LINE_QUARTER),
            (-1.75, LINE_QUARTER),
            (8.3, LINE_UNSUPPORTED),
            (0.1, LINE_UNSUPPORTED),
        ],
    )
    def test_lines_are_classified_by_what_they_can_do(self, line: float, expected: str) -> None:
        assert line_granularity(line) == expected


# ── settlement model ─────────────────────────────────────────────────────────

TWO_WAY = frozenset({Selection.HOME, Selection.AWAY})
THREE_WAY = frozenset({Selection.HOME, Selection.AWAY, Selection.DRAW})
OVER_UNDER = frozenset({Selection.OVER, Selection.UNDER})


class TestMoneylineSettlementBySport:
    """The push outcome of a moneyline is a property of ``(sport, period)``."""

    @pytest.mark.parametrize(
        "sport",
        [Sport.BASEBALL, Sport.BASKETBALL, Sport.HOCKEY, Sport.TENNIS],
        ids=lambda s: s.value,
    )
    def test_a_full_game_that_cannot_tie_has_two_outcomes(self, sport: Sport) -> None:
        """Extra innings, overtime and the shootout all run until somebody wins,
        and a tennis match cannot end level.  Inventing a push here understates a
        real position's floor as zero for an outcome that cannot occur."""
        outcomes = settlement_outcomes(sport, Market.MONEYLINE, Period.FULL_GAME, None, TWO_WAY)
        assert [label for label, _ in outcomes] == ["away", "home"]

    def test_an_nfl_full_game_two_way_moneyline_has_a_void_outcome(self) -> None:
        """An NFL game still level after overtime is a tie, and US books do not
        price a draw on the two-way moneyline: the bet voids.  Nothing on either
        row mentions it, which is exactly why it lives in PERIOD_RULES."""
        outcomes = settlement_outcomes(
            Sport.FOOTBALL, Market.MONEYLINE, Period.FULL_GAME, None, TWO_WAY
        )
        labels = [label for label, _ in outcomes]
        assert labels == ["away", "home", "push"]
        push = dict(outcomes)["push"]
        assert push == {Selection.HOME: "push", Selection.AWAY: "push"}

    @pytest.mark.parametrize(
        ("sport", "period"),
        [(Sport.SOCCER, Period.FULL_GAME), (Sport.HOCKEY, Period.REGULATION)],
        ids=["soccer_90", "hockey_regulation"],
    )
    def test_a_priced_draw_leaves_nothing_to_void(self, sport: Sport, period: Period) -> None:
        outcomes = settlement_outcomes(sport, Market.MONEYLINE, period, None, THREE_WAY)
        labels = [label for label, _ in outcomes]
        assert set(labels) == {"home", "away", "draw"}
        assert "push" not in labels

    def test_a_two_way_partial_period_moneyline_can_push(self) -> None:
        """Five innings can end level.  A book offering only two selections must
        be voiding the tie, so the tie refunds both legs."""
        outcomes = settlement_outcomes(
            Sport.BASEBALL, Market.MONEYLINE, Period.FIRST_5_INNINGS, None, TWO_WAY
        )
        assert "push" in [label for label, _ in outcomes]

    def test_a_hockey_regulation_two_way_market_can_push(self) -> None:
        """Sixty minutes can end level, so a two-way regulation market voids on a
        tie — the reading the detector refuses to guess at, kept correct here so
        that the refusal is a policy choice rather than a missing branch."""
        outcomes = settlement_outcomes(
            Sport.HOCKEY, Market.MONEYLINE, Period.REGULATION, None, TWO_WAY
        )
        assert "push" in [label for label, _ in outcomes]


class TestLineSettlement:
    def test_half_lines_cannot_push(self) -> None:
        outcomes = settlement_outcomes(
            Sport.BASEBALL, Market.SPREAD, Period.FULL_GAME, -1.5, TWO_WAY
        )
        assert [label for label, _ in outcomes] == ["home_covers", "away_covers"]

    def test_whole_spreads_can_push(self) -> None:
        outcomes = settlement_outcomes(
            Sport.BASEBALL, Market.SPREAD, Period.FULL_GAME, -1.0, TWO_WAY
        )
        assert "push" in [label for label, _ in outcomes]

    def test_whole_totals_can_push(self) -> None:
        outcomes = settlement_outcomes(
            Sport.BASEBALL, Market.TOTAL, Period.FULL_GAME, 9.0, OVER_UNDER
        )
        assert "push" in [label for label, _ in outcomes]

    def test_half_totals_cannot_push(self) -> None:
        outcomes = settlement_outcomes(
            Sport.BASEBALL, Market.TOTAL, Period.FULL_GAME, 8.5, OVER_UNDER
        )
        assert "push" not in [label for label, _ in outcomes]

    def test_a_pick_em_spread_pushes_only_where_a_tie_is_possible(self) -> None:
        """A spread of zero is a draw-no-bet: it pushes exactly when the contest
        can end level.  Baseball cannot, the NFL and soccer can."""
        def labels(sport: Sport) -> list[str]:
            return [
                label
                for label, _ in settlement_outcomes(
                    sport, Market.SPREAD, Period.FULL_GAME, 0.0, TWO_WAY
                )
            ]

        assert "push" not in labels(Sport.BASEBALL)
        assert "push" not in labels(Sport.HOCKEY)
        assert "push" in labels(Sport.FOOTBALL)
        assert "push" in labels(Sport.SOCCER)


class TestQuarterLineSettlement:
    """A quarter line is two half-stake bets, so landing on the whole number
    refunds half the stake instead of all of it."""

    def test_a_quarter_total_splits_the_stake_around_the_whole_number(self) -> None:
        """2.75 is half on over 2.5 and half on over 3.0.  A match ending 2-1
        wins the 2.5 half and refunds the 3.0 half."""
        outcomes = settlement_outcomes(
            Sport.SOCCER, Market.TOTAL, Period.FULL_GAME, 2.75, OVER_UNDER
        )
        labels = [label for label, _ in outcomes]
        assert labels == ["over", "under", "half_push_at_3"]
        landing = dict(outcomes)["half_push_at_3"]
        assert landing[Selection.OVER] == "half_win"
        assert landing[Selection.UNDER] == "half_lose"

    def test_the_quarter_above_the_whole_number_favours_the_under(self) -> None:
        """3.25 is half on 3.0 and half on 3.5.  Landing on 3 refunds the 3.0 half
        and loses the 3.5 half for the over — the mirror image of 2.75, and
        getting the direction wrong hands the refund to the wrong side."""
        outcomes = settlement_outcomes(
            Sport.SOCCER, Market.TOTAL, Period.FULL_GAME, 3.25, OVER_UNDER
        )
        landing = dict(outcomes)["half_push_at_3"]
        assert landing[Selection.UNDER] == "half_win"
        assert landing[Selection.OVER] == "half_lose"

    def test_a_quarter_spread_below_the_whole_number_favours_the_home_side(self) -> None:
        """Home -0.75 is half on -0.5 and half on -1.0; a one-goal home win wins
        the -0.5 half and refunds the -1.0 half."""
        outcomes = settlement_outcomes(
            Sport.SOCCER, Market.SPREAD, Period.FULL_GAME, -0.75, TWO_WAY
        )
        landing = dict(outcomes)["half_push_at_-1"]
        assert landing[Selection.HOME] == "half_win"
        assert landing[Selection.AWAY] == "half_lose"

    def test_a_quarter_spread_above_the_whole_number_favours_the_away_side(self) -> None:
        """Home -1.25 is half on -1.0 and half on -1.5; a one-goal home win
        refunds the -1.0 half and loses the -1.5 half."""
        outcomes = settlement_outcomes(
            Sport.SOCCER, Market.SPREAD, Period.FULL_GAME, -1.25, TWO_WAY
        )
        landing = dict(outcomes)["half_push_at_-1"]
        assert landing[Selection.HOME] == "half_lose"
        assert landing[Selection.AWAY] == "half_win"

    def test_a_quarter_pick_em_still_needs_a_possible_tie(self) -> None:
        """-0.25 straddles 0 and -0.5, so its half push is the draw.  In baseball
        there is no draw to land on."""
        soccer = settlement_outcomes(
            Sport.SOCCER, Market.SPREAD, Period.FULL_GAME, -0.25, TWO_WAY
        )
        baseball = settlement_outcomes(
            Sport.BASEBALL, Market.SPREAD, Period.FULL_GAME, -0.25, TWO_WAY
        )
        assert any(label.startswith("half_push") for label, _ in soccer)
        assert not any(label.startswith("half_push") for label, _ in baseball)


class TestContractShape:
    def test_a_draw_row_only_makes_a_three_way_market_where_draws_are_priced(self) -> None:
        rows = [
            _quote(
                SOCCER_GAME,
                "book_a",
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
            )
            for selection, odds in (
                (Selection.HOME, 2.40),
                (Selection.AWAY, 3.30),
                (Selection.DRAW, 3.40),
            )
        ]
        assert contract_shape(Sport.SOCCER, Market.MONEYLINE, Period.FULL_GAME, rows) == THREE_WAY

    def test_a_hockey_full_game_market_is_two_way_whatever_arrives(self) -> None:
        rows = [
            _quote(NHL_GAME, "book_a", market=Market.MONEYLINE, selection=Selection.HOME),
            _quote(NHL_GAME, "book_a", market=Market.MONEYLINE, selection=Selection.AWAY),
        ]
        assert contract_shape(Sport.HOCKEY, Market.MONEYLINE, Period.FULL_GAME, rows) == TWO_WAY

    def test_a_suspended_leg_does_not_shrink_the_contract(self) -> None:
        rows = [
            _quote(NHL_GAME, "book_a", market=Market.MONEYLINE, selection=Selection.HOME),
            _quote(
                NHL_GAME,
                "book_a",
                market=Market.MONEYLINE,
                selection=Selection.AWAY,
                status=QuoteStatus.SUSPENDED,
            ),
        ]
        assert contract_shape(Sport.HOCKEY, Market.MONEYLINE, Period.FULL_GAME, rows) == TWO_WAY


# ── detection: synthetic two-book slates ─────────────────────────────────────


def _pair(
    *,
    fixture: Fixture = MLB_GAME,
    market: Market = Market.MONEYLINE,
    period: Period = Period.FULL_GAME,
    home_odds: float,
    away_odds: float,
    home_line: float | None = None,
    away_line: float | None = None,
    home_source: str = "book_a",
    away_source: str = "book_b",
    **extra,
) -> list[Quote]:
    """Two synthetic quotes forming one cross-book market.

    Not observed prices: every price here is chosen to sit exactly on one side of
    one threshold.
    """
    over_under = market in (Market.TOTAL, Market.TEAM_TOTAL)
    home = _quote(
        fixture,
        home_source,
        market=market,
        period=period,
        selection=Selection.OVER if over_under else Selection.HOME,
        line=home_line,
        decimal_odds=home_odds,
        source_market_id=f"{home_source}-m",
        **extra,
    )
    away = _quote(
        fixture,
        away_source,
        market=market,
        period=period,
        selection=Selection.UNDER if over_under else Selection.AWAY,
        line=away_line,
        decimal_odds=away_odds,
        source_market_id=f"{away_source}-m",
        **extra,
    )
    return [home, away]


def _three_way_book(
    source: str,
    period: Period,
    home: float,
    away: float,
    draw: float,
    *,
    fixture: Fixture = MLB_GAME,
) -> list[Quote]:
    """One book's three-way market.  Prices must hold a real edge for that book —
    a book whose own three prices sum below 1.0 does not exist, and the detector
    now refuses to draw legs from one."""
    return [
        _quote(
            fixture,
            source,
            market=Market.MONEYLINE,
            period=period,
            selection=selection,
            decimal_odds=odds,
            source_market_id=f"{source}-3way",
        )
        for selection, odds in (
            (Selection.HOME, home),
            (Selection.AWAY, away),
            (Selection.DRAW, draw),
        )
    ]


def _three_way_slate(period: Period, *, fixture: Fixture = MLB_GAME) -> list[Quote]:
    """Two three-way books, each holding a 3.85% edge, that disagree on the
    favourite.  Best-of-each is an arbitrage; neither book is one alone."""
    return [
        *_three_way_book("book_a", period, home=2.60, away=2.00, draw=6.50, fixture=fixture),
        *_three_way_book("book_b", period, home=2.00, away=2.60, draw=6.50, fixture=fixture),
    ]


class TestDetection:
    def test_finds_a_clear_two_way_moneyline_arb(self) -> None:
        """Synthetic: 2.10 both sides. 1/2.1 * 2 = 0.952381 -> 4.76% margin."""
        report = find_opportunities(_pair(home_odds=2.10, away_odds=2.10), total_stake=100.0)
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert opp.margin == pytest.approx(0.047619, abs=1e-6)
        assert opp.roi == pytest.approx(0.05, abs=1e-6)
        assert sorted(opp.sources) == ["book_a", "book_b"]
        assert opp.guaranteed_profit == pytest.approx(5.0, abs=0.01)
        assert opp.is_risk_free
        assert opp.sport is Sport.BASEBALL

    def test_reports_no_arb_on_a_normally_juiced_market(self) -> None:
        report = find_opportunities(_pair(home_odds=1.91, away_odds=1.91))
        assert report.opportunities == []
        assert report.comparable_group_count == 1

    def test_a_market_priced_exactly_fair_is_not_an_arb(self) -> None:
        report = find_opportunities(_pair(home_odds=2.0, away_odds=2.0))
        assert report.opportunities == []

    def test_profit_is_identical_in_every_outcome(self) -> None:
        """The defining property: the payout does not depend on the result."""
        report = find_opportunities(
            _pair(home_odds=2.30, away_odds=2.05), total_stake=1000.0, stake_increment=0.0
        )
        opp = report.opportunities[0]
        profits = [profit for _, profit in opp.outcome_profits]
        assert len(profits) == 2
        assert profits[0] == pytest.approx(profits[1])
        assert opp.guaranteed_profit > 0

    def test_spread_arb_pairs_mirrored_lines_across_books(self) -> None:
        report = find_opportunities(
            _pair(
                market=Market.SPREAD,
                home_odds=2.10,
                away_odds=2.10,
                home_line=-1.5,
                away_line=1.5,
            )
        )
        assert len(report.opportunities) == 1
        assert report.opportunities[0].line == -1.5
        assert not report.opportunities[0].can_push

    def test_mismatched_spreads_produce_nothing(self) -> None:
        """A middle must not surface through the arbitrage interface."""
        report = find_opportunities(
            _pair(
                market=Market.SPREAD,
                home_odds=2.10,
                away_odds=2.10,
                home_line=-1.5,
                away_line=2.5,
            )
        )
        assert report.opportunities == []
        assert report.comparable_group_count == 0

    def test_totals_arb(self) -> None:
        report = find_opportunities(
            _pair(
                market=Market.TOTAL,
                home_odds=2.05,
                away_odds=2.10,
                home_line=8.5,
                away_line=8.5,
            )
        )
        assert len(report.opportunities) == 1
        assert report.opportunities[0].line == 8.5

    def test_suspended_prices_are_not_bettable(self) -> None:
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        quotes[0] = quotes[0].model_copy(update={"status": QuoteStatus.SUSPENDED})
        report = find_opportunities(quotes)
        assert report.opportunities == []

    def test_a_single_book_pricing_both_sides_is_rejected(self) -> None:
        """A negative overround at one book means mispaired prices, not free
        money — validation reports it and the detector must never bet it."""
        report = find_opportunities(
            _pair(home_odds=2.10, away_odds=2.10, home_source="book_a", away_source="book_a")
        )
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["source_prices_itself_to_lose"]

    def test_a_leg_is_never_taken_from_a_book_that_prices_itself_to_lose(self) -> None:
        """book_a's own two prices sum to 0.952, so at least one of them is
        mis-grouped.  Pairing its away price against book_b's home price used to
        report a clean "guaranteed profit" built on a row that validation calls
        an error — the whole market is refused instead."""
        quotes = [
            *_pair(home_odds=2.10, away_odds=2.10, home_source="book_a", away_source="book_a"),
            _quote(
                MLB_GAME,
                "book_b",
                market=Market.MONEYLINE,
                selection=Selection.HOME,
                decimal_odds=2.05,
                source_market_id="book_b-m",
            ),
        ]
        report = find_opportunities(quotes)
        assert report.opportunities == []
        assert "source_prices_itself_to_lose" in [d.code for d in report.diagnostics]

    def test_two_prices_for_one_selection_blocks_the_market(self) -> None:
        """Taking the better of two conflicting prices is betting on the parse
        fault that produced them."""
        quotes = [
            *_pair(home_odds=2.10, away_odds=2.10),
            _quote(
                MLB_GAME,
                "book_a",
                market=Market.MONEYLINE,
                selection=Selection.HOME,
                decimal_odds=1.40,
                source_market_id="book_a-other",
            ),
        ]
        report = find_opportunities(quotes)
        assert report.opportunities == []
        assert "duplicate_selection" in [d.code for d in report.diagnostics]

    def test_legs_that_describe_different_games_are_refused(self) -> None:
        """Both legs backing the same team loses the whole bankroll together, so
        this is guarded here even though reconciliation and validation also catch
        it."""
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        quotes[1] = quotes[1].model_copy(
            update={
                "home_participant": "MLB-PHI",
                "away_participant": "MLB-MIA",
                "home_team": "Philadelphia Phillies",
                "away_team": "Miami Marlins",
            }
        )
        report = find_opportunities(quotes)
        assert report.opportunities == []
        assert "legs_disagree_on_the_game" in [d.code for d in report.diagnostics]

    def test_two_books_spelling_a_club_differently_still_pair(self) -> None:
        """Open-roster identity is the participant key, not the book's spelling:
        "Wolves" and "Wolverhampton" are one club.  Comparing display names here
        refused every legitimate soccer and tennis position."""
        quotes = _pair(fixture=SOCCER_GAME, market=Market.SPREAD,
                       home_odds=2.10, away_odds=2.10, home_line=-0.5, away_line=0.5)
        quotes[1] = quotes[1].model_copy(update={"home_team": "The Arsenal", "away_team": "Chelsea FC"})
        report = find_opportunities(quotes)
        assert len(report.opportunities) == 1

    def test_legs_observed_far_apart_are_rejected(self) -> None:
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        stale = quotes[0].observed_at - timedelta(hours=1)
        quotes[0] = quotes[0].model_copy(update={"observed_at": stale})
        report = find_opportunities(quotes)
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["stale_leg"]

    def test_a_freshness_window_can_be_widened_deliberately(self) -> None:
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        stale = quotes[0].observed_at - timedelta(minutes=10)
        quotes[0] = quotes[0].model_copy(update={"observed_at": stale})
        report = find_opportunities(quotes, max_observation_spread=timedelta(minutes=30))
        assert len(report.opportunities) == 1

    def test_min_margin_filters_thin_edges(self) -> None:
        quotes = _pair(home_odds=2.02, away_odds=2.02)  # margin ~0.99%
        assert find_opportunities(quotes, min_margin=0.0).opportunities
        assert find_opportunities(quotes, min_margin=0.02).opportunities == []

    def test_best_price_is_taken_when_three_books_compete(self) -> None:
        quotes = _pair(home_odds=1.80, away_odds=2.05)
        quotes.append(
            _quote(
                MLB_GAME,
                "book_c",
                market=Market.MONEYLINE,
                selection=Selection.HOME,
                decimal_odds=2.20,
                source_market_id="book_c-m",
            )
        )
        report = find_opportunities(quotes)
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        home_leg = next(leg for leg in opp.legs if leg.selection is Selection.HOME)
        assert home_leg.source == "book_c"
        assert home_leg.decimal_odds == 2.20

    def test_a_mixed_sport_group_is_refused_and_counted(self) -> None:
        """It cannot arise from legitimate data, so if it arises a row's sport is
        mislabelled — and every settlement rule is looked up by sport, so the
        group's risk model is unknowable rather than merely unusual."""
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        quotes[1] = quotes[1].model_copy(update={"sport": Sport.SOCCER})
        report = find_opportunities(quotes)
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["mixed_sport"]


class TestTieSettlementBySport:
    """The same two prices, five different sports, four different answers."""

    def test_an_nfl_two_way_moneyline_is_not_reported_as_guaranteed_money(self) -> None:
        """Synthetic 2.10 / 2.10 on an NFL moneyline.  The headline margin is
        4.76%, but a game still tied after overtime voids both legs and the
        position makes nothing — so the guaranteed profit is zero.  Reporting the
        4.76% as guaranteed is reporting a bet that can return the stake as a bet
        that cannot."""
        report = find_opportunities(
            _pair(fixture=NFL_GAME, home_odds=2.10, away_odds=2.10), total_stake=100.0
        )
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert opp.margin == pytest.approx(0.047619, abs=1e-6)
        assert opp.can_push
        assert opp.guaranteed_profit == pytest.approx(0.0, abs=1e-9)
        assert opp.is_risk_free  # cannot lose, but may make nothing
        assert any("tie voids every leg" in note for note in opp.notes)

    def test_the_same_prices_in_baseball_keep_their_whole_profit(self) -> None:
        """The contrast that shows the difference comes from the sport and not
        from the prices: nine innings cannot end level, so there is no void
        outcome to survive."""
        report = find_opportunities(
            _pair(fixture=MLB_GAME, home_odds=2.10, away_odds=2.10), total_stake=100.0
        )
        opp = report.opportunities[0]
        assert not opp.can_push
        assert opp.guaranteed_profit == pytest.approx(5.0, abs=0.01)

    @pytest.mark.parametrize(
        "fixture", [MLB_GAME, NHL_GAME, WNBA_GAME, TENNIS_MATCH], ids=lambda f: f.league
    )
    def test_a_full_game_that_cannot_tie_has_no_push(self, fixture: Fixture) -> None:
        report = find_opportunities(
            _pair(fixture=fixture, home_odds=2.10, away_odds=2.10), total_stake=100.0
        )
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert not opp.can_push
        assert opp.guaranteed_profit == pytest.approx(5.0, abs=0.01)

    def test_a_two_way_soccer_moneyline_is_refused_outright(self) -> None:
        """A 90-minute soccer market with two selections is missing its draw leg.
        Either the book voids the draw (floor zero) or the draw leg was dropped on
        the way in and a draw *loses both legs* (floor minus the whole bankroll).
        The two readings differ by 100% of the stake and nothing on the row says
        which, so nothing is reported."""
        report = find_opportunities(
            _pair(fixture=SOCCER_GAME, home_odds=2.10, away_odds=2.10)
        )
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["ambiguous_tie_settlement"]

    def test_a_two_way_hockey_regulation_moneyline_is_refused_outright(self) -> None:
        """The same refusal one period-definition away from a market that is
        perfectly fine: hockey regulation is priced three-way."""
        report = find_opportunities(
            _pair(fixture=NHL_GAME, period=Period.REGULATION, home_odds=2.10, away_odds=2.10)
        )
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["ambiguous_tie_settlement"]

    def test_a_two_way_hockey_full_game_moneyline_is_fine(self) -> None:
        """The shootout decides it, so two legs really are the whole contract."""
        report = find_opportunities(
            _pair(fixture=NHL_GAME, period=Period.FULL_GAME, home_odds=2.10, away_odds=2.10)
        )
        assert len(report.opportunities) == 1
        assert report.opportunities[0].guaranteed_profit == pytest.approx(5.0, abs=0.01)

    def test_a_three_way_soccer_arb_covers_all_three_outcomes(self) -> None:
        """Synthetic: two books each holding a 3.85% edge that disagree on the
        favourite.  1/2.60 + 1/2.60 + 1/6.50 = 0.9231, a 7.69% margin, and the
        draw is a covered leg rather than a risk."""
        report = find_opportunities(_three_way_slate(Period.FULL_GAME, fixture=SOCCER_GAME))
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert len(opp.legs) == 3
        assert {leg.selection for leg in opp.legs} == {
            Selection.HOME,
            Selection.AWAY,
            Selection.DRAW,
        }
        assert not opp.can_push
        assert opp.margin == pytest.approx(1 - (1 / 2.60 + 1 / 2.60 + 1 / 6.50), abs=1e-9)
        assert any("three-way market" in note for note in opp.notes)

    def test_a_three_way_hockey_regulation_arb_is_fine(self) -> None:
        report = find_opportunities(
            _three_way_slate(Period.REGULATION, fixture=NHL_GAME)
        )
        assert len(report.opportunities) == 1
        assert len(report.opportunities[0].legs) == 3

    def test_hockey_full_game_and_regulation_are_never_paired(self) -> None:
        """Different contracts: one includes the shootout and cannot tie, the
        other excludes it and is three-way.  Pairing them looks like a huge edge
        on two fair prices — here 1/2.10 + 1/2.10 = 0.952 — and it is not one."""
        quotes = [
            _quote(
                NHL_GAME,
                "book_a",
                market=Market.MONEYLINE,
                period=Period.FULL_GAME,
                selection=Selection.HOME,
                decimal_odds=2.10,
                source_market_id="book_a-fg",
            ),
            _quote(
                NHL_GAME,
                "book_b",
                market=Market.MONEYLINE,
                period=Period.REGULATION,
                selection=Selection.AWAY,
                decimal_odds=2.10,
                source_market_id="book_b-reg",
            ),
        ]
        report = find_opportunities(quotes)
        assert report.opportunities == []
        assert report.comparable_group_count == 0

    def test_a_two_way_and_a_three_way_book_are_never_combined(self) -> None:
        """Backing home at a two-way book and away at a three-way book leaves
        the draw uncovered: a level score loses one leg and voids the other."""
        quotes = [
            *(
                _quote(
                    MLB_GAME,
                    "two_way",
                    market=Market.MONEYLINE,
                    period=Period.FIRST_5_INNINGS,
                    selection=selection,
                    decimal_odds=odds,
                    source_market_id="two_way-m",
                )
                # 1/2.30 + 1/1.60 = 1.060: a normal two-way market.
                for selection, odds in ((Selection.HOME, 2.30), (Selection.AWAY, 1.60))
            ),
            *_three_way_book("three_way", Period.FIRST_5_INNINGS, 2.60, 2.00, 6.50),
        ]
        report = find_opportunities(quotes)
        assert "settlement_shape_mismatch" in [d.code for d in report.diagnostics]
        # Neither contract has two books of its own, so nothing is combinable.
        assert report.opportunities == []

    def test_covering_two_of_three_outcomes_is_not_an_arb(self) -> None:
        """Two legs of a three-way market at 2.10 each sum to 0.952 — which the
        naive test would call a 4.8% arbitrage while the draw is uncovered."""
        quotes = []
        for source in ("book_a", "book_b"):
            for selection, odds in (
                (Selection.HOME, 2.10),
                (Selection.AWAY, 2.10),
                (Selection.DRAW, 15.0),
            ):
                quotes.append(
                    _quote(
                        SOCCER_GAME,
                        source,
                        market=Market.MONEYLINE,
                        selection=selection,
                        decimal_odds=odds,
                        source_market_id=f"{source}-m",
                    )
                )
        report = find_opportunities(quotes)
        # 1/2.1 + 1/2.1 + 1/15 = 0.952381 + 0.066667 = 1.019 -> no arb.
        assert report.opportunities == []
        # And every leg set considered covered all three outcomes.
        assert report.comparable_group_count == 1


class TestPushRisk:
    def test_whole_number_total_reports_a_zero_floor(self) -> None:
        """Synthetic: over/under 9 at 2.10 each looks like a 4.76% arb, but the
        game landing on exactly 9 refunds both legs."""
        report = find_opportunities(
            _pair(
                market=Market.TOTAL,
                home_odds=2.10,
                away_odds=2.10,
                home_line=9.0,
                away_line=9.0,
            ),
            total_stake=100.0,
        )
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert opp.margin == pytest.approx(0.047619, abs=1e-6)
        assert opp.can_push
        assert opp.guaranteed_profit == pytest.approx(0.0, abs=1e-9)
        assert opp.is_risk_free  # cannot lose, but may make nothing
        assert any("whole number" in note for note in opp.notes)

    def test_whole_number_spread_reports_a_zero_floor(self) -> None:
        report = find_opportunities(
            _pair(
                market=Market.SPREAD,
                home_odds=2.10,
                away_odds=2.10,
                home_line=-1.0,
                away_line=1.0,
            )
        )
        opp = report.opportunities[0]
        assert opp.can_push
        assert opp.guaranteed_profit == pytest.approx(0.0, abs=1e-9)

    def test_a_two_way_partial_period_moneyline_is_refused_outright(self) -> None:
        """A first-five-innings market with only two selections is ambiguous: a
        tie either voids both legs (floor zero) or loses both (floor minus the
        whole bankroll), and adapters drop an unpriced draw leg silently, so the
        row cannot say which.  The two readings differ by 100% of the stake, so
        nothing is reported."""
        report = find_opportunities(
            _pair(period=Period.FIRST_5_INNINGS, home_odds=2.10, away_odds=2.10)
        )
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["ambiguous_tie_settlement"]

    def test_a_three_way_partial_period_moneyline_is_fine(self) -> None:
        """With the draw priced there is no ambiguity left to worry about."""
        report = find_opportunities(_three_way_slate(Period.FIRST_5_INNINGS))
        assert len(report.opportunities) == 1
        assert not report.opportunities[0].can_push

    def test_half_line_total_keeps_its_full_profit(self) -> None:
        report = find_opportunities(
            _pair(
                market=Market.TOTAL,
                home_odds=2.10,
                away_odds=2.10,
                home_line=8.5,
                away_line=8.5,
            ),
            total_stake=100.0,
        )
        opp = report.opportunities[0]
        assert not opp.can_push
        assert opp.guaranteed_profit == pytest.approx(5.0, abs=0.01)


class TestQuarterLineDetection:
    def test_a_quarter_total_arb_is_settled_at_its_half_push(self) -> None:
        """Synthetic, hand-computed.  Over/under 2.75 at 2.10 each, 100 staked
        50/50.  Over 3+ goals: 50*2.10 = 105 against 100 staked, so +5.  Under 2:
        +5.  Exactly 3 goals: the over's 2.5 half wins and its 3.0 half refunds,
        returning 25*2.10 + 25 = 77.50, while the under's 3.0 half refunds and its
        2.5 half loses, returning 25.  Total 102.50, so +2.50.  The floor is that
        2.50 — not the 5 a half-line market would pay, and not the zero a
        whole-line market would."""
        report = find_opportunities(
            _pair(
                fixture=SOCCER_GAME,
                market=Market.TOTAL,
                home_odds=2.10,
                away_odds=2.10,
                home_line=2.75,
                away_line=2.75,
            ),
            total_stake=100.0,
        )
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert not opp.can_push
        assert opp.can_half_push
        assert dict(opp.outcome_profits)["half_push_at_3"] == pytest.approx(2.50, abs=1e-9)
        assert opp.guaranteed_profit == pytest.approx(2.50, abs=1e-9)
        assert opp.is_risk_free
        assert any("quarter line" in note for note in opp.notes)

    def test_the_half_push_pays_exactly_half_of_the_winning_half(self) -> None:
        """The identity worth pinning, because it is what makes a quarter line
        safe to report at all: with stake ``s`` on the side whose half survives,
        the half push returns ``(s*d + s)/2 + (T - s)/2 = (s*d + T)/2``, so its
        profit is exactly half the profit of that side's own outcome.  It is
        therefore a *reduction* of the edge and never a reversal of it — unlike a
        whole-line push, which refunds everything and zeroes the position."""
        report = find_opportunities(
            _pair(
                fixture=SOCCER_GAME,
                market=Market.TOTAL,
                home_odds=2.30,
                away_odds=2.05,
                home_line=2.75,
                away_line=2.75,
            ),
            total_stake=1000.0,
            stake_increment=0.0,
        )
        profits = dict(report.opportunities[0].outcome_profits)
        assert profits["half_push_at_3"] == pytest.approx(profits["over"] / 2.0, abs=1e-9)
        assert report.opportunities[0].guaranteed_profit == pytest.approx(
            min(profits["under"], profits["over"] / 2.0), abs=1e-9
        )

    @pytest.mark.parametrize(
        ("over_odds", "under_odds", "total"),
        [(2.10, 2.10, 100.0), (2.30, 2.05, 100.0), (2.60, 1.65, 10_000.0), (6.19, 1.20, 200.0)],
    )
    def test_a_quarter_line_reduces_the_floor_without_destroying_it(
        self, over_odds: float, under_odds: float, total: float
    ) -> None:
        """Stated as a property over several synthetic price pairs: whatever a
        half-line version of the position guarantees, the quarter-line version
        guarantees something smaller and still positive.  A test that only checked
        one price pair could not tell that apart from a coincidence."""
        half = find_opportunities(
            _pair(
                fixture=SOCCER_GAME,
                market=Market.TOTAL,
                home_odds=over_odds,
                away_odds=under_odds,
                home_line=2.5,
                away_line=2.5,
            ),
            total_stake=total,
        )
        quarter = find_opportunities(
            _pair(
                fixture=SOCCER_GAME,
                market=Market.TOTAL,
                home_odds=over_odds,
                away_odds=under_odds,
                home_line=2.75,
                away_line=2.75,
            ),
            total_stake=total,
        )
        assert len(half.opportunities) == len(quarter.opportunities) == 1
        assert quarter.opportunities[0].guaranteed_profit > 0
        assert (
            quarter.opportunities[0].guaranteed_profit
            <= half.opportunities[0].guaranteed_profit + 1e-9
        )

    def test_a_quarter_spread_arb_favours_the_side_that_half_wins(self) -> None:
        """Home -0.75 at 2.10 against away +0.75 at 2.10.  A one-goal home win
        half-wins the home leg: 25*2.10 + 25 = 77.50 plus the away leg's refunded
        half, 25, is 102.50."""
        report = find_opportunities(
            _pair(
                fixture=SOCCER_GAME,
                market=Market.SPREAD,
                home_odds=2.10,
                away_odds=2.10,
                home_line=-0.75,
                away_line=0.75,
            ),
            total_stake=100.0,
        )
        opp = report.opportunities[0]
        assert opp.line == -0.75
        assert dict(opp.outcome_profits)["half_push_at_-1"] == pytest.approx(2.50, abs=1e-9)

    def test_an_unmodelled_line_granularity_is_refused_and_counted(self) -> None:
        """Silently settling a line whose granularity is unknown means assuming it
        can never land on its own number, which is exactly the assumption that
        turns a mis-settled market into a reported guarantee."""
        report = find_opportunities(
            _pair(
                market=Market.TOTAL,
                home_odds=2.10,
                away_odds=2.10,
                home_line=8.3,
                away_line=8.3,
            )
        )
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["unsupported_line_granularity"]


class TestImplausibleMargins:
    def test_a_5_percent_edge_is_reported_with_a_warning_note(self) -> None:
        report = find_opportunities(_pair(home_odds=2.30, away_odds=2.30), total_stake=100.0)
        opp = report.opportunities[0]
        assert opp.margin > IMPLAUSIBLE_MARGIN
        assert any("implausibly large" in note for note in opp.notes)

    def test_an_inverted_tennis_price_is_refused_rather_than_reported(self) -> None:
        """Modelled on the captured tennis slate, where two books' home/away price
        assignment for one match is inverted: both then price the same player as
        the underdog, and the two "complementary" legs really back the same
        competitor.  Synthetic here — 1.217 and 4.320 are the shape of that
        failure — and the point is that a 54% edge is refused and counted rather
        than reported with a note, because a note on the top-ranked line of the
        report is not a defence."""
        report = find_opportunities(
            _pair(fixture=TENNIS_MATCH, home_odds=4.320, away_odds=4.400), total_stake=100.0
        )
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["margin_implausibly_large"]
        assert "wrong participant" in report.diagnostics[0].detail

    def test_the_refusal_threshold_leaves_real_edges_alone(self) -> None:
        """A genuine cross-book edge is fractions of a percent to low single
        digits, so the bar sits far above anything real."""
        assert REFUSE_MARGIN > IMPLAUSIBLE_MARGIN
        report = find_opportunities(_pair(home_odds=2.20, away_odds=2.20))
        assert report.opportunities[0].margin < REFUSE_MARGIN
        assert len(report.opportunities) == 1


class TestStakeRoundingAndLimits:
    def test_stakes_are_whole_units_and_spend_the_whole_bankroll(self) -> None:
        report = find_opportunities(
            _pair(home_odds=2.37, away_odds=2.11), total_stake=100.0, stake_increment=1.0
        )
        opp = report.opportunities[0]
        assert all(leg.stake == pytest.approx(round(leg.stake)) for leg in opp.legs)
        assert sum(leg.stake for leg in opp.legs) == pytest.approx(100.0)

    def test_rounding_never_overstates_the_guarantee(self) -> None:
        report = find_opportunities(
            _pair(home_odds=2.37, away_odds=2.11), total_stake=100.0, stake_increment=1.0
        )
        opp = report.opportunities[0]
        ideal = opp.roi * opp.total_stake
        assert opp.guaranteed_profit <= ideal + 1e-9

    def test_an_edge_thinner_than_one_betting_unit_is_rejected(self) -> None:
        """Synthetic, hand-computed: 2.60 / 1.65 is a 0.93% edge.  On a 20-unit
        bankroll the ideal split is 7.77 / 12.23; whole units force 8 / 12, and
        the away leg then returns 12 * 1.65 = 19.80 against 20 staked.  The
        headline margin is still positive, so this must be caught here."""
        report = find_opportunities(
            _pair(home_odds=2.60, away_odds=1.65), total_stake=20.0, stake_increment=1.0
        )
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["rounding_destroys_edge"]

    def test_the_same_thin_edge_survives_a_larger_bankroll(self) -> None:
        """Ideal split of 10,000 is 3882.36 / 6117.64; the spare unit goes to the
        larger remainder, giving 3882 / 6118.  Home returns 3882 * 2.6 =
        10,093.20 and away 6118 * 1.65 = 10,094.70, so the floor is +93.20."""
        report = find_opportunities(
            _pair(home_odds=2.60, away_odds=1.65), total_stake=10_000.0, stake_increment=1.0
        )
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert sorted(leg.stake for leg in opp.legs) == [3882.0, 6118.0]
        assert opp.guaranteed_profit == pytest.approx(93.20, abs=0.01)

    def test_stated_limits_cap_the_bankroll(self) -> None:
        """Both books cap at 50. Equal odds means an equal split, so the whole
        position is capped at 100."""
        report = find_opportunities(
            _pair(home_odds=2.10, away_odds=2.10, limit_amount=50.0), total_stake=100.0
        )
        opp = report.opportunities[0]
        assert opp.max_total_stake == pytest.approx(100.0)

    def test_an_uneven_limit_binds_on_the_smaller_leg(self) -> None:
        quotes = _pair(home_odds=3.0, away_odds=2.0)
        # 1/3 + 1/2 = 0.8333. Home takes 0.3333/0.8333 = 40% of the bankroll.
        # A 20-unit cap on that leg therefore caps the bankroll at 50.
        quotes[0] = quotes[0].model_copy(update={"limit_amount": 20.0})
        quotes[1] = quotes[1].model_copy(update={"limit_amount": 1000.0})
        report = find_opportunities(quotes, total_stake=100.0)
        opp = report.opportunities[0]
        assert opp.max_total_stake == pytest.approx(50.0, rel=1e-6)

    def test_an_unstated_limit_is_unknown_rather_than_unlimited(self) -> None:
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        quotes[0] = quotes[0].model_copy(update={"limit_amount": 50.0})
        report = find_opportunities(quotes)
        assert report.opportunities[0].max_total_stake is None


class TestStakeAllocation:
    """Rounding chooses by profit floor, and never spends more than the bankroll."""

    def test_the_split_that_preserves_the_edge_is_chosen(self) -> None:
        """Hand-computed: 6.19 / 1.20 is a 0.51% edge.  Ideal stakes on a 200
        bankroll are 32.16 / 167.84.  Giving the spare unit to the *larger*
        remainder — which is what minimising rounding error does — yields 32/168
        and a floor of 32 * 6.19 - 200 = -1.92.  The other whole-unit split,
        33/167, floors at 33 * 6.19 - 200 = +4.27 and 167 * 1.20 - 200 = +0.40.
        The second is a real risk-free position and must be the one reported."""
        report = find_opportunities(
            _pair(home_odds=6.19, away_odds=1.20), total_stake=200.0, stake_increment=1.0
        )
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert sorted(leg.stake for leg in opp.legs) == [33.0, 167.0]
        assert opp.guaranteed_profit == pytest.approx(0.40, abs=0.01)
        assert opp.is_risk_free

    @pytest.mark.parametrize(
        ("total", "increment"),
        [(100.0, 1.0), (100.0, 3.0), (100.0, 6.0), (250.0, 20.0), (270.0, 20.0), (5.0, 3.0)],
    )
    def test_the_position_never_exceeds_the_bankroll(
        self, total: float, increment: float
    ) -> None:
        """An increment that does not divide the bankroll must round the position
        *down*.  Rounding the unit count up overspends — 50/50 of 100 in units of
        6 becomes 54/48, which is 102."""
        candidates = stake_candidates(stake_split([2.0, 2.0], total), total, increment)
        for stakes in candidates:
            assert sum(stakes) <= total + 1e-9, f"{stakes} exceeds {total}"

    def test_stakes_are_always_whole_multiples_of_the_increment(self) -> None:
        candidates = stake_candidates(stake_split([2.37, 2.11], 100.0), 100.0, 5.0)
        for stakes in candidates:
            for stake in stakes:
                assert abs(stake / 5.0 - round(stake / 5.0)) < 1e-9

    def test_a_zero_increment_leaves_the_ideal_split_alone(self) -> None:
        ideal = stake_split([2.10, 2.05], 1000.0)
        assert stake_candidates(ideal, 1000.0, 0.0) == [ideal]


class TestLimits:
    def test_the_capped_bankroll_lands_on_whole_units(self) -> None:
        """The cap is derived from ideal stakes, so staking it and then rounding
        would push the binding leg a fraction past the book's stated maximum — and
        a leg the book rejects is a leg that is not on."""
        quotes = _pair(home_odds=3.0, away_odds=2.0)
        quotes[0] = quotes[0].model_copy(update={"limit_amount": 47.0})
        quotes[1] = quotes[1].model_copy(update={"limit_amount": 60.0})
        report = find_opportunities(quotes, total_stake=100.0, stake_increment=1.0)
        cap = report.opportunities[0].max_total_stake
        assert cap == pytest.approx(round(cap))

    def test_no_leg_exceeds_its_limit_at_the_capped_bankroll(self) -> None:
        quotes = _pair(home_odds=3.0, away_odds=2.0)
        limits = {Selection.HOME: 20.0, Selection.AWAY: 1000.0}
        quotes = [q.model_copy(update={"limit_amount": limits[q.selection]}) for q in quotes]
        report = find_opportunities(quotes, total_stake=100.0, stake_increment=1.0)
        opp = report.opportunities[0]
        cap = opp.max_total_stake
        assert cap is not None
        restaked = find_opportunities(quotes, total_stake=cap, stake_increment=1.0)
        for leg in restaked.opportunities[0].legs:
            assert leg.stake <= leg.quote.limit_amount + 1e-9

    def test_a_stated_limit_of_zero_is_not_reported_as_no_limit(self) -> None:
        """"This book will not take the bet" and "no limit is stated" are
        different facts and must not share a representation."""
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        quotes[0] = quotes[0].model_copy(update={"limit_amount": 0.0})
        quotes[1] = quotes[1].model_copy(update={"limit_amount": 500.0})
        opp = find_opportunities(quotes).opportunities[0]
        assert opp.max_total_stake == 0.0
        assert any("limit of zero" in note for note in opp.notes)


class TestPickEmSpread:
    def test_a_full_game_baseball_spread_at_zero_cannot_push(self) -> None:
        """Extra innings are played until someone wins, so a nine-inning game
        cannot end level and a pick'em run line has no push outcome.  Treating it
        as pushable reported a floor of zero for an impossible outcome."""
        report = find_opportunities(
            _pair(
                market=Market.SPREAD,
                home_odds=2.10,
                away_odds=2.10,
                home_line=0.0,
                away_line=0.0,
            ),
            total_stake=100.0,
        )
        opp = report.opportunities[0]
        assert not opp.can_push
        assert opp.guaranteed_profit == pytest.approx(5.0, abs=0.01)

    def test_a_full_game_nfl_spread_at_zero_can_push(self) -> None:
        """The same market in a sport that can tie: an NFL pick'em refunds on a
        tie, so the floor is zero.  The line is identical; only the sport
        differs."""
        report = find_opportunities(
            _pair(
                fixture=NFL_GAME,
                market=Market.SPREAD,
                home_odds=2.10,
                away_odds=2.10,
                home_line=0.0,
                away_line=0.0,
            ),
            total_stake=100.0,
        )
        opp = report.opportunities[0]
        assert opp.can_push
        assert opp.guaranteed_profit == pytest.approx(0.0, abs=1e-9)

    def test_a_soccer_draw_no_bet_can_push(self) -> None:
        report = find_opportunities(
            _pair(
                fixture=SOCCER_GAME,
                market=Market.SPREAD,
                home_odds=2.10,
                away_odds=2.10,
                home_line=0.0,
                away_line=0.0,
            ),
            total_stake=100.0,
        )
        assert report.opportunities[0].can_push

    def test_a_partial_period_baseball_spread_at_zero_can_push(self) -> None:
        """Five innings really can end level, so there the push is real."""
        report = find_opportunities(
            _pair(
                market=Market.SPREAD,
                period=Period.FIRST_5_INNINGS,
                home_odds=2.10,
                away_odds=2.10,
                home_line=0.0,
                away_line=0.0,
            )
        )
        opp = report.opportunities[0]
        assert opp.can_push
        assert opp.guaranteed_profit == pytest.approx(0.0, abs=1e-9)


class TestAsOf:
    def test_a_game_that_has_already_started_is_not_reported(self) -> None:
        """Re-analysing a stored run yields legs that agree with each other
        perfectly and describe games that have already been played."""
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        after_first_pitch = quotes[0].commence_time + timedelta(hours=1)
        assert find_opportunities(quotes, as_of=after_first_pitch).opportunities == []

    def test_a_future_game_is_reported(self) -> None:
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        before = quotes[0].commence_time - timedelta(hours=1)
        assert len(find_opportunities(quotes, as_of=before).opportunities) == 1

    def test_without_a_clock_no_gate_is_applied(self) -> None:
        """A historical study of the stored data wants every position, settled
        or not."""
        quotes = _pair(home_odds=2.10, away_odds=2.10)
        assert len(find_opportunities(quotes).opportunities) == 1


class TestOrdering:
    def test_opportunities_are_ranked_by_guaranteed_money(self) -> None:
        """A pushable market can show a 9% margin against a floor of zero.
        Ranking by margin would list it above a 2% position that really pays."""
        pushable = _pair(
            market=Market.TOTAL,
            home_odds=2.20,
            away_odds=2.20,
            home_line=9.0,
            away_line=9.0,
        )
        real = [
            q.model_copy(update={"event_key": "MLB-AAA@MLB-BBB:2026-07-28"})
            for q in _pair(home_odds=2.05, away_odds=2.05)
        ]
        report = find_opportunities([*pushable, *real], total_stake=100.0)
        assert len(report.opportunities) == 2
        first, second = report.opportunities
        assert first.guaranteed_profit > second.guaranteed_profit
        assert first.margin < second.margin, "the higher margin must not win"
        assert second.can_push

    def test_an_nfl_moneyline_ranks_below_a_smaller_baseball_edge(self) -> None:
        """The same ranking property, driven by a sport fact rather than a line:
        the NFL position's floor is zero because a tie voids it."""
        nfl = _pair(fixture=NFL_GAME, home_odds=2.20, away_odds=2.20)
        mlb = _pair(fixture=MLB_GAME, home_odds=2.05, away_odds=2.05)
        report = find_opportunities([*nfl, *mlb], total_stake=100.0)
        assert len(report.opportunities) == 2
        first, second = report.opportunities
        assert first.sport is Sport.BASEBALL
        assert second.sport is Sport.FOOTBALL
        assert first.margin < second.margin


class TestInputValidation:
    @pytest.mark.parametrize("bad", [[], [1.0, 2.0], [2.0, 0.5], [2.0, -1.0]])
    def test_roi_rejects_what_margin_rejects(self, bad: list[float]) -> None:
        """The two must agree on what a price is, or one silently returns a
        number for input the other refuses."""
        with pytest.raises(ValueError):
            arb_roi(bad)
        with pytest.raises(ValueError):
            arb_margin(bad)

    @pytest.mark.parametrize("bad", [[], [1.0, 2.0], [2.0, 0.5]])
    def test_stake_split_rejects_non_prices(self, bad: list[float]) -> None:
        with pytest.raises(ValueError):
            stake_split(bad, 100.0)


class TestReportShape:
    def test_summary_distinguishes_no_data_from_no_edge(self) -> None:
        """"No opportunities" is only meaningful next to how many markets were
        actually comparable across books."""
        nothing = find_opportunities([])
        assert nothing.comparable_group_count == 0
        one = find_opportunities(_pair(home_odds=1.91, away_odds=1.91))
        assert one.comparable_group_count == 1
        assert "1 cross-book markets" in one.summary()

    def test_every_refusal_carries_a_sport_and_an_explanation(self) -> None:
        """A refusal that cannot be read is indistinguishable from a silent drop,
        and with six sports in one run the code alone does not say where to look."""
        quotes = [
            *_pair(fixture=SOCCER_GAME, home_odds=2.10, away_odds=2.10),
            *_pair(
                fixture=MLB_GAME,
                market=Market.TOTAL,
                home_odds=2.10,
                away_odds=2.10,
                home_line=8.3,
                away_line=8.3,
            ),
        ]
        report = find_opportunities(quotes)
        assert {d.code for d in report.diagnostics} == {
            "ambiguous_tie_settlement",
            "unsupported_line_granularity",
        }
        for diagnostic in report.diagnostics:
            assert diagnostic.sport is not None
            assert len(diagnostic.detail) > 40

    def test_opportunities_are_ordered_by_margin(self) -> None:
        quotes = [
            *_pair(home_odds=2.05, away_odds=2.05),
            *[
                q.model_copy(update={"event_key": "MLB-AAA@MLB-BBB:2026-07-28"})
                for q in _pair(home_odds=2.20, away_odds=2.20)
            ],
        ]
        report = find_opportunities(quotes)
        margins = [opp.margin for opp in report.opportunities]
        assert margins == sorted(margins, reverse=True)

    def test_describe_renders_without_error(self) -> None:
        report = find_opportunities(_pair(home_odds=2.10, away_odds=2.10))
        text = report.opportunities[0].describe()
        assert "book_a" in text and "book_b" in text
        assert "outcomes:" in text
        assert "baseball" in text

    def test_describe_renders_a_three_way_soccer_position(self) -> None:
        report = find_opportunities(_three_way_slate(Period.FULL_GAME, fixture=SOCCER_GAME))
        text = report.opportunities[0].describe()
        assert "soccer" in text
        assert "draw" in text


# ── the real captured slate ──────────────────────────────────────────────────


class TestRealFixtures:
    """Observed prices, from the captured multi-sport run.

    Nothing here is expected to be an arbitrage.  Three correctly-priced books do
    not leave free money lying on a liquid market, so zero is the *expected*
    answer and a non-zero one means the detector has invented an edge out of a
    pairing error.
    """

    def test_the_captured_slate_contains_no_arbitrage(self, all_fixture_quotes) -> None:
        report = find_opportunities(all_fixture_quotes)
        assert report.opportunities == [], "\n".join(
            opp.describe() for opp in report.opportunities
        )

    def test_the_captured_slate_is_actually_comparable_across_books(
        self, all_fixture_quotes
    ) -> None:
        """Guards the test above: "no arbitrage" would be trivially true if
        nothing were being compared at all."""
        report = find_opportunities(all_fixture_quotes)
        assert report.comparable_group_count >= 100

    def test_more_than_one_sport_is_cross_book(self, all_fixture_quotes) -> None:
        """A multi-sport claim needs multi-sport evidence: a run that only ever
        compares baseball would satisfy every other assertion here."""
        cross_book = {
            quote.sport
            for selections in best_prices(all_fixture_quotes).values()
            if len({q.source for q in selections.values()}) > 1
            for quote in selections.values()
        }
        assert len(cross_book) >= 4, sorted(s.value for s in cross_book)

    def test_every_refusal_on_real_data_is_one_of_the_known_reasons(
        self, all_fixture_quotes
    ) -> None:
        """Every dropped market has a stated reason, so "no opportunities" can be
        read as a result rather than as silence."""
        report = find_opportunities(all_fixture_quotes)
        known = {
            "ambiguous_tie_settlement",
            "duplicate_selection",
            "legs_disagree_on_the_game",
            "margin_implausibly_large",
            "mixed_sport",
            "rounding_destroys_edge",
            "settlement_shape_mismatch",
            "source_prices_itself_to_lose",
            "stale_leg",
            "unsupported_line_granularity",
        }
        assert {d.code for d in report.diagnostics} <= known

    def test_every_cross_book_pair_is_line_consistent(self, all_fixture_quotes) -> None:
        """Whatever gets grouped together must agree on the market it is."""
        for key, selections in best_prices(all_fixture_quotes).items():
            _, market, _, _, line = key
            for quote in selections.values():
                assert canonical_line(quote) == line
                assert quote.market is market

    def test_no_group_mixes_events_periods_or_sports(self, all_fixture_quotes) -> None:
        for key, selections in best_prices(all_fixture_quotes).items():
            event_key, market, period, side, _ = key
            sports = {quote.sport for quote in selections.values()}
            assert len(sports) == 1
            for quote in selections.values():
                assert quote.event_key == event_key
                assert quote.period is period
                assert quote.side == side

    def test_real_quarter_lines_are_present_and_settled(self, all_fixture_quotes) -> None:
        """Quarter lines are not hypothetical: the captured slate carries them, so
        refusing to model them would have discarded real markets, and settling
        them as half lines would have mis-stated every one of their floors."""
        quarters = [
            quote
            for quote in all_fixture_quotes
            if quote.line is not None and line_granularity(quote.line) == LINE_QUARTER
        ]
        assert quarters
        for quote in quarters[:50]:
            outcomes = settlement_outcomes(
                quote.sport,
                quote.market,
                quote.period,
                canonical_line(quote),
                frozenset({quote.selection}),
            )
            assert any(label.startswith("half_push") for label, _ in outcomes)
