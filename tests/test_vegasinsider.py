"""Offline pins for named sportsbook columns on VegasInsider."""
from __future__ import annotations

import glob

import pytest

from src.raw_store import RawStore
from src.schema import Market, Selection
from src.sources.vegasinsider import parse_vegasinsider


@pytest.mark.parametrize(
    "source",
    ["vi_draftkings", "vi_caesars", "vi_hardrock", "vi_fanatics", "vi_bet365"],
)
def test_real_mlb_capture_produces_complete_game_lines(source: str) -> None:
    path = glob.glob(f"tests/fixtures/raw/{source}__*.json")
    assert len(path) == 1
    raw = RawStore("tests/fixtures/raw").read(path[0])
    outcome = parse_vegasinsider([raw])
    assert not outcome.rejections
    assert len(outcome.quotes) == 48
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
