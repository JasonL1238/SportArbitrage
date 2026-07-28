"""Tests for arbitrage detection.

The captured slate contains no arbitrage — three correctly-priced books rarely
produce one — so the positive cases here are built from **synthetic quotes**
constructed to hit specific traps.  They are labelled as synthetic throughout
and are never presented as observed prices; the real captured fixtures are
exercised separately at the bottom of this file, where the expected answer is
"no opportunities".

Every expected number below is derived by hand in the test itself, so a change
in the engine's arithmetic fails here rather than silently changing what gets
reported as risk-free money.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.arb import (
    ArbLeg,
    Opportunity,
    arb_margin,
    arb_roi,
    best_prices,
    canonical_line,
    find_opportunities,
    group_key,
    settlement_outcomes,
    stake_candidates,
    stake_split,
)
from src.schema import Market, Period, QuoteStatus, Selection, Side
from tests.conftest import make_quote

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


# ── line orientation ─────────────────────────────────────────────────────────


class TestCanonicalLine:
    def test_run_line_is_expressed_from_the_home_perspective(self) -> None:
        home = make_quote(market=Market.RUN_LINE, selection=Selection.HOME, line=-1.5)
        away = make_quote(market=Market.RUN_LINE, selection=Selection.AWAY, line=1.5)
        assert canonical_line(home) == -1.5
        assert canonical_line(away) == -1.5

    def test_mirrored_run_lines_land_in_one_group(self) -> None:
        home = make_quote(market=Market.RUN_LINE, selection=Selection.HOME, line=-1.5)
        away = make_quote(market=Market.RUN_LINE, selection=Selection.AWAY, line=1.5)
        assert group_key(home) == group_key(away)

    def test_unmirrored_run_lines_stay_apart(self) -> None:
        """Home -1.5 against away +2.5 is a middle, not an arbitrage, and the
        two must never be grouped as opposite halves of one market."""
        home = make_quote(market=Market.RUN_LINE, selection=Selection.HOME, line=-1.5)
        away = make_quote(market=Market.RUN_LINE, selection=Selection.AWAY, line=2.5)
        assert group_key(home) != group_key(away)

    def test_totals_share_their_line_directly(self) -> None:
        over = make_quote(market=Market.TOTAL_RUNS, selection=Selection.OVER, line=8.5)
        under = make_quote(market=Market.TOTAL_RUNS, selection=Selection.UNDER, line=8.5)
        assert canonical_line(over) == canonical_line(under) == 8.5
        assert group_key(over) == group_key(under)

    def test_different_totals_stay_apart(self) -> None:
        over = make_quote(market=Market.TOTAL_RUNS, selection=Selection.OVER, line=8.5)
        under = make_quote(market=Market.TOTAL_RUNS, selection=Selection.UNDER, line=9.5)
        assert group_key(over) != group_key(under)

    def test_team_totals_are_separated_by_side(self) -> None:
        home_side = make_quote(
            market=Market.TEAM_TOTAL_RUNS, selection=Selection.OVER, line=4.5, side=Side.HOME
        )
        away_side = make_quote(
            market=Market.TEAM_TOTAL_RUNS, selection=Selection.UNDER, line=4.5, side=Side.AWAY
        )
        assert group_key(home_side) != group_key(away_side)


# ── settlement model ─────────────────────────────────────────────────────────


class TestSettlementOutcomes:
    def test_full_game_moneyline_cannot_push(self) -> None:
        outcomes = settlement_outcomes(
            Market.MONEYLINE, Period.FULL_GAME, None, frozenset({Selection.HOME, Selection.AWAY})
        )
        assert [label for label, _ in outcomes] == ["away", "home"]

    def test_two_way_first_five_innings_moneyline_can_push(self) -> None:
        """Five innings can end level. A book offering only two selections must
        be voiding the tie, so the tie refunds both legs."""
        outcomes = settlement_outcomes(
            Market.MONEYLINE,
            Period.FIRST_5_INNINGS,
            None,
            frozenset({Selection.HOME, Selection.AWAY}),
        )
        assert "push" in [label for label, _ in outcomes]

    def test_three_way_market_has_no_push(self) -> None:
        """When the tie is itself a priced selection there is nothing to void."""
        outcomes = settlement_outcomes(
            Market.MONEYLINE,
            Period.FIRST_5_INNINGS,
            None,
            frozenset({Selection.HOME, Selection.AWAY, Selection.DRAW}),
        )
        labels = [label for label, _ in outcomes]
        assert "push" not in labels
        assert set(labels) == {"home", "away", "draw"}

    def test_half_run_lines_cannot_push(self) -> None:
        outcomes = settlement_outcomes(
            Market.RUN_LINE, Period.FULL_GAME, -1.5, frozenset({Selection.HOME, Selection.AWAY})
        )
        assert [label for label, _ in outcomes] == ["home_covers", "away_covers"]

    def test_whole_run_lines_can_push(self) -> None:
        outcomes = settlement_outcomes(
            Market.RUN_LINE, Period.FULL_GAME, -1.0, frozenset({Selection.HOME, Selection.AWAY})
        )
        assert "push" in [label for label, _ in outcomes]

    def test_whole_totals_can_push(self) -> None:
        outcomes = settlement_outcomes(
            Market.TOTAL_RUNS, Period.FULL_GAME, 9.0, frozenset({Selection.OVER, Selection.UNDER})
        )
        assert "push" in [label for label, _ in outcomes]

    def test_half_totals_cannot_push(self) -> None:
        outcomes = settlement_outcomes(
            Market.TOTAL_RUNS, Period.FULL_GAME, 8.5, frozenset({Selection.OVER, Selection.UNDER})
        )
        assert "push" not in [label for label, _ in outcomes]


# ── detection: synthetic two-book slates ─────────────────────────────────────


def _pair(
    *,
    market: Market = Market.MONEYLINE,
    period: Period = Period.FULL_GAME,
    home_odds: float,
    away_odds: float,
    home_line: float | None = None,
    away_line: float | None = None,
    home_source: str = "book_a",
    away_source: str = "book_b",
    **extra,
) -> list:
    """Two synthetic quotes forming one cross-book market."""
    home = make_quote(
        source=home_source,
        market=market,
        period=period,
        selection=Selection.HOME if market is not Market.TOTAL_RUNS else Selection.OVER,
        line=home_line,
        decimal_odds=home_odds,
        source_market_id=f"{home_source}-m",
        **extra,
    )
    away = make_quote(
        source=away_source,
        market=market,
        period=period,
        selection=Selection.AWAY if market is not Market.TOTAL_RUNS else Selection.UNDER,
        line=away_line,
        decimal_odds=away_odds,
        source_market_id=f"{away_source}-m",
        **extra,
    )
    return [home, away]


def _three_way_book(
    source: str, period: Period, home: float, away: float, draw: float
) -> list:
    """One book's three-way market.  Prices must hold a real edge for that book —
    a book whose own three prices sum below 1.0 does not exist, and the detector
    now refuses to draw legs from one."""
    return [
        make_quote(
            source=source,
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


def _three_way_slate(period: Period) -> list:
    """Two three-way books, each holding a 3.85% edge, that disagree on the
    favourite.  Best-of-each is an arbitrage; neither book is one alone."""
    return [
        *_three_way_book("book_a", period, home=2.60, away=2.00, draw=6.50),
        *_three_way_book("book_b", period, home=2.00, away=2.60, draw=6.50),
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

    def test_run_line_arb_pairs_mirrored_lines_across_books(self) -> None:
        report = find_opportunities(
            _pair(
                market=Market.RUN_LINE,
                home_odds=2.10,
                away_odds=2.10,
                home_line=-1.5,
                away_line=1.5,
            )
        )
        assert len(report.opportunities) == 1
        assert report.opportunities[0].line == -1.5
        assert not report.opportunities[0].can_push

    def test_mismatched_run_lines_produce_nothing(self) -> None:
        """A middle must not surface through the arbitrage interface."""
        report = find_opportunities(
            _pair(
                market=Market.RUN_LINE,
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
                market=Market.TOTAL_RUNS,
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
            make_quote(
                source="book_b",
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
            make_quote(
                source="book_a",
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
            update={"home_team": "Philadelphia Phillies", "away_team": "Miami Marlins"}
        )
        report = find_opportunities(quotes)
        assert report.opportunities == []
        assert "legs_disagree_on_the_game" in [d.code for d in report.diagnostics]

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
            make_quote(
                source="book_c",
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


class TestPushRisk:
    def test_whole_number_total_reports_a_zero_floor(self) -> None:
        """Synthetic: over/under 9 at 2.10 each looks like a 4.76% arb, but the
        game landing on exactly 9 refunds both legs."""
        report = find_opportunities(
            _pair(
                market=Market.TOTAL_RUNS,
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

    def test_whole_number_run_line_reports_a_zero_floor(self) -> None:
        report = find_opportunities(
            _pair(
                market=Market.RUN_LINE,
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
                market=Market.TOTAL_RUNS,
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


class TestSettlementCompatibility:
    def test_two_way_and_three_way_books_are_never_combined(self) -> None:
        """Backing home at a two-way book and away at a three-way book leaves
        the draw uncovered: a level score loses one leg and voids the other."""
        quotes = [
            *(
                make_quote(
                    source="two_way",
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

    def test_a_three_way_arb_needs_all_three_outcomes(self) -> None:
        """Each book holds a normal edge on its own — 1/2.60 + 1/2.00 + 1/6.50 =
        1.0385 — but they disagree on which side is the favourite.  Taking the
        best of each gives 1/2.60 + 1/2.60 + 1/6.50 = 0.9231, a 7.69% margin."""
        report = find_opportunities(_three_way_slate(Period.FIRST_1_INNING))
        assert len(report.opportunities) == 1
        opp = report.opportunities[0]
        assert len(opp.legs) == 3
        assert {leg.selection for leg in opp.legs} == {
            Selection.HOME,
            Selection.AWAY,
            Selection.DRAW,
        }
        assert opp.margin == pytest.approx(1 - (1 / 2.60 + 1 / 2.60 + 1 / 6.50), abs=1e-9)

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
                    make_quote(
                        source=source,
                        period=Period.FIRST_1_INNING,
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


class TestFullGamePickEm:
    def test_a_full_game_run_line_at_zero_cannot_push(self) -> None:
        """Extra innings are played until someone wins, so a nine-inning game
        cannot end level and a pick'em run line has no push outcome.  Treating it
        as pushable reported a floor of zero for an impossible outcome."""
        report = find_opportunities(
            _pair(
                market=Market.RUN_LINE,
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

    def test_a_partial_period_run_line_at_zero_can_push(self) -> None:
        """Five innings really can end level, so there the push is real."""
        report = find_opportunities(
            _pair(
                market=Market.RUN_LINE,
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
            market=Market.TOTAL_RUNS,
            home_odds=2.20,
            away_odds=2.20,
            home_line=9.0,
            away_line=9.0,
        )
        real = [
            q.model_copy(update={"event_key": "AAA@BBB:2026-07-28"})
            for q in _pair(home_odds=2.05, away_odds=2.05)
        ]
        report = find_opportunities([*pushable, *real], total_stake=100.0)
        assert len(report.opportunities) == 2
        first, second = report.opportunities
        assert first.guaranteed_profit > second.guaranteed_profit
        assert first.margin < second.margin, "the higher margin must not win"
        assert second.can_push


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

    def test_opportunities_are_ordered_by_margin(self) -> None:
        quotes = [
            *_pair(home_odds=2.05, away_odds=2.05),
            *[
                q.model_copy(update={"event_key": "AAA@BBB:2026-07-28"})
                for q in _pair(home_odds=2.40, away_odds=2.40)
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


# ── the real captured slate ──────────────────────────────────────────────────


class TestRealFixtures:
    def test_the_captured_slate_contains_no_arbitrage(self, all_fixture_quotes) -> None:
        """Three correctly-priced books on one real MLB slate.  Zero is the
        expected answer; a non-zero result here means the detector has invented
        an edge out of a pairing error."""
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
        assert report.comparable_group_count >= 30

    def test_every_cross_book_pair_is_line_consistent(self, all_fixture_quotes) -> None:
        """Whatever gets grouped together must agree on the market it is."""
        for key, selections in best_prices(all_fixture_quotes).items():
            _, market, _, _, line = key
            for quote in selections.values():
                assert canonical_line(quote) == line
                assert quote.market is market

    def test_no_group_mixes_events_or_periods(self, all_fixture_quotes) -> None:
        for key, selections in best_prices(all_fixture_quotes).items():
            event_key, market, period, side, _ = key
            for quote in selections.values():
                assert quote.event_key == event_key
                assert quote.period is period
                assert quote.side == side
