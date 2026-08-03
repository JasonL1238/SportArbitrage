"""Offline pins for VSiN's named Circa column."""
from __future__ import annotations

import glob

from src.raw_store import RawStore
from src.schema import Market, Selection
from src.sources.vsin import parse_vsin_circa


def _quotes():
    paths = glob.glob("tests/fixtures/raw/vsin_circa__*.json")
    assert len(paths) == 1
    raw = RawStore("tests/fixtures/raw").read(paths[0])
    outcome = parse_vsin_circa([raw])
    assert not outcome.rejections
    return outcome.quotes


def test_real_mlb_capture_produces_circa_game_lines() -> None:
    quotes = _quotes()
    assert len(quotes) == 48
    assert len({quote.event_key for quote in quotes}) == 8
    assert {quote.source for quote in quotes} == {"vsin_circa"}
    assert {quote.market for quote in quotes} == {
        Market.MONEYLINE,
        Market.SPREAD,
        Market.TOTAL,
    }


def test_circa_line_direction_and_total_side_survive_html_parsing() -> None:
    quotes = _quotes()
    first_event = quotes[0].event_key
    by_market_selection = {
        (quote.market, quote.selection): quote
        for quote in quotes
        if quote.event_key == first_event
    }
    assert by_market_selection[Market.SPREAD, Selection.AWAY].line == 1.5
    assert by_market_selection[Market.SPREAD, Selection.HOME].line == -1.5
    assert by_market_selection[Market.TOTAL, Selection.OVER].line == 9.0
    assert by_market_selection[Market.TOTAL, Selection.UNDER].line == 9.0
