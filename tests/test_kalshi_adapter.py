from __future__ import annotations

import pytest

from src.sources.base import QuoteSourceAdapter
from src.sources.kalshi import KalshiQuoteAdapter, parse_kalshi_quotes


def test_kalshi_quote_adapter_satisfies_protocol():
    adapter = KalshiQuoteAdapter()
    assert isinstance(adapter, QuoteSourceAdapter)
    adapter.close()


def test_parse_kalshi_quotes_from_public_market_data():
    markets = [
        {
            "ticker": "KXMLBGAME-26JUL011507NYMTOR-NYM",
            "event_ticker": "KXMLBGAME-26JUL011507NYMTOR",
            "title": "New York M wins",
            "yes_sub_title": "New York M",
            "no_sub_title": "Toronto",
            "status": "active",
            "updated_time": "2026-07-01T19:09:28.402526Z",
            "expected_expiration_time": "2026-07-02T02:40:00Z",
            "yes_ask_dollars": "0.5600",
            "yes_ask_size_fp": "12.50",
            "no_ask_dollars": "0.4700",
            "no_ask_size_fp": "18.25",
            "liquidity_dollars": "200.00",
        }
    ]

    quotes = parse_kalshi_quotes(markets)

    assert len(quotes) == 2
    yes = next(q for q in quotes if q.selection == "YES")
    assert yes.source == "kalshi"
    assert yes.sport == "baseball_mlb"
    assert yes.source_event_id == "KXMLBGAME-26JUL011507NYMTOR"
    assert yes.source_market_id == "KXMLBGAME-26JUL011507NYMTOR-NYM"
    assert yes.price == pytest.approx(0.56)
    assert yes.decimal_odds == pytest.approx(1 / 0.56)
    assert yes.implied_probability == pytest.approx(0.56)
    assert yes.liquidity == pytest.approx(12.5)
    assert yes.timestamp.isoformat() == "2026-07-01T19:09:28.402526+00:00"


def test_parse_kalshi_quotes_skips_inactive_and_zero_prices():
    quotes = parse_kalshi_quotes(
        [
            {"ticker": "inactive", "status": "closed", "yes_ask_dollars": "0.5"},
            {
                "ticker": "active_zero",
                "event_ticker": "evt",
                "title": "Zero price",
                "status": "active",
                "yes_ask_dollars": "0.0000",
                "no_ask_dollars": "0.0000",
            },
        ]
    )

    assert quotes == []
