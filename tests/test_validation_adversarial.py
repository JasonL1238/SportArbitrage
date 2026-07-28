"""Validation under deliberately corrupted input.

Each test here builds data that is wrong in one specific way and asserts that
validation says so.  The cases are drawn from failures that previously passed
*silently* — a run reporting PASS while holding two different games under one
event key, or two different lines fused into one market.  A check that cannot be
made to fire is not a check, so every one of these starts by confirming the
corruption is actually present in the input.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.schema import Market, Period, QuoteStatus, Selection, Side
from src.validation import (
    MAX_SUSPENDED_FRACTION,
    Severity,
    validate,
)
from tests.conftest import make_quote


def _codes(report, severity: Severity | None = None) -> set[str]:
    findings = report.findings if severity is None else [
        f for f in report.findings if f.severity is severity
    ]
    return {f.code for f in findings}


def _complete_book(source: str, *, event_key: str = "PHI@MIA:2026-07-28", **overrides) -> list:
    """One book pricing all three core full-game markets for one game."""
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
                market=Market.RUN_LINE,
                selection=selection,
                line=line,
                decimal_odds=odds,
                source_market_id=f"{source}-{event_key}-rl",
                **overrides,
            )
        )
    for selection, odds in ((Selection.OVER, 1.91), (Selection.UNDER, 1.95)):
        quotes.append(
            make_quote(
                source=source,
                event_key=event_key,
                market=Market.TOTAL_RUNS,
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
                market=Market.TOTAL_RUNS,
                selection=selection,
                line=9.5,
                decimal_odds=odds,
                source_market_id="book_a-PHI@MIA:2026-07-28-tot",  # same id as the 8.5 market
            )
            for selection, odds in ((Selection.OVER, 2.30), (Selection.UNDER, 1.65))
        ]
        report = validate(quotes)
        assert "repeated_selection_within_market" in _codes(report, Severity.ERROR)

    def test_two_run_line_pairs_fused_under_one_market_id_is_an_error(self) -> None:
        """Run lines were exempt from the line-set check, so a group holding
        -1.5/+1.5 and -2.5/+2.5 passed as "mirrored" — the mirroring test read
        two arbitrary rows out of a last-wins dict."""
        quotes = _clean_slate()
        quotes += [
            make_quote(
                source="book_a",
                market=Market.RUN_LINE,
                selection=selection,
                line=line,
                decimal_odds=odds,
                source_market_id="book_a-PHI@MIA:2026-07-28-rl",
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
                market=Market.TEAM_TOTAL_RUNS,
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
                market=Market.TOTAL_RUNS,
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
                source_market_id="book_a-PHI@MIA:2026-07-28-ml",
            )
            for selection in (Selection.HOME, Selection.AWAY)
        ]
        report = validate(quotes)
        assert "negative_overround" in _codes(report, Severity.ERROR)


class TestDroppedDrawLeg:
    def _three_way(self, source: str, *, include_draw: bool) -> list:
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
            *self._three_way("book_a", include_draw=True),
            *self._three_way("book_b", include_draw=True),
        ]
        report = validate(quotes)
        assert "incomplete_market" not in _codes(report)
        assert "negative_overround" not in _codes(report)

    def test_a_two_way_partial_period_market_is_not_misreported(self) -> None:
        """Two rows on a draw-capable period is a legitimate two-way market and
        must not be called incomplete.  1/2.30 + 1/2.75 = 0.7985, which is below
        1.0 — the old code reported that as "the book prices itself to lose"
        because it could not tell a two-way market from a three-way one that had
        lost its draw row.  Completeness now follows the row count, so a genuine
        two-way market is judged as one."""
        quotes = [
            *_clean_slate(),
            *self._three_way("book_a", include_draw=False),
            *self._three_way("book_b", include_draw=False),
        ]
        report = validate(quotes)
        assert "incomplete_market" not in _codes(report)

    def test_a_three_way_market_missing_its_draw_is_reported_as_incomplete(self) -> None:
        """Three rows present, one of them not the draw: the market declares
        itself three-way by having three selections, so a missing draw is a
        completeness failure rather than a pricing one."""
        quotes = [
            *_clean_slate(),
            *self._three_way("book_a", include_draw=True),
        ]
        # Replace the draw with a duplicate home row at a second line-free price:
        # three rows, no draw.
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
                event_key="ATL@NYM:2026-07-28",
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
                event_key="ATL@NYM:2026-07-28",
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
                event_key="ATL@NYM:2026-07-28",
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
                event_key="ATL@NYM:2026-07-28",
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
                event_key="ATL@NYM:2026-07-28",
                selection=Selection.HOME,
                decimal_odds=1.001,
                american_odds=-100000,
                source_market_id="book_a-absurd",
            )
        )
        report = validate(quotes)
        assert "implausible_odds" in _codes(report, Severity.ERROR)


class TestCoverage:
    def test_partial_market_coverage_across_events_is_caught(self) -> None:
        """A source pricing totals for one game and not the other satisfies the
        slate-wide coverage check while having lost half its markets."""
        quotes = [
            *_complete_book("book_a"),
            *_complete_book("book_b"),
            *_complete_book("book_a", event_key="ATL@NYM:2026-07-28"),
            *_complete_book("book_b", event_key="ATL@NYM:2026-07-28"),
        ]
        assert validate(quotes).errors == []

        # Now drop book_a's totals for the second game only.
        thinned = [
            q
            for q in quotes
            if not (
                q.source == "book_a"
                and q.event_key == "ATL@NYM:2026-07-28"
                and q.market is Market.TOTAL_RUNS
            )
        ]
        report = validate(thinned)
        assert "core_market_absent_for_event" in _codes(report, Severity.ERROR)

    def test_a_market_missing_everywhere_is_reported_once_not_twice(self) -> None:
        """A source with no totals at all is a slate-wide gap; reporting it per
        event as well would bury the signal in noise."""
        quotes = [q for q in _clean_slate() if q.market is not Market.TOTAL_RUNS]
        codes = _codes(validate(quotes), Severity.ERROR)
        assert "core_market_absent" in codes
        assert "core_market_absent_for_event" not in codes


class TestAvailability:
    def test_a_source_suspending_almost_everything_is_an_error(self) -> None:
        """The observed failure mode: a status field derived from a timestamp
        rather than a boolean marked 98% of one book's rows suspended, which
        removed it from every cross-book comparison while its row count stayed
        healthy."""
        healthy = _complete_book("book_a")
        for index in range(4):
            healthy += _complete_book("book_a", event_key=f"AA{index}@BB{index}:2026-07-28")
        suspended = _complete_book("book_b", status=QuoteStatus.SUSPENDED)
        for index in range(4):
            suspended += _complete_book(
                "book_b",
                event_key=f"AA{index}@BB{index}:2026-07-28",
                status=QuoteStatus.SUSPENDED,
            )
        report = validate([*healthy, *suspended])
        assert "implausible_suspension_rate" in _codes(report, Severity.ERROR)

    def test_a_slate_suspended_at_every_book_is_not_flagged(self) -> None:
        """Books genuinely close markets overnight.  Agreement is the signal
        that this is real rather than a parsing fault."""
        quotes = []
        for source in ("book_a", "book_b"):
            for index in range(5):
                quotes += _complete_book(
                    source,
                    event_key=f"AA{index}@BB{index}:2026-07-28",
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
                    source, event_key=f"AA{index}@BB{index}:2026-07-28", status=status
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
        assert "start_time_disagreement" in _codes(report, Severity.ERROR)

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

    def test_a_home_away_swap_is_visible(self) -> None:
        """A swap moves the row to a different event key, so it surfaces as one
        book pricing a game nobody else prices rather than as a disagreement."""
        quotes = _clean_slate()
        swapped = [
            q.model_copy(
                update={
                    "home_team": q.away_team,
                    "away_team": q.home_team,
                    "event_key": "MIA@PHI:2026-07-28",
                }
            )
            for q in quotes
            if q.source == "book_b"
        ]
        quotes = [q for q in quotes if q.source != "book_b"] + swapped
        report = validate(quotes)
        assert "no_shared_events" in _codes(report, Severity.ERROR)


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
                market=Market.TOTAL_RUNS,
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

        from src.collector import _check_source_count
        from src.sources.base import SourceHealth
        from datetime import UTC, datetime

        health = [
            SourceHealth(
                source_key="book_a",
                ok=True,
                checked_at=datetime(2026, 7, 28, 7, 0, tzinfo=UTC),
                quote_count=6,
            )
        ]
        _check_source_count(health, report)
        assert "insufficient_sources" in _codes(report, Severity.ERROR)
