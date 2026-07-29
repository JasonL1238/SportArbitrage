"""Validation under deliberately corrupted input.

Each test here builds data that is wrong in one specific way and asserts that
validation says so.  The cases are drawn from failures that previously passed
*silently* — a run reporting PASS while holding two different games under one
event key, two different lines fused into one market, or a soccer moneyline whose
draw leg was dropped on the way in.  A check that cannot be made to fire is not a
check, so every one of these starts by confirming the corruption is actually
present in the input.

Every row is **synthetic**, hand-built around one fault.  None of it is an
observed price.

The multi-sport fixtures are shared with :mod:`tests.test_validation` rather than
duplicated: there is one definition of what a clean NFL, NHL, soccer and tennis
slate looks like, so a change to it cannot make one file's baseline disagree with
the other's.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.schema import Market, Period, QuoteStatus, Selection, Side, Sport
from src.validation import (
    MAX_SUSPENDED_FRACTION,
    Severity,
    validate,
)
from tests.conftest import make_quote
from tests.test_validation import (
    MLB_GAME,
    NFL_GAME,
    NHL_GAME,
    SOCCER_GAME,
    TENNIS_MATCH,
    Fixture,
    _book,
    _quote,
)

BASEBALL_EVENT = "MLB-PHI@MLB-MIA:2026-07-28"


def _codes(report, severity: Severity | None = None) -> set[str]:
    findings = report.findings if severity is None else [
        f for f in report.findings if f.severity is severity
    ]
    return {f.code for f in findings}


def _complete_book(source: str, *, event_key: str = BASEBALL_EVENT, **overrides) -> list:
    """One book pricing all three core full-game baseball markets for one game."""
    quotes = []
    for selection, odds in ((Selection.HOME, 1.91), (Selection.AWAY, 1.95)):
        quotes.append(
            make_quote(
                source=source,
                event_key=event_key,
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"{source}-{event_key}-ml",
                **overrides,
            )
        )
    for selection, line, odds in (
        (Selection.HOME, -1.5, 2.40),
        (Selection.AWAY, 1.5, 1.62),
    ):
        quotes.append(
            make_quote(
                source=source,
                event_key=event_key,
                market=Market.SPREAD,
                selection=selection,
                line=line,
                decimal_odds=odds,
                source_market_id=f"{source}-{event_key}-sp",
                **overrides,
            )
        )
    for selection, odds in ((Selection.OVER, 1.91), (Selection.UNDER, 1.95)):
        quotes.append(
            make_quote(
                source=source,
                event_key=event_key,
                market=Market.TOTAL,
                selection=selection,
                line=8.5,
                decimal_odds=odds,
                source_market_id=f"{source}-{event_key}-tot",
                **overrides,
            )
        )
    return quotes


def _clean_slate() -> list:
    return [*_complete_book("book_a"), *_complete_book("book_b")]


def test_the_baseline_slate_is_clean() -> None:
    """Everything below perturbs this, so it must start with no errors — a test
    that fires on clean data proves nothing about the corruption."""
    report = validate(_clean_slate())
    assert report.errors == [], "\n".join(str(f) for f in report.errors)


class TestMarketGrouping:
    def test_two_lines_fused_under_one_market_id_is_an_error(self) -> None:
        """A primary total and an alternate total sharing a market id used to
        pass: the group looked complete, and summing four probabilities put the
        overround near 2.0, which only tripped a warning."""
        quotes = _clean_slate()
        quotes += [
            make_quote(
                source="book_a",
                market=Market.TOTAL,
                selection=selection,
                line=9.5,
                decimal_odds=odds,
                source_market_id=f"book_a-{BASEBALL_EVENT}-tot",  # same id as the 8.5 market
            )
            for selection, odds in ((Selection.OVER, 2.30), (Selection.UNDER, 1.65))
        ]
        report = validate(quotes)
        assert "repeated_selection_within_market" in _codes(report, Severity.ERROR)

    def test_two_spread_pairs_fused_under_one_market_id_is_an_error(self) -> None:
        """Spreads were exempt from the line-set check, so a group holding
        -1.5/+1.5 and -2.5/+2.5 passed as "mirrored" — the mirroring test read
        two arbitrary rows out of a last-wins dict."""
        quotes = _clean_slate()
        quotes += [
            make_quote(
                source="book_a",
                market=Market.SPREAD,
                selection=selection,
                line=line,
                decimal_odds=odds,
                source_market_id=f"book_a-{BASEBALL_EVENT}-sp",
            )
            for selection, line, odds in (
                (Selection.HOME, -2.5, 3.20),
                (Selection.AWAY, 2.5, 1.36),
            )
        ]
        report = validate(quotes)
        assert "repeated_selection_within_market" in _codes(report, Severity.ERROR)

    def test_both_teams_totals_fused_under_one_market_id_is_an_error(self) -> None:
        """Two different teams' totals in one group: every check read the side
        off row zero and applied it to all four rows."""
        quotes = _clean_slate()
        quotes += [
            make_quote(
                source="book_a",
                market=Market.TEAM_TOTAL,
                selection=selection,
                side=side,
                line=4.5,
                decimal_odds=odds,
                source_market_id="book_a-tt-shared",
            )
            for side, selection, odds in (
                (Side.HOME, Selection.OVER, 1.95),
                (Side.HOME, Selection.UNDER, 1.91),
                (Side.AWAY, Selection.OVER, 1.87),
                (Side.AWAY, Selection.UNDER, 2.00),
            )
        ]
        report = validate(quotes)
        assert "heterogeneous_market" in _codes(report, Severity.ERROR)

    def test_a_market_id_spanning_two_periods_is_an_error(self) -> None:
        quotes = _clean_slate()
        quotes += [
            make_quote(
                source="book_a",
                market=Market.TOTAL,
                period=period,
                selection=selection,
                line=4.5,
                decimal_odds=1.91,
                source_market_id="book_a-mixed-period",
            )
            for period, selection in (
                (Period.FIRST_5_INNINGS, Selection.OVER),
                (Period.FIRST_1_INNING, Selection.UNDER),
            )
        ]
        report = validate(quotes)
        assert "heterogeneous_market" in _codes(report, Severity.ERROR)

    def test_a_market_id_fusing_full_game_and_regulation_hockey_is_an_error(self) -> None:
        """The expensive fusion in hockey: full game includes the shootout and
        cannot tie, regulation excludes it and is priced three-way.  Under one
        market id the group looks like a five-outcome market."""
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b")]
        quotes += [
            _quote(
                NHL_GAME,
                "book_a",
                market=Market.MONEYLINE,
                period=period,
                selection=selection,
                decimal_odds=odds,
                source_market_id="book_a-fused-periods",
            )
            for period, selection, odds in (
                (Period.FULL_GAME, Selection.HOME, 1.91),
                (Period.REGULATION, Selection.DRAW, 3.40),
            )
        ]
        report = validate(quotes)
        assert "heterogeneous_market" in _codes(report, Severity.ERROR)

    def test_a_genuinely_negative_overround_is_still_caught(self) -> None:
        """The homogeneity guards must not shadow the check they protect."""
        quotes = _clean_slate()
        quotes = [q for q in quotes if not (q.source == "book_a" and q.market is Market.MONEYLINE)]
        quotes += [
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=2.20,
                source_market_id=f"book_a-{BASEBALL_EVENT}-ml",
            )
            for selection in (Selection.HOME, Selection.AWAY)
        ]
        report = validate(quotes)
        assert "negative_overround" in _codes(report, Severity.ERROR)


class TestDroppedDrawLeg:
    """The same two rows are a complete market in one sport and a fault in another."""

    def _partial_period(self, source: str, *, include_draw: bool) -> list:
        pairs = [(Selection.HOME, 2.30), (Selection.AWAY, 2.75)]
        if include_draw:
            pairs.append((Selection.DRAW, 4.50))
        return [
            make_quote(
                source=source,
                market=Market.MONEYLINE,
                period=Period.FIRST_1_INNING,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"{source}-f1-ml",
            )
            for selection, odds in pairs
        ]

    def test_a_complete_three_way_market_is_clean(self) -> None:
        quotes = [
            *_clean_slate(),
            *self._partial_period("book_a", include_draw=True),
            *self._partial_period("book_b", include_draw=True),
        ]
        report = validate(quotes)
        assert "draw_leg_missing" not in _codes(report)
        assert "negative_overround" not in _codes(report)

    def test_a_two_way_partial_period_market_is_a_warning_not_an_error(self) -> None:
        """A two-way tie-void first-inning market is a product books really sell,
        so a missing draw there is suspicious rather than certainly wrong.  It
        must not be graded like the soccer case below, and — the original bug —
        its 1/2.30 + 1/2.75 = 0.7985 must not be reported as "the book prices
        itself to lose", which blames the prices for the absent row."""
        quotes = [
            *_clean_slate(),
            *self._partial_period("book_a", include_draw=False),
            *self._partial_period("book_b", include_draw=False),
        ]
        report = validate(quotes)
        assert "incomplete_market" in _codes(report, Severity.WARNING)
        assert "draw_leg_missing" not in _codes(report, Severity.ERROR)
        assert "negative_overround" not in _codes(report)

    def test_a_soccer_ninety_minute_moneyline_missing_its_draw_is_an_error(self) -> None:
        """This is the case a row-count rule can never catch.  Two rows on a
        soccer full-game moneyline look exactly like a fair two-way market, and
        the two prices sum to 0.72 — so read as two-way it is a 28% "arbitrage".
        The books do not sell a two-way 90-minute market: FanDuel's headline
        market is WIN-DRAW-WIN, Kambi's is "Full Time", and Pinnacle prices a
        ``draw`` designation.  Three legs is the market; two is a dropped leg."""
        two_way = [
            _quote(
                SOCCER_GAME,
                "book_a",
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"book_a-{SOCCER_GAME.event_key}-ml",
            )
            for selection, odds in ((Selection.HOME, 2.40), (Selection.AWAY, 3.30))
        ]
        quotes = [
            *[q for q in _book(SOCCER_GAME, "book_a") if q.market is not Market.MONEYLINE],
            *two_way,
            *_book(SOCCER_GAME, "book_b"),
        ]
        report = validate(quotes)
        assert sum(1.0 / q.decimal_odds for q in two_way) < 1.0
        assert "draw_leg_missing" in _codes(report, Severity.ERROR)

    def test_a_soccer_two_way_market_is_not_excused_by_a_plausible_sum(self) -> None:
        """Made harder: prices chosen so the two remaining legs sum *above* 1.0,
        which is what a healthy two-way market looks like.  Nothing about the
        prices reveals the missing leg, so the check has to come from the sport."""
        two_way = [
            _quote(
                SOCCER_GAME,
                "book_a",
                market=Market.MONEYLINE,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"book_a-{SOCCER_GAME.event_key}-ml",
            )
            for selection, odds in ((Selection.HOME, 1.91), (Selection.AWAY, 1.95))
        ]
        quotes = [
            *[q for q in _book(SOCCER_GAME, "book_a") if q.market is not Market.MONEYLINE],
            *two_way,
            *_book(SOCCER_GAME, "book_b"),
        ]
        assert sum(1.0 / q.decimal_odds for q in two_way) > 1.0
        report = validate(quotes)
        assert "draw_leg_missing" in _codes(report, Severity.ERROR)

    def test_a_three_way_market_missing_its_draw_is_reported(self) -> None:
        """Three rows present, one of them not the draw: the market declares
        itself three-way by having three selections, so a missing draw is a
        completeness failure rather than a pricing one."""
        quotes = [
            *_clean_slate(),
            *self._partial_period("book_a", include_draw=True),
        ]
        quotes = [q for q in quotes if q.selection is not Selection.DRAW]
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                period=Period.FIRST_1_INNING,
                selection=Selection.HOME,
                decimal_odds=2.31,
                source_market_id="book_a-f1-ml",
                is_alternate=True,
            )
        )
        report = validate(quotes)
        codes = _codes(report, Severity.ERROR)
        assert "repeated_selection_within_market" in codes or "incomplete_market" in _codes(report)


class TestDrawWhereNoneExists:
    """A draw on the wrong window is a third runner, and it is not always visible."""

    def test_a_hockey_full_game_draw_cannot_even_be_constructed(self) -> None:
        with pytest.raises(ValidationError):
            _quote(
                NHL_GAME,
                "book_a",
                market=Market.MONEYLINE,
                selection=Selection.DRAW,
                decimal_odds=4.20,
            )

    def test_a_hockey_full_game_draw_read_back_from_storage_is_an_error(self) -> None:
        """Rows arrive from SQLite and from ``model_copy`` as well as from
        adapters, and neither path re-runs the schema's validators — so the check
        has to exist in validation too, or a corrupt row that was persisted once
        is trusted forever."""
        forged = _quote(
            NHL_GAME,
            "book_a",
            market=Market.MONEYLINE,
            period=Period.REGULATION,
            selection=Selection.DRAW,
            decimal_odds=4.20,
            source_market_id="book_a-forged",
        ).model_copy(update={"period": Period.FULL_GAME})
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b"), forged]
        assert "illegal_draw" in _codes(validate(quotes), Severity.ERROR)

    def test_a_tennis_draw_is_an_error(self) -> None:
        """A tennis match cannot end level.  A "draw" runner on a match-winner
        market is a third selection from some other market."""
        forged = _quote(
            SOCCER_GAME,
            "book_a",
            market=Market.MONEYLINE,
            selection=Selection.DRAW,
            decimal_odds=3.40,
            source_market_id="book_a-forged",
        ).model_copy(
            update={
                "sport": Sport.TENNIS,
                "league": "ATP",
                "home_participant": TENNIS_MATCH.home_participant,
                "away_participant": TENNIS_MATCH.away_participant,
                "home_team": TENNIS_MATCH.home_team,
                "away_team": TENNIS_MATCH.away_team,
                "event_key": TENNIS_MATCH.event_key,
            }
        )
        quotes = [*_book(TENNIS_MATCH, "book_a"), *_book(TENNIS_MATCH, "book_b"), forged]
        assert "illegal_draw" in _codes(validate(quotes), Severity.ERROR)

    def test_a_hockey_regulation_draw_is_not_flagged(self) -> None:
        """The counterpart that must *not* fire: the same fixture's 60-minute
        market is three-way, and flagging it would delete a real market."""
        regulation = [
            _quote(
                NHL_GAME,
                source,
                market=Market.MONEYLINE,
                period=Period.REGULATION,
                selection=selection,
                decimal_odds=odds,
                source_market_id=f"{source}-reg-ml",
            )
            for source in ("book_a", "book_b")
            for selection, odds in (
                (Selection.HOME, 2.40),
                (Selection.AWAY, 3.30),
                (Selection.DRAW, 3.40),
            )
        ]
        quotes = [*_book(NHL_GAME, "book_a"), *_book(NHL_GAME, "book_b"), *regulation]
        report = validate(quotes)
        assert "illegal_draw" not in _codes(report)
        assert report.errors == [], "\n".join(str(f) for f in report.errors)


class TestFuturesLeakage:
    """The horizon must be wide enough for the NFL and still catch a 2030 opener."""

    def test_a_real_nfl_slate_five_months_out_is_not_rejected(self) -> None:
        quotes = [*_book(NFL_GAME, "book_a"), *_book(NFL_GAME, "book_b")]
        report = validate(quotes)
        assert "commence_time_too_far" not in _codes(report)
        assert report.errors == [], "\n".join(str(f) for f in report.errors)

    def test_a_2030_specials_container_with_resolvable_teams_is_still_caught(self) -> None:
        """The hard version of the case: the participants resolve perfectly well —
        a "Bills v Ravens" specials container names two real clubs — so the only
        thing separating it from a game is the date."""
        specials = Fixture(
            sport=Sport.FOOTBALL,
            league="NFL",
            home_team=NFL_GAME.home_team,
            away_team=NFL_GAME.away_team,
            home_participant=NFL_GAME.home_participant,
            away_participant=NFL_GAME.away_participant,
            commence=datetime(2030, 12, 1, 18, 0, tzinfo=UTC),
            total=44.5,
            spread=3.5,
        )
        quotes = [
            *_book(NFL_GAME, "book_a"),
            *_book(NFL_GAME, "book_b"),
            *_book(specials, "book_a"),
        ]
        report = validate(quotes)
        assert "commence_time_too_far" in _codes(report, Severity.ERROR)

    def test_a_baseball_game_at_the_nfl_horizon_is_still_rejected(self) -> None:
        """Widening the horizon per league must not widen it for everyone: 150
        days out is a normal NFL fixture and an impossible MLB one."""
        far = Fixture(
            sport=Sport.BASEBALL,
            league="MLB",
            home_team=MLB_GAME.home_team,
            away_team=MLB_GAME.away_team,
            home_participant=MLB_GAME.home_participant,
            away_participant=MLB_GAME.away_participant,
            commence=NFL_GAME.commence,
            total=8.5,
            spread=1.5,
        )
        quotes = [*_book(MLB_GAME, "book_a"), *_book(MLB_GAME, "book_b"), *_book(far, "book_a")]
        assert "commence_time_too_far" in _codes(validate(quotes), Severity.ERROR)


class TestUnitsAndWrongSport:
    """Kambi sends lines in thousandths, and a mapping error moves a whole market."""

    def test_a_thousandths_total_is_an_error_in_every_sport(self) -> None:
        for fixture, line in (
            (MLB_GAME, 8000.0),
            (NFL_GAME, 44500.0),
            (SOCCER_GAME, 2500.0),
        ):
            quotes = [
                *_book(fixture, "book_a"),
                *_book(fixture, "book_b"),
                *[
                    _quote(
                        fixture,
                        "book_a",
                        market=Market.TOTAL,
                        selection=selection,
                        line=line,
                        decimal_odds=1.91,
                        source_market_id="book_a-thousandths",
                        is_alternate=True,
                    )
                    for selection in (Selection.OVER, Selection.UNDER)
                ],
            ]
            assert "implausible_total" in _codes(validate(quotes), Severity.ERROR), fixture.league

    def test_a_soccer_total_mapped_onto_an_nfl_game_is_an_error(self) -> None:
        """2.5 is the most common soccer total there is and an impossible NFL one.
        Nothing about the row is malformed; only the sport it landed in is."""
        quotes = [
            *_book(NFL_GAME, "book_a"),
            *_book(NFL_GAME, "book_b"),
            *[
                _quote(
                    NFL_GAME,
                    "book_a",
                    market=Market.TOTAL,
                    selection=selection,
                    line=2.5,
                    decimal_odds=1.91,
                    source_market_id="book_a-wrong-sport",
                    is_alternate=True,
                )
                for selection in (Selection.OVER, Selection.UNDER)
            ],
        ]
        assert "implausible_total" in _codes(validate(quotes), Severity.ERROR)

    def test_an_nfl_total_mapped_onto_a_soccer_game_is_an_error(self) -> None:
        quotes = [
            *_book(SOCCER_GAME, "book_a"),
            *_book(SOCCER_GAME, "book_b"),
            *[
                _quote(
                    SOCCER_GAME,
                    "book_a",
                    market=Market.TOTAL,
                    selection=selection,
                    line=44.5,
                    decimal_odds=1.91,
                    source_market_id="book_a-wrong-sport",
                    is_alternate=True,
                )
                for selection in (Selection.OVER, Selection.UNDER)
            ],
        ]
        assert "implausible_total" in _codes(validate(quotes), Severity.ERROR)

    def test_a_row_whose_sport_contradicts_its_league_is_an_error(self) -> None:
        forged = _quote(
            SOCCER_GAME,
            "book_a",
            market=Market.MONEYLINE,
            selection=Selection.HOME,
            decimal_odds=2.40,
            source_market_id="book_a-mislabelled",
        ).model_copy(update={"sport": Sport.HOCKEY})
        quotes = [*_book(SOCCER_GAME, "book_a"), *_book(SOCCER_GAME, "book_b"), forged]
        assert "league_sport_mismatch" in _codes(validate(quotes), Severity.ERROR)


class TestPriceEncoding:
    def test_a_mispaired_american_value_is_caught_at_a_short_price(self) -> None:
        """The case a decimal-relative tolerance let through: american -5000
        (net payout 0.02) against decimal 1.0404 (net payout 0.0404) is a
        factor-of-two error in what the bet pays."""
        quotes = _clean_slate()
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                event_key="MLB-ATL@MLB-NYM:2026-07-28",
                selection=Selection.HOME,
                decimal_odds=1.0404,
                american_odds=-5000,
                source_market_id="book_a-short",
            )
        )
        report = validate(quotes)
        assert "odds_format_mismatch" in _codes(report, Severity.ERROR)

    def test_honest_rounding_at_a_long_price_is_not_flagged(self) -> None:
        """+2400 is exactly 25.0; one American point either way must not fire."""
        quotes = _clean_slate()
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                event_key="MLB-ATL@MLB-NYM:2026-07-28",
                selection=Selection.HOME,
                decimal_odds=25.0,
                american_odds=2400,
                source_market_id="book_a-long",
            )
        )
        report = validate(quotes)
        assert "odds_format_mismatch" not in _codes(report)

    def test_honest_rounding_at_even_money_is_not_flagged(self) -> None:
        """-110 is 1.9090909...; a book publishing 1.91 is rounding, not lying."""
        quotes = _clean_slate()
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                event_key="MLB-ATL@MLB-NYM:2026-07-28",
                selection=Selection.HOME,
                decimal_odds=1.91,
                american_odds=-110,
                source_market_id="book_a-even",
            )
        )
        report = validate(quotes)
        assert "odds_format_mismatch" not in _codes(report)

    def test_an_american_value_that_is_not_a_price_is_caught(self) -> None:
        """-50 is not an American price at all.  It used to convert cleanly to
        3.00 and validate against a decimal_odds of 3.00, so the corruption was
        self-consistent and invisible."""
        quotes = _clean_slate()
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                event_key="MLB-ATL@MLB-NYM:2026-07-28",
                selection=Selection.HOME,
                decimal_odds=3.0,
                american_odds=-50,
                source_market_id="book_a-bogus",
            )
        )
        report = validate(quotes)
        assert "american_odds_not_a_price" in _codes(report, Severity.ERROR)

    def test_an_absurd_decimal_price_is_caught(self) -> None:
        quotes = _clean_slate()
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                event_key="MLB-ATL@MLB-NYM:2026-07-28",
                selection=Selection.HOME,
                decimal_odds=1.0005,
                american_odds=-100000,
                source_market_id="book_a-absurd",
            )
        )
        report = validate(quotes)
        assert "implausible_odds" in _codes(report, Severity.ERROR)

    def test_a_thousandths_decimal_price_is_caught(self) -> None:
        """Kambi's 1640 means 1.640.  Left unscaled it is a price of 1640.0, which
        the schema refuses outright — and validation refuses it again, because a
        row can also arrive from storage or from ``model_copy``, neither of which
        re-runs the schema."""
        with pytest.raises(ValidationError):
            make_quote(decimal_odds=1640.0, american_odds=163900)

        forged = make_quote(
            source="book_a",
            market=Market.MONEYLINE,
            event_key="MLB-ATL@MLB-NYM:2026-07-28",
            selection=Selection.HOME,
            decimal_odds=999.0,
            source_market_id="book_a-thousandths",
        ).model_copy(update={"decimal_odds": 1640.0, "implied_probability": 1 / 1640.0})
        quotes = [*_clean_slate(), forged]
        assert "implausible_odds" in _codes(validate(quotes), Severity.ERROR)


class TestCoverage:
    def test_partial_market_coverage_across_events_is_caught(self) -> None:
        """A source pricing totals for most games and not a few satisfies the
        slate-wide coverage check while having lost those games' markets.

        Sized to a real slate deliberately.  The rule will not call a one-in-three
        gap a rename, because on that sample a book that simply did not post the
        market on one game is indistinguishable from one whose label changed — so a
        two-event version of this test would be asserting a guarantee the code
        does not, and should not, make.
        """
        from src.participants import roster_members

        clubs = list(roster_members("mlb"))
        slate = [
            (
                f"{clubs[2 * i + 1].key}@{clubs[2 * i].key}:2026-07-28",
                clubs[2 * i],
                clubs[2 * i + 1],
            )
            for i in range(10)
        ]

        def book(source: str, entry, *, with_total: bool = True) -> list:
            event_key, home, away = entry
            rows = _complete_book(
                source,
                event_key=event_key,
                home_participant=home.key,
                away_participant=away.key,
                home_team=home.name,
                away_team=away.name,
            )
            return rows if with_total else [r for r in rows if r.market is not Market.TOTAL]

        quotes = [
            row
            for entry in slate
            for source in ("book_a", "book_b")
            for row in book(source, entry)
        ]
        assert validate(quotes).errors == []

        # Now drop book_a's totals for a clear minority of the slate.
        thinned = [
            row
            for index, entry in enumerate(slate)
            for source in ("book_a", "book_b")
            for row in book(source, entry, with_total=not (source == "book_a" and index < 3))
        ]
        report = validate(thinned)
        # A warning, not an error: one run cannot separate a renamed label from a
        # book pricing some fixtures thinly, and an always-failing report has no
        # signal left in it.  A market vanishing from the sport entirely is what
        # `core_market_absent` still grades an error.
        assert "core_market_absent_for_event" in _codes(report, Severity.WARNING)
        assert "core_market_absent_for_event" not in _codes(report, Severity.ERROR)

    def test_a_market_missing_everywhere_is_reported_once_not_twice(self) -> None:
        """A source with no totals at all is a slate-wide gap; reporting it per
        event as well would bury the signal in noise."""
        quotes = [q for q in _clean_slate() if q.market is not Market.TOTAL]
        codes = _codes(validate(quotes), Severity.ERROR)
        assert "core_market_absent" in codes
        assert "core_market_absent_for_event" not in codes

    def test_a_missing_market_in_one_sport_does_not_condemn_another(self) -> None:
        """Coverage is per (source, sport): a book with a complete baseball slate
        and no soccer spreads must be reported for soccer only, or the finding
        cannot be acted on."""
        quotes = [
            *_complete_book("book_a"),
            *_complete_book("book_b"),
            *[q for q in _book(SOCCER_GAME, "book_a") if q.market is not Market.SPREAD],
            *_book(SOCCER_GAME, "book_b"),
        ]
        absent = [f for f in validate(quotes).errors if f.code == "core_market_absent"]
        assert len(absent) == 1
        assert "soccer" in absent[0].message and absent[0].source == "book_a"

    def test_tennis_without_totals_is_not_a_coverage_failure(self) -> None:
        quotes = [
            *_complete_book("book_a"),
            *_complete_book("book_b"),
            *_book(TENNIS_MATCH, "book_a"),
            *_book(TENNIS_MATCH, "book_b"),
        ]
        assert validate(quotes).errors == []


class TestAvailability:
    def test_a_source_suspending_almost_everything_is_an_error(self) -> None:
        """The observed failure mode: a status field derived from a timestamp
        rather than a boolean marked 98% of one book's rows suspended, which
        removed it from every cross-book comparison while its row count stayed
        healthy."""
        healthy = _complete_book("book_a")
        for index in range(4):
            healthy += _complete_book("book_a", event_key=f"MLB-AA{index}@MLB-BB{index}:2026-07-28")
        suspended = _complete_book("book_b", status=QuoteStatus.SUSPENDED)
        for index in range(4):
            suspended += _complete_book(
                "book_b",
                event_key=f"MLB-AA{index}@MLB-BB{index}:2026-07-28",
                status=QuoteStatus.SUSPENDED,
            )
        report = validate([*healthy, *suspended])
        assert "implausible_suspension_rate" in _codes(report, Severity.ERROR)

    def test_it_still_fires_when_the_broken_sources_are_the_majority(self) -> None:
        """The case a median-based reference went silent on.

        Two sources at 100% suspended against one open source produced **zero**
        findings once the reference was the median, because the broken pair *were*
        the middle.  That is the shape of a shared fault — five order-driven
        adapters deriving ``status`` the same wrong way — and it is precisely when
        the check has to speak, not when it is safe for it to stop.
        """
        quotes = []
        for index in range(5):
            event = f"MLB-AA{index}@MLB-BB{index}:2026-07-28"
            quotes += _complete_book("open_book", event_key=event)
            for broken in ("broken_a", "broken_b"):
                quotes += _complete_book(
                    broken, event_key=event, status=QuoteStatus.SUSPENDED
                )
        report = validate(quotes)
        flagged = {
            finding.source
            for finding in report.errors
            if finding.code == "implausible_suspension_rate"
        }
        assert flagged == {"broken_a", "broken_b"}, "every offender is named, not just one"

    def test_every_offender_is_named_not_only_the_worst(self) -> None:
        """Naming ``max(rates)`` alone reported one broken source when several
        shared a fault, leaving the rest invisible behind it."""
        quotes = []
        for index in range(5):
            event = f"MLB-AA{index}@MLB-BB{index}:2026-07-28"
            for healthy in ("open_a", "open_b", "open_c"):
                quotes += _complete_book(healthy, event_key=event)
            for broken in ("broken_a", "broken_b"):
                quotes += _complete_book(
                    broken, event_key=event, status=QuoteStatus.SUSPENDED
                )
        report = validate(quotes)
        flagged = {
            finding.source
            for finding in report.errors
            if finding.code == "implausible_suspension_rate"
        }
        assert flagged == {"broken_a", "broken_b"}

    def test_a_slate_suspended_at_every_book_is_not_flagged(self) -> None:
        """Books genuinely close markets overnight.  Agreement is the signal
        that this is real rather than a parsing fault."""
        quotes = []
        for source in ("book_a", "book_b"):
            for index in range(5):
                quotes += _complete_book(
                    source,
                    event_key=f"MLB-AA{index}@MLB-BB{index}:2026-07-28",
                    status=QuoteStatus.SUSPENDED,
                )
        report = validate(quotes)
        assert "implausible_suspension_rate" not in _codes(report)

    def test_a_moderate_suspension_rate_is_not_flagged(self) -> None:
        quotes = []
        for source in ("book_a", "book_b"):
            for index in range(5):
                status = QuoteStatus.SUSPENDED if index < 2 else QuoteStatus.ACTIVE
                quotes += _complete_book(
                    source, event_key=f"MLB-AA{index}@MLB-BB{index}:2026-07-28", status=status
                )
        rate = 2 / 5
        assert rate < MAX_SUSPENDED_FRACTION
        assert "implausible_suspension_rate" not in _codes(validate(quotes))


class TestCrossSourceIdentity:
    def test_two_different_games_under_one_event_key_are_caught(self) -> None:
        """The doubleheader mis-join, seen from validation's side: book_b's rows
        carry game two's start time under game one's key."""
        quotes = _clean_slate()
        shifted = [
            q.model_copy(update={"commence_time": q.commence_time + timedelta(hours=3, minutes=30)})
            for q in quotes
            if q.source == "book_b"
        ]
        quotes = [q for q in quotes if q.source != "book_b"] + shifted
        report = validate(quotes)
        assert "start_time_disagreement" in _codes(report, Severity.WARNING)

    def test_a_one_minute_start_time_difference_is_tolerated(self) -> None:
        """Every captured FanDuel start time is a minute later than the others."""
        quotes = _clean_slate()
        shifted = [
            q.model_copy(update={"commence_time": q.commence_time + timedelta(minutes=1)})
            for q in quotes
            if q.source == "book_b"
        ]
        quotes = [q for q in quotes if q.source != "book_b"] + shifted
        assert "start_time_disagreement" not in _codes(validate(quotes))

    def test_a_tennis_estimate_hours_out_is_tolerated(self) -> None:
        """The tolerance that would be a mis-join in baseball is normal in tennis,
        where the start time is a "not before" estimate per book.  Using
        baseball's window here split the same match into two events, which is
        exactly the join that arbitrage depends on."""
        quotes = [
            *_book(TENNIS_MATCH, "book_a"),
            *[
                q.model_copy(update={"commence_time": q.commence_time + timedelta(hours=6)})
                for q in _book(TENNIS_MATCH, "book_b")
            ],
        ]
        assert "start_time_disagreement" not in _codes(validate(quotes))

    def test_a_home_away_swap_is_visible(self) -> None:
        """A swap moves the row to a different event key, so it surfaces as one
        book pricing a game nobody else prices rather than as a disagreement."""
        quotes = _clean_slate()
        swapped = [
            q.model_copy(
                update={
                    "home_team": q.away_team,
                    "away_team": q.home_team,
                    "home_participant": q.away_participant,
                    "away_participant": q.home_participant,
                    "event_key": "MLB-MIA@MLB-PHI:2026-07-28",
                }
            )
            for q in quotes
            if q.source == "book_b"
        ]
        quotes = [q for q in quotes if q.source != "book_b"] + swapped
        report = validate(quotes)
        assert "no_shared_events" in _codes(report, Severity.ERROR)

    def test_a_soccer_home_away_swap_is_reported_however_the_key_is_spelled(self) -> None:
        """Clubs have a home ground, so the two books cannot both be right.

        Reported whether or not the swapped book kept the other's key, because
        the realistic case is that it did *not*: reconciliation rebuilds the key
        from the participants each source reported, so a reversed book gets a key
        of its own and used to fall out of this comparison entirely.

        One fixture is a warning rather than an error — two books can legitimately
        disagree about the home side of a match on neutral ground, and with a
        single shared fixture there is no way to tell that from a broken adapter.
        The severity is decided on the rate; see
        ``TestAReversedBookIsNamedRatherThanLosingItsJoins``.
        """
        def swap(quote, keep_key: bool):
            update = {
                "home_participant": quote.away_participant,
                "away_participant": quote.home_participant,
                "home_team": quote.away_team,
                "away_team": quote.home_team,
            }
            if not keep_key:
                update["event_key"] = (
                    f"{quote.home_participant}@{quote.away_participant}"
                    f":{quote.event_key.partition(':')[2]}"
                )
            return quote.model_copy(update=update)

        for keep_key in (True, False):
            swapped = [swap(q, keep_key) for q in _book(SOCCER_GAME, "book_b")]
            quotes = [*_book(SOCCER_GAME, "book_a"), *swapped]
            codes = _codes(validate(quotes))
            assert "home_away_disagreement" in codes, keep_key

    def test_a_tennis_ordering_disagreement_is_not_a_home_away_disagreement(self) -> None:
        """Each book orders the two players however it likes and src.events.orient
        imposes its own order, so "which is home" is not a claim either book made.
        Reporting a disagreement there would be reporting on our own convention."""
        reversed_rows = [
            q.model_copy(
                update={
                    "home_participant": q.away_participant,
                    "away_participant": q.home_participant,
                    "home_team": q.away_team,
                    "away_team": q.home_team,
                }
            )
            for q in _book(TENNIS_MATCH, "book_b")
        ]
        quotes = [*_book(TENNIS_MATCH, "book_a"), *reversed_rows]
        codes = _codes(validate(quotes))
        assert "home_away_disagreement" not in codes
        # The pair is still recognised as one pair …
        assert "participant_pair_disagreement" not in codes
        # … and the non-canonical ordering is reported for what it is.
        assert "participant_order_not_canonical" in _codes(validate(quotes), Severity.ERROR)

    def test_two_books_naming_different_tennis_players_is_caught(self) -> None:
        """With no home/away check to lean on, the pair check is the only thing
        standing between two different matches joined under one key and a phantom
        arbitrage across them."""
        other = [
            q.model_copy(
                update={
                    "away_participant": "TENNIS-janniksinner",
                    "away_team": "Jannik Sinner",
                }
            )
            for q in _book(TENNIS_MATCH, "book_b")
        ]
        quotes = [*_book(TENNIS_MATCH, "book_a"), *other]
        assert "participant_pair_disagreement" in _codes(validate(quotes), Severity.ERROR)

    def test_a_display_name_that_names_another_club_is_caught(self) -> None:
        """The keys join and the names do not: one of the two is wrong, and if it
        is the key then this row's prices belong to a different game."""
        quotes = _clean_slate()
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                selection=Selection.HOME,
                home_team="Atlanta Braves",
                decimal_odds=1.91,
                source_market_id="book_a-namecheck",
            )
        )
        assert "participant_key_mismatch" in _codes(validate(quotes), Severity.ERROR)


class TestConflictingPrices:
    def test_two_prices_for_one_selection_is_an_error(self) -> None:
        quotes = _clean_slate()
        quotes.append(
            make_quote(
                source="book_a",
                market=Market.MONEYLINE,
                selection=Selection.HOME,
                decimal_odds=2.50,
                source_market_id="book_a-elsewhere",
            )
        )
        report = validate(quotes)
        assert "conflicting_duplicate" in _codes(report, Severity.ERROR)

    def test_a_primary_and_an_alternate_at_the_same_line_are_both_legitimate(self) -> None:
        """Books really do offer the same number twice, at different prices and
        limits.  Treating that as a duplicate reported real data as corrupt —
        and, because storage enforces the same key, aborted the whole run's
        insert."""
        quotes = _clean_slate()
        quotes += [
            make_quote(
                source="book_a",
                market=Market.TOTAL,
                selection=selection,
                line=8.5,
                decimal_odds=odds,
                is_alternate=True,
                source_market_id="book_a-alt-tot",
            )
            for selection, odds in ((Selection.OVER, 1.89), (Selection.UNDER, 1.97))
        ]
        report = validate(quotes)
        assert "conflicting_duplicate" not in _codes(report)
        assert report.errors == [], "\n".join(str(f) for f in report.errors)


class TestEmptyAndDegenerate:
    def test_no_quotes_at_all_is_an_error(self) -> None:
        report = validate([])
        assert not report.ok
        assert "no_quotes" in _codes(report)

    def test_a_single_source_slate_is_internally_valid_but_uncomparable(self) -> None:
        """Nothing is *wrong* with one book's rows, so validation passes them.
        The two-source requirement is a property of a run rather than of a row,
        and is enforced by the collector — asserted here so the division of
        responsibility is pinned rather than assumed."""
        report = validate(_complete_book("book_a"))
        assert report.ok
        assert report.source_count == 1

        from src.collector import _check_source_health
        from src.sources.base import SourceHealth

        health = [
            SourceHealth(
                source_key="book_a",
                ok=True,
                checked_at=datetime(2026, 7, 28, 7, 0, tzinfo=UTC),
                quote_count=6,
            )
        ]
        _check_source_health(health, report, configured=["book_a"])
        assert "insufficient_sources" in _codes(report, Severity.ERROR)
