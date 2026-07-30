"""The closed vocabularies and the settlement facts.

:data:`src.vocab.PERIOD_RULES` is the highest-consequence table in the codebase.
It records, per ``(sport, period)``, whether a scoring window can end level and
whether the books price the tie as a third selection.  Those two bits decide
whether a two-way moneyline has a push outcome, and the two readings differ by
the entire bankroll — so each entry is asserted here against the rules of the
sport rather than left to be inferred from a comment.
"""
from __future__ import annotations

import pytest

from src.vocab import (
    CORE_MARKETS_BY_SPORT,
    MARKETS_REQUIRING_LINE,
    MARKETS_REQUIRING_SIDE,
    PERIOD_RULES,
    SELECTIONS_BY_MARKET,
    Market,
    Period,
    Selection,
    Sport,
    draw_is_priced,
    is_collectable,
    period_rules,
    scoring_unit,
    tie_possible,
)


class TestClosedVocabularies:
    def test_every_market_declares_its_legal_selections(self) -> None:
        assert set(SELECTIONS_BY_MARKET) == set(Market)
        for market, selections in SELECTIONS_BY_MARKET.items():
            assert selections, f"{market} has no legal selections"

    def test_only_the_moneyline_may_carry_a_draw(self) -> None:
        """A draw is a result, so it cannot be a side of a spread or a total."""
        for market, selections in SELECTIONS_BY_MARKET.items():
            if market is not Market.MONEYLINE:
                assert Selection.DRAW not in selections, market

    def test_line_and_side_requirements_are_disjoint_and_sensible(self) -> None:
        assert Market.MONEYLINE not in MARKETS_REQUIRING_LINE
        assert MARKETS_REQUIRING_SIDE == {Market.TEAM_TOTAL}
        assert MARKETS_REQUIRING_SIDE <= MARKETS_REQUIRING_LINE

    def test_every_sport_declares_core_markets(self) -> None:
        assert set(CORE_MARKETS_BY_SPORT) == set(Sport)
        for sport, markets in CORE_MARKETS_BY_SPORT.items():
            assert Market.MONEYLINE in markets, f"{sport} must at least have a moneyline"

    def test_tennis_is_scoped_to_the_match_winner(self) -> None:
        """Games handicaps and total games are real markets but a different
        scope; they are counted out of scope rather than half-supported."""
        assert CORE_MARKETS_BY_SPORT[Sport.TENNIS] == {Market.MONEYLINE}


class TestSettlementFacts:
    @pytest.mark.parametrize(("sport", "period"), sorted(PERIOD_RULES, key=str))
    def test_a_priced_draw_implies_a_tie_is_possible(self, sport, period) -> None:
        """The books cannot price an outcome that cannot occur.  The converse does
        not hold: an NFL game can tie without a draw being priced."""
        rules = PERIOD_RULES[(sport, period)]
        if rules.draw_is_priced:
            assert rules.tie_possible, f"{sport}/{period} prices a draw it says cannot happen"

    @pytest.mark.parametrize(("sport", "period"), sorted(PERIOD_RULES, key=str))
    def test_every_window_names_its_scoring_unit(self, sport, period) -> None:
        assert PERIOD_RULES[(sport, period)].scoring_unit

    def test_windows_that_cannot_end_level(self) -> None:
        """Extra innings, overtime periods and shootouts run until decided, and a
        tennis match must produce a winner.  A two-way moneyline in one of these
        windows is complete and has no push outcome."""
        for sport in (Sport.BASEBALL, Sport.BASKETBALL, Sport.HOCKEY, Sport.TENNIS):
            assert not tie_possible(sport, Period.FULL_GAME), sport
            assert not draw_is_priced(sport, Period.FULL_GAME), sport

    def test_football_can_tie_without_pricing_a_draw(self) -> None:
        """The entry that costs the most if it is wrong.  An NFL game still level
        after overtime is a tie, and the two-way moneyline VOIDS — a push outcome
        that no field on the row reveals.  Arbitrage must enumerate it."""
        assert tie_possible(Sport.FOOTBALL, Period.FULL_GAME)
        assert not draw_is_priced(Sport.FOOTBALL, Period.FULL_GAME)

    def test_soccer_full_time_is_three_way(self) -> None:
        """A soccer FULL_GAME market is 90 minutes plus stoppage, which ends level
        often enough that the draw is priced.  A complete market has three legs,
        so a two-way one is missing a leg rather than being a two-way contract."""
        assert draw_is_priced(Sport.SOCCER, Period.FULL_GAME)
        assert tie_possible(Sport.SOCCER, Period.FULL_GAME)

    def test_hockey_regulation_and_full_game_are_different_contracts(self) -> None:
        """The distinction the whole REGULATION period exists for.  Both books
        offer both: Kambi as "Puck Line - Regular Time" versus "...Including
        Overtime and Penalty Shootout", Pinnacle as period 6 versus period 0.
        Sixty minutes is three-way; including the shootout is two-way."""
        assert draw_is_priced(Sport.HOCKEY, Period.REGULATION)
        assert not draw_is_priced(Sport.HOCKEY, Period.FULL_GAME)
        assert tie_possible(Sport.HOCKEY, Period.REGULATION)
        assert not tie_possible(Sport.HOCKEY, Period.FULL_GAME)

    def test_baseball_partial_periods_price_a_draw(self) -> None:
        """Five innings can end level and the books price it; nine cannot."""
        assert draw_is_priced(Sport.BASEBALL, Period.FIRST_5_INNINGS)
        assert draw_is_priced(Sport.BASEBALL, Period.FIRST_1_INNING)
        assert not draw_is_priced(Sport.BASEBALL, Period.FULL_GAME)

    def test_scoring_units_are_the_sports_own(self) -> None:
        assert scoring_unit(Sport.BASEBALL) == "runs"
        assert scoring_unit(Sport.HOCKEY) == "goals"
        assert scoring_unit(Sport.SOCCER) == "goals"
        assert scoring_unit(Sport.BASKETBALL) == "points"
        assert scoring_unit(Sport.FOOTBALL) == "points"
        assert scoring_unit(Sport.TENNIS) == "games"


class TestEverySettlementFactIsWrittenDown:
    """The table asserted entry by entry, against literals rather than against
    itself.

    The reasoned tests above are real oracles, but they only reach nine of the
    fourteen entries.  ``basketball/regulation``, ``basketball/first_half``,
    ``football/regulation``, ``football/first_half`` and ``soccer/first_half``
    had none: flipping ``draw_is_priced`` on four of them at once left 655 of 656
    adversarial cases green, and the one failure blamed the README.

    No fixture can close that gap.  A July capture contains no NFL first-half
    three-way, so replaying real bytes proves nothing about these windows — the
    fact has to be *written down*, the way ``COMMISSIONS`` and ``SETTLEMENT``
    are.  What it is worth if it is wrong is the whole stake: ``src.arb`` skips
    ``ambiguous_tie_settlement`` when ``draw_is_priced`` is False and appends a
    push outcome instead, so a flip publishes a real three-way market as a
    void-on-tie two-way "guarantee".
    """

    #: ``(sport, period) -> (draw_is_priced, tie_possible, scoring_unit)``.
    #: Keyed and valued in plain strings and bools so that renaming an enum
    #: member is a failure here rather than a silent rewrite of the oracle.
    EXPECTED = {
        ("baseball", "full_game"): (False, False, "runs"),
        ("baseball", "first_5_innings"): (True, True, "runs"),
        ("baseball", "first_1_inning"): (True, True, "runs"),
        ("basketball", "full_game"): (False, False, "points"),
        ("basketball", "regulation"): (True, True, "points"),
        ("basketball", "first_half"): (True, True, "points"),
        ("hockey", "full_game"): (False, False, "goals"),
        ("hockey", "regulation"): (True, True, "goals"),
        ("football", "full_game"): (False, True, "points"),
        ("football", "regulation"): (True, True, "points"),
        ("football", "first_half"): (True, True, "points"),
        ("tennis", "full_game"): (False, False, "games"),
        ("soccer", "full_game"): (True, True, "goals"),
        ("soccer", "first_half"): (True, True, "goals"),
    }

    def test_the_table_is_exactly_what_it_is(self) -> None:
        assert {
            (sport.value, period.value): (
                rules.draw_is_priced,
                rules.tie_possible,
                rules.scoring_unit,
            )
            for (sport, period), rules in PERIOD_RULES.items()
        } == self.EXPECTED

    def test_a_partial_window_that_can_end_level_is_priced_three_way(self) -> None:
        """The five entries the reasoned tests above could not reach, stated as
        the rule they share.

        A partial window — a half, or regulation before overtime — stops on the
        clock rather than on a decision, so it can end level; and where a level
        result is a real outcome the books sell it as one.  The half-time result
        and the 60-minute "regular time" line are three-way markets in all three
        of these sports, which is why a two-way price in one of these windows is
        a market with a leg missing rather than a two-way contract.
        """
        for sport, period in (
            (Sport.BASKETBALL, Period.REGULATION),
            (Sport.BASKETBALL, Period.FIRST_HALF),
            (Sport.FOOTBALL, Period.REGULATION),
            (Sport.FOOTBALL, Period.FIRST_HALF),
            (Sport.SOCCER, Period.FIRST_HALF),
        ):
            assert draw_is_priced(sport, period), f"{sport.value}/{period.value}"
            assert tie_possible(sport, period), f"{sport.value}/{period.value}"

    def test_the_partial_window_differs_from_its_own_full_game(self) -> None:
        """Stated as a contrast so that "everything is three-way" cannot pass it.

        Basketball and football price the same fixture two ways at once: the
        full game cannot be sold as a draw because overtime decides it, while
        regulation and the first half can.  A change that flipped a whole sport
        in one direction would satisfy the entry-by-entry pin's *shape* and fail
        here.
        """
        for sport in (Sport.BASKETBALL, Sport.FOOTBALL):
            assert not draw_is_priced(sport, Period.FULL_GAME), sport
            assert draw_is_priced(sport, Period.REGULATION), sport
        # Soccer is the mirror image: 90 minutes is itself a draw-priced window,
        # so its first half is not a contrast but a continuation.
        assert draw_is_priced(Sport.SOCCER, Period.FULL_GAME)

    def test_football_is_the_only_window_that_ties_without_pricing_it(self) -> None:
        """``(False, True)`` is the combination that costs a bankroll, because it
        is the only one where a two-way market is complete *and* has a push.  It
        appears exactly once, and a second appearance would mean some other
        window had quietly acquired a void outcome."""
        assert [
            f"{sport.value}/{period.value}"
            for (sport, period), rules in PERIOD_RULES.items()
            if rules.tie_possible and not rules.draw_is_priced
        ] == ["football/full_game"]


class TestUnknownWindowsAreRefused:
    def test_an_unrecorded_window_is_not_collectable(self) -> None:
        """Tennis has no first half, and hockey has no fifth inning."""
        assert not is_collectable(Sport.TENNIS, Period.FIRST_5_INNINGS)
        assert not is_collectable(Sport.HOCKEY, Period.FIRST_5_INNINGS)
        assert not is_collectable(Sport.SOCCER, Period.FIRST_1_INNING)

    def test_asking_for_an_unrecorded_window_raises_rather_than_defaulting(self) -> None:
        """A default here would be a guess about whether a tie voids the bet, and
        that guess is worth the whole stake."""
        with pytest.raises(KeyError, match="no settlement rules recorded"):
            period_rules(Sport.TENNIS, Period.FIRST_5_INNINGS)

    def test_the_predicates_are_safe_on_unknown_windows(self) -> None:
        """They answer False rather than raising, so a caller checking legality
        does not have to guard every call."""
        assert not draw_is_priced(Sport.TENNIS, Period.FIRST_5_INNINGS)
        assert not tie_possible(Sport.TENNIS, Period.FIRST_5_INNINGS)

    def test_every_sport_can_collect_a_full_game(self) -> None:
        for sport in Sport:
            assert is_collectable(sport, Period.FULL_GAME), sport
