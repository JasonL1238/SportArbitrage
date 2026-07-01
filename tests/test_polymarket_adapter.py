from __future__ import annotations

import pytest

from src.sources.base import QuoteSourceAdapter
from src.sources.polymarket import (
    PolymarketQuoteAdapter,
    parse_polymarket_gamma_quotes,
    parse_polymarket_quotes,
)


def test_polymarket_quote_adapter_satisfies_protocol():
    adapter = PolymarketQuoteAdapter()
    assert isinstance(adapter, QuoteSourceAdapter)
    adapter.close()


def test_parse_polymarket_quotes_from_order_book():
    markets = [
        {
            "condition_id": "cond_1",
            "event_id": "evt_1",
            "question": "Will the Lakers beat the Celtics?",
            "market_slug": "nba-lakers-celtics",
            "active": True,
            "closed": False,
            "accepting_orders": True,
            "game_start_time": "2026-07-02T01:00:00Z",
            "tokens": [
                {"token_id": "yes_token", "outcome": "YES", "price": 0.55},
                {"token_id": "no_token", "outcome": "NO", "price": 0.45},
            ],
            "tags": ["sports", "nba"],
        }
    ]
    books = [
        {
            "asset_id": "yes_token",
            "market": "cond_1",
            "timestamp": 1782954000000,
            "bids": [{"price": "0.53", "size": "20"}],
            "asks": [{"price": "0.56", "size": "42"}, {"price": "0.57", "size": "100"}],
        },
        {
            "asset_id": "no_token",
            "market": "cond_1",
            "timestamp": 1782954000000,
            "bids": [{"price": "0.42", "size": "20"}],
            "asks": [{"price": "0.46", "size": "75"}],
        },
    ]

    quotes = parse_polymarket_quotes(markets, books)

    assert len(quotes) == 2
    yes = next(q for q in quotes if q.selection == "YES")
    assert yes.source == "polymarket"
    assert yes.sport == "basketball_nba"
    assert yes.market_type == "moneyline"
    assert yes.source_market_id == "cond_1"
    assert yes.source_selection_id == "yes_token"
    assert yes.price == pytest.approx(0.56)
    assert yes.decimal_odds == pytest.approx(1 / 0.56)
    assert yes.implied_probability == pytest.approx(0.56)
    assert yes.liquidity == pytest.approx(42.0)
    assert yes.event_start_time is not None


def test_parse_polymarket_quotes_skips_missing_asks():
    quotes = parse_polymarket_quotes(
        [{"condition_id": "cond_1", "tokens": [{"token_id": "yes", "outcome": "YES"}]}],
        [{"asset_id": "yes", "bids": [{"price": "0.4", "size": "1"}], "asks": []}],
    )

    assert quotes == []


def test_parse_polymarket_gamma_sports_quotes():
    events = [
        {
            "id": "evt_1",
            "slug": "world-cup-winner",
            "title": "World Cup Winner",
            "active": True,
            "closed": False,
            "endDate": "2026-07-20T00:00:00Z",
            "tags": [{"label": "Sports"}, {"label": "Soccer"}],
            "markets": [
                {
                    "id": "mkt_1",
                    "conditionId": "cond_1",
                    "question": "Will Spain win the 2026 FIFA World Cup?",
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "outcomes": "[\"Yes\", \"No\"]",
                    "outcomePrices": "[\"0.1005\", \"0.8995\"]",
                    "clobTokenIds": "[\"yes_token\", \"no_token\"]",
                    "liquidityNum": 1234.5,
                    "updatedAt": "2026-07-01T19:14:57.214237Z",
                }
            ],
        }
    ]

    quotes = parse_polymarket_gamma_quotes(events)

    assert len(quotes) == 2
    yes = next(q for q in quotes if q.selection == "Yes")
    assert yes.source == "polymarket"
    assert yes.sport == "soccer"
    assert yes.market_type == "futures"
    assert yes.source_event_id == "evt_1"
    assert yes.source_market_id == "cond_1"
    assert yes.source_selection_id == "yes_token"
    assert yes.price == pytest.approx(0.1005)
    assert yes.decimal_odds == pytest.approx(1 / 0.1005)
    assert yes.liquidity == pytest.approx(1234.5)
    assert yes.market_confidence is not None and yes.market_confidence > 0.7
