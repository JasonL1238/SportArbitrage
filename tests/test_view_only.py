"""View-only sources (AN Open) — shown, never bet against, never best-priced."""
from __future__ import annotations

from src.arb import best_prices, counterparty_groups, find_opportunities
from src.distinctness import compare_all
from src.report import _source_entry
from src.schema import Selection
from src.sources.registry import VIEW_ONLY_SOURCES, is_view_only, keys
from tests.conftest import make_quote


def test_an_open_is_registered_as_view_only() -> None:
    assert "an_open" in keys()
    assert "an_open" in VIEW_ONLY_SOURCES
    assert is_view_only("an_open")
    assert not is_view_only("fanduel")
    assert _source_entry("an_open")["view_only"] is True
    assert _source_entry("fanduel")["view_only"] is False


def test_view_only_is_invisible_to_distinctness() -> None:
    """A consensus feed that agrees with A and B must not union-find A with B."""
    event = "MLB-PHI@MLB-MIA:2026-07-28"
    quotes = []
    for source, home, away in (
        ("an_open", 1.90, 1.90),
        ("fanduel", 1.90, 1.90),
        ("pinnacle", 1.90, 1.90),
    ):
        quotes.append(
            make_quote(
                source=source,
                event_key=event,
                selection=Selection.HOME,
                decimal_odds=home,
            )
        )
        quotes.append(
            make_quote(
                source=source,
                event_key=event,
                selection=Selection.AWAY,
                decimal_odds=away,
            )
        )
    pairs = compare_all(quotes)
    involved = {pair.source_a for pair in pairs} | {pair.source_b for pair in pairs}
    assert "an_open" not in involved
    groups = counterparty_groups(quotes)
    for bucket in groups.values():
        for group in bucket:
            assert "an_open" not in group


def test_open_plus_one_book_does_not_create_arb() -> None:
    # Prices that would be a clear arb if Open counted as a book.
    quotes = [
        make_quote(source="an_open", selection=Selection.HOME, decimal_odds=2.20),
        make_quote(source="an_open", selection=Selection.AWAY, decimal_odds=2.20),
        make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.80),
        make_quote(source="fanduel", selection=Selection.AWAY, decimal_odds=2.05),
    ]
    report = find_opportunities(quotes)
    assert not report.opportunities
    for selections in best_prices(quotes).values():
        assert all(q.source != "an_open" for q in selections.values())


def test_juicy_open_does_not_win_best_prices() -> None:
    quotes = [
        make_quote(source="an_open", selection=Selection.HOME, decimal_odds=5.0),
        make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.91),
        make_quote(source="pinnacle", selection=Selection.HOME, decimal_odds=1.95),
    ]
    surface = best_prices(quotes)
    assert surface
    for selections in surface.values():
        assert selections[Selection.HOME].source == "pinnacle"
