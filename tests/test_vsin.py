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


def test_the_mid_move_guard_is_wired_into_this_parser(monkeypatch) -> None:
    """The guard call in ``parse_vsin_circa`` is load-bearing and unpinnable
    by fixture arithmetic alone.

    The committed capture carries no sub-fair pairing — every multi-row market
    sums ≥ +2.55% over fair — so the guard never fires on it, and an
    adversarial probe demonstrated that deleting the call left the entire
    suite green while Circa's Las Vegas column silently resumed publishing
    mid-move pairings.  (Contrast the VegasInsider wire, which the vi_hardrock
    40-rows/8-counted pin holds.)  This pin asserts the wiring itself: the
    parser hands its finished outcome to ``refuse_mid_move_pairings`` before
    returning.
    """
    import src.sources.vsin as vsin_mod

    calls: list[tuple[str, int]] = []
    real = vsin_mod.refuse_mid_move_pairings

    def recorder(source, outcome):
        calls.append((source, len(outcome.quotes)))
        return real(source, outcome)

    monkeypatch.setattr(vsin_mod, "refuse_mid_move_pairings", recorder)
    quotes = _quotes()
    assert calls == [("vsin_circa", 48)], calls
    assert len(quotes) == 48


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
