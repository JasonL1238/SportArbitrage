"""Schema invariants — the guarantees every downstream consumer relies on."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.schema import Market, Period, Quote, Selection, Side, Sport
from tests.conftest import make_quote


def test_valid_quote_round_trips() -> None:
    quote = make_quote()
    assert Quote(**quote.model_dump()) == quote


def test_quotes_are_immutable() -> None:
    quote = make_quote()
    with pytest.raises(ValidationError):
        quote.decimal_odds = 2.0


def test_unknown_field_is_rejected() -> None:
    """``extra="forbid"`` stops a source-specific field being smuggled in."""
    with pytest.raises(ValidationError):
        make_quote(kambi_criterion="Moneyline")


@pytest.mark.parametrize("odds", [1.0, 0.5, 0.0, -1.5, 1001.0])
def test_impossible_odds_are_rejected(odds: float) -> None:
    with pytest.raises(ValidationError):
        make_quote(decimal_odds=odds, implied_probability=0.5)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_quote(observed_at=datetime(2026, 7, 28, 7, 0))


def test_datetimes_are_normalized_to_utc() -> None:
    quote = make_quote(observed_at=datetime(2026, 7, 28, 3, 0, tzinfo=UTC) + timedelta(0))
    assert quote.observed_at.tzinfo is UTC


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_identity_fields_are_rejected(blank: str) -> None:
    with pytest.raises(ValidationError):
        make_quote(event_key=blank)
    with pytest.raises(ValidationError):
        make_quote(home_team=blank)


def test_selection_must_be_legal_for_the_market() -> None:
    with pytest.raises(ValidationError):
        make_quote(market=Market.MONEYLINE, selection=Selection.OVER)
    with pytest.raises(ValidationError):
        make_quote(market=Market.TOTAL, selection=Selection.HOME, line=8.5)
    with pytest.raises(ValidationError):
        make_quote(market=Market.SPREAD, selection=Selection.DRAW, line=1.5)


def test_line_is_required_exactly_where_it_is_meaningful() -> None:
    with pytest.raises(ValidationError):
        make_quote(market=Market.SPREAD, selection=Selection.HOME, line=None)
    with pytest.raises(ValidationError):
        make_quote(market=Market.TOTAL, selection=Selection.OVER, line=None)
    # A moneyline carries no line; FanDuel sends handicap 0 for it.
    with pytest.raises(ValidationError):
        make_quote(market=Market.MONEYLINE, selection=Selection.HOME, line=0.0)


def test_side_is_required_only_for_team_totals() -> None:
    with pytest.raises(ValidationError):
        make_quote(market=Market.TEAM_TOTAL, selection=Selection.OVER, line=4.5, side=None)
    with pytest.raises(ValidationError):
        make_quote(market=Market.MONEYLINE, selection=Selection.HOME, side=Side.HOME)
    assert make_quote(
        market=Market.TEAM_TOTAL, selection=Selection.OVER, line=4.5, side=Side.AWAY
    ).side is Side.AWAY


def test_draw_legality_follows_the_sport_and_period() -> None:
    """A full baseball game cannot end level, so a full-game draw price would be a
    parsing error.  Five innings can, and the books price it."""
    with pytest.raises(ValidationError):
        make_quote(period=Period.FULL_GAME, selection=Selection.DRAW)
    for period in (Period.FIRST_1_INNING, Period.FIRST_5_INNINGS):
        assert make_quote(period=period, selection=Selection.DRAW).period is period


def test_draw_is_legal_where_the_books_price_one() -> None:
    """Soccer over 90 minutes and hockey over 60 both price a draw; the same
    hockey market including the shootout does not, because it cannot tie."""
    soccer = make_quote(
        sport=Sport.SOCCER, league="EPL", period=Period.FULL_GAME, selection=Selection.DRAW,
        home_participant="SOCCER-chelsea", away_participant="SOCCER-arsenal",
        home_team="Chelsea", away_team="Arsenal",
    )
    assert soccer.selection is Selection.DRAW

    hockey_regulation = make_quote(
        sport=Sport.HOCKEY, league="NHL", period=Period.REGULATION, selection=Selection.DRAW,
        home_participant="NHL-BOS", away_participant="NHL-NYR",
        home_team="Boston Bruins", away_team="New York Rangers",
    )
    assert hockey_regulation.period is Period.REGULATION

    with pytest.raises(ValidationError):
        make_quote(
            sport=Sport.HOCKEY, league="NHL", period=Period.FULL_GAME, selection=Selection.DRAW,
            home_participant="NHL-BOS", away_participant="NHL-NYR",
            home_team="Boston Bruins", away_team="New York Rangers",
        )


def test_a_window_without_recorded_settlement_rules_is_refused() -> None:
    """Collecting a window whose tie semantics are unknown would mean guessing
    whether a tie voids the bet, and that guess is worth the whole stake."""
    with pytest.raises(ValidationError, match="settlement rules"):
        make_quote(sport=Sport.TENNIS, league="ATP", period=Period.FIRST_5_INNINGS,
                   home_participant="TENNIS-ax", away_participant="TENNIS-by",
                   home_team="A X", away_team="B Y")


def test_a_participant_cannot_play_itself() -> None:
    with pytest.raises(ValidationError):
        make_quote(home_participant="MLB-MIA", away_participant="MLB-MIA")


def test_market_key_groups_by_source_market_not_by_line() -> None:
    """Both sides of a run line must land in one group even though their lines
    are opposites."""
    home = make_quote(market=Market.SPREAD, selection=Selection.HOME, line=1.5,
                      source_market_id="m1", decimal_odds=1.5, american_odds=-200)
    away = make_quote(market=Market.SPREAD, selection=Selection.AWAY, line=-1.5,
                      source_market_id="m1", decimal_odds=2.6, american_odds=160)
    other = make_quote(market=Market.SPREAD, selection=Selection.AWAY, line=1.5,
                       source_market_id="m2", decimal_odds=1.4, american_odds=-250)
    assert home.market_key == away.market_key
    assert other.market_key != home.market_key


def test_dedup_key_separates_lines_and_sides() -> None:
    over_85 = make_quote(market=Market.TOTAL, selection=Selection.OVER, line=8.5)
    over_95 = make_quote(market=Market.TOTAL, selection=Selection.OVER, line=9.5)
    assert over_85.dedup_key != over_95.dedup_key

    home_tt = make_quote(market=Market.TEAM_TOTAL, selection=Selection.OVER,
                         line=4.5, side=Side.HOME)
    away_tt = make_quote(market=Market.TEAM_TOTAL, selection=Selection.OVER,
                         line=4.5, side=Side.AWAY)
    assert home_tt.dedup_key != away_tt.dedup_key
