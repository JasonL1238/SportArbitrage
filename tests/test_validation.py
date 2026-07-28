"""Validation must actually catch corruption, not just pass clean data.

Every test here injects a specific fault into otherwise-valid rows and asserts
that the corresponding check fires.  Without these, a passing validation report
proves nothing.
"""
from __future__ import annotations

from datetime import timedelta

from src.schema import Market, Selection, Side
from src.validation import Severity, validate
from tests.conftest import make_quote


def _codes(quotes) -> set[str]:
    return {finding.code for finding in validate(quotes).findings}


def _two_source_pair(**overrides):
    """A minimal clean slate: one market priced by two books."""
    base = [
        make_quote(source="bookA", selection=Selection.HOME, decimal_odds=1.9, american_odds=-111),
        make_quote(source="bookA", selection=Selection.AWAY, decimal_odds=2.05, american_odds=105),
        make_quote(source="bookB", selection=Selection.HOME, decimal_odds=1.95, american_odds=-105),
        make_quote(source="bookB", selection=Selection.AWAY, decimal_odds=2.0, american_odds=100),
    ]
    return base


def test_clean_slate_passes_except_for_expected_coverage_gap() -> None:
    """The minimal slate only has moneylines, so the coverage check must flag
    the missing core markets — proving that check is live."""
    report = validate(_two_source_pair())
    assert {f.code for f in report.errors} == {"core_market_absent"}


def _full_slate(source: str, *, home_odds: float = 1.9) -> list:
    """One book pricing all three core full-game markets."""
    return [
        make_quote(source=source, market=Market.MONEYLINE, selection=Selection.HOME,
                   decimal_odds=home_odds, american_odds=-111, source_market_id=f"{source}-ml"),
        make_quote(source=source, market=Market.MONEYLINE, selection=Selection.AWAY,
                   decimal_odds=2.05, american_odds=105, source_market_id=f"{source}-ml"),
        make_quote(source=source, market=Market.RUN_LINE, selection=Selection.HOME, line=1.5,
                   decimal_odds=1.5, american_odds=-200, source_market_id=f"{source}-rl"),
        make_quote(source=source, market=Market.RUN_LINE, selection=Selection.AWAY, line=-1.5,
                   decimal_odds=2.6, american_odds=160, source_market_id=f"{source}-rl"),
        make_quote(source=source, market=Market.TOTAL_RUNS, selection=Selection.OVER, line=8.5,
                   decimal_odds=1.92, american_odds=-108, source_market_id=f"{source}-tot"),
        make_quote(source=source, market=Market.TOTAL_RUNS, selection=Selection.UNDER, line=8.5,
                   decimal_odds=1.9, american_odds=-111, source_market_id=f"{source}-tot"),
    ]


def test_two_book_full_slate_is_clean() -> None:
    report = validate(_full_slate("bookA") + _full_slate("bookB"))
    assert report.ok, [str(f) for f in report.errors]


def test_negative_overround_is_an_error() -> None:
    """Both sides above evens means the book priced itself to lose, which in
    practice means the parser mispaired prices or lines."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes[0] = make_quote(
        source="bookA", market=Market.MONEYLINE, selection=Selection.HOME,
        decimal_odds=2.6, american_odds=160, source_market_id="bookA-ml",
    )
    assert "negative_overround" in _codes(quotes)


def test_unmirrored_run_line_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes[3] = make_quote(
        source="bookA", market=Market.RUN_LINE, selection=Selection.AWAY, line=1.5,
        decimal_odds=2.6, american_odds=160, source_market_id="bookA-rl",
    )
    assert "run_line_not_mirrored" in _codes(quotes)


def test_mismatched_total_lines_within_one_market_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes[5] = make_quote(
        source="bookA", market=Market.TOTAL_RUNS, selection=Selection.UNDER, line=9.5,
        decimal_odds=1.9, american_odds=-111, source_market_id="bookA-tot",
    )
    assert "line_mismatch_within_market" in _codes(quotes)


def test_home_away_disagreement_across_sources_is_an_error() -> None:
    """If two books disagree about which team is home, the join is wrong."""
    quotes = _full_slate("bookA") + [
        make_quote(source="bookB", home_team="Philadelphia Phillies",
                   away_team="Miami Marlins", source_market_id="bookB-ml")
    ]
    assert "home_away_disagreement" in _codes(quotes)


def test_start_time_disagreement_across_sources_is_an_error() -> None:
    quotes = _full_slate("bookA")
    late = quotes[0].commence_time + timedelta(hours=3)
    quotes.append(make_quote(source="bookB", commence_time=late, source_market_id="bookB-ml"))
    assert "start_time_disagreement" in _codes(quotes)


def test_conflicting_duplicate_is_an_error() -> None:
    """Two different prices for one selection — how pitcher-conditional
    moneylines leak in as the moneyline."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", market=Market.MONEYLINE, selection=Selection.HOME,
                   decimal_odds=1.7, american_odds=-143, source_market_id="bookA-ml-conditional")
    )
    assert "conflicting_duplicate" in _codes(quotes)


def test_identical_duplicate_is_only_a_warning() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(quotes[0])
    report = validate(quotes)
    assert "duplicate_quote" in {f.code for f in report.warnings}
    assert "conflicting_duplicate" not in {f.code for f in report.errors}


def test_futures_style_commence_time_is_an_error() -> None:
    """FanDuel futures containers carry openDate 2030/2099; a game does not."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    far = quotes[0].observed_at + timedelta(days=400)
    quotes.append(make_quote(source="bookA", commence_time=far, source_market_id="bookA-fut"))
    assert "commence_time_too_far" in _codes(quotes)


def test_implausible_line_is_an_error() -> None:
    """The Kambi thousandths bug produced a total of 8000 runs."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", market=Market.TOTAL_RUNS, selection=Selection.OVER,
                   line=8000.0, decimal_odds=1.9, american_odds=-111, source_market_id="bookA-bad")
    )
    assert "line_out_of_range" in _codes(quotes)


def test_source_reported_change_time_after_observation_is_an_error() -> None:
    """Pinnacle's cutoffAt is in the future; using it as a timestamp made fresh
    data look like it came from the future."""
    quotes = _full_slate("bookA") + _full_slate("bookB")
    future = quotes[0].observed_at + timedelta(hours=6)
    quotes.append(make_quote(source="bookA", last_change_at=future, source_market_id="bookA-x"))
    assert "last_change_in_future" in _codes(quotes)


def test_unknown_team_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(make_quote(source="bookA", home_team="MLB Futures", source_market_id="bookA-y"))
    assert "unknown_team" in _codes(quotes)


def test_odds_format_mismatch_is_an_error() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", decimal_odds=1.9, american_odds=500,
                   implied_probability=1 / 1.9, source_market_id="bookA-z")
    )
    assert "odds_format_mismatch" in _codes(quotes)


def test_implied_probability_must_match_decimal_odds() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes.append(
        make_quote(source="bookA", decimal_odds=1.9, american_odds=-111,
                   implied_probability=0.25, source_market_id="bookA-w")
    )
    assert "implied_probability_mismatch" in _codes(quotes)


def test_missing_core_market_is_an_error() -> None:
    """Catches an upstream rename that turns a real market into a silent skip."""
    quotes = [q for q in _full_slate("bookA") if q.market is not Market.TOTAL_RUNS]
    quotes += _full_slate("bookB")
    report = validate(quotes)
    codes = {f.code for f in report.errors}
    assert "core_market_absent" in codes


def test_no_shared_events_is_an_error() -> None:
    quotes = _full_slate("bookA")
    other = [
        make_quote(source="bookB", event_key="ATL@NYM:2026-07-28", home_team="New York Mets",
                   away_team="Atlanta Braves", market=q.market, period=q.period,
                   selection=q.selection, side=q.side, line=q.line,
                   decimal_odds=q.decimal_odds, american_odds=q.american_odds,
                   source_market_id=f"bookB-{q.market.value}")
        for q in _full_slate("bookB")
    ]
    assert "no_shared_events" in _codes(quotes + other)


def test_empty_input_is_an_error() -> None:
    report = validate([])
    assert not report.ok
    assert {f.code for f in report.errors} == {"no_quotes"}


def test_team_total_requires_a_side() -> None:
    quotes = _full_slate("bookA") + _full_slate("bookB")
    quotes += [
        make_quote(source="bookA", market=Market.TEAM_TOTAL_RUNS, selection=Selection.OVER,
                   side=Side.HOME, line=4.5, decimal_odds=1.95, american_odds=-105,
                   source_market_id="bookA-tt"),
        make_quote(source="bookA", market=Market.TEAM_TOTAL_RUNS, selection=Selection.UNDER,
                   side=Side.HOME, line=4.5, decimal_odds=1.9, american_odds=-111,
                   source_market_id="bookA-tt"),
    ]
    report = validate(quotes)
    assert report.ok, [str(f) for f in report.errors]


def test_report_severity_split_and_summary() -> None:
    report = validate(_full_slate("bookA") + _full_slate("bookB"))
    assert report.ok
    assert "PASS" in report.summary()
    assert all(f.severity in (Severity.ERROR, Severity.WARNING) for f in report.findings)
