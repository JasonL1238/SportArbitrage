from __future__ import annotations

import pytest

from src.quotes import quotes_from_events


def test_quotes_from_events_flattens_shared_schema(two_way_arb_event):
    quotes = quotes_from_events([two_way_arb_event], source="unit")

    assert len(quotes) == 4
    first = quotes[0]
    assert first.source == "unit"
    assert first.sport == "basketball_nba"
    assert first.event_name == "Team B @ Team A"
    assert first.market_type == "moneyline"
    assert first.decimal_odds is not None
    assert first.implied_probability == pytest.approx(1 / first.decimal_odds)
    assert first.source_event_id == "evt_1"


def test_quotes_preserve_line_liquidity_and_status(totals_arb_event):
    totals_arb_event.bookmakers[0].markets[0].outcomes[0].liquidity = 125.0
    totals_arb_event.bookmakers[0].markets[0].outcomes[0].status = "suspended"

    quotes = quotes_from_events([totals_arb_event], source="unit")
    over = next(q for q in quotes if q.selection == "Over")

    assert over.market_type == "totals"
    assert over.line == 215.5
    assert over.liquidity == 125.0
    assert over.status == "suspended"
