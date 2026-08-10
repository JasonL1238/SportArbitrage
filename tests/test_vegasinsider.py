"""Offline pins for named sportsbook columns on VegasInsider."""
from __future__ import annotations

import glob

import pytest

from src.raw_store import RawStore
from src.schema import Market, Selection
from src.sources.vegasinsider import parse_vegasinsider


#: The 2026-08-03 page carries 8 games × 3 markets × 2 sides = 48 rows per
#: column.  ``vi_hardrock`` publishes four fewer, both drops mid-move pairings
#: no book offered at once: its LAD@CHC total read ``o8.5 +110 / u8.5 −105``
#: (implied sum 0.9884) and its WSH@PHI spread read ``home −1.5 +145 /
#: away +1.5 −140`` (implied sum 0.9915 — the pair the first synthesized
#: ``market_key`` tore in half and therefore never judged).
#: ``refuse_mid_move_pairings`` drops all four legs and counts them.
@pytest.mark.parametrize(
    ("source", "expected", "dropped"),
    [
        ("vi_draftkings", 48, 0),
        ("vi_caesars", 48, 0),
        ("vi_hardrock", 44, 4),
        ("vi_fanatics", 48, 0),
        ("vi_bet365", 48, 0),
    ],
)
def test_real_mlb_capture_produces_complete_game_lines(
    source: str, expected: int, dropped: int
) -> None:
    path = glob.glob(f"tests/fixtures/raw/{source}__*.json")
    assert len(path) == 1
    raw = RawStore("tests/fixtures/raw").read(path[0])
    outcome = parse_vegasinsider([raw])
    assert not outcome.rejections
    assert len(outcome.quotes) == expected
    assert outcome.skipped["market_prices_the_book_to_lose"] == dropped
    assert {quote.source for quote in outcome.quotes} == {source}
    assert {quote.market for quote in outcome.quotes} == {
        Market.MONEYLINE, Market.SPREAD, Market.TOTAL,
    }


def test_line_direction_and_total_side_survive_html_parsing() -> None:
    path = glob.glob("tests/fixtures/raw/vi_draftkings__*.json")
    raw = RawStore("tests/fixtures/raw").read(path[0])
    quotes = parse_vegasinsider([raw]).quotes
    first_event = quotes[0].event_key
    by_market_selection = {
        (quote.market, quote.selection): quote
        for quote in quotes if quote.event_key == first_event
    }
    assert by_market_selection[Market.SPREAD, Selection.AWAY].line == 1.5
    assert by_market_selection[Market.SPREAD, Selection.HOME].line == -1.5
    assert by_market_selection[Market.TOTAL, Selection.OVER].line == 9.5
    assert by_market_selection[Market.TOTAL, Selection.UNDER].line == 9.5
