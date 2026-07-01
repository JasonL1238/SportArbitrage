from __future__ import annotations

import json
from pathlib import Path

from scripts.smoke_sources import (
    _validate_espn_quote_values,
    _validate_kalshi_quote_values,
    _validate_polymarket_gamma_quote_values,
    _validate_polymarket_quote_values,
    _validate_quotes,
)
from src.quotes import quotes_from_events
from src.sources.espn_odds import EspnOddsAdapter
from src.sources.kalshi import parse_kalshi_quotes
from src.sources.polymarket import parse_polymarket_gamma_quotes, parse_polymarket_quotes

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "source_smoke"


def test_espn_smoke_fixture_parses_to_price_quotes():
    raw = json.loads((FIXTURE_DIR / "espn_odds_latest.json").read_text(encoding="utf-8"))
    adapter = EspnOddsAdapter()
    try:
        events = adapter.parse_events(raw)
    finally:
        adapter.close()

    quotes = quotes_from_events(
        events,
        source="espn_odds",
        raw_payload_ref="tests/fixtures/source_smoke/espn_odds_latest.json",
    )

    _validate_quotes("espn_odds", quotes)
    _validate_espn_quote_values(raw, quotes)
    assert {q.source for q in quotes} == {"espn_odds"}
    assert {q.sport for q in quotes} == {"baseball_mlb"}


def test_polymarket_smoke_fixture_parses_to_price_quotes():
    raw = json.loads((FIXTURE_DIR / "polymarket_latest.json").read_text(encoding="utf-8"))
    if "gamma_events" in raw:
        quotes = parse_polymarket_gamma_quotes(raw["gamma_events"])
        _validate_polymarket_gamma_quote_values(raw["gamma_events"], quotes)
    else:
        quotes = parse_polymarket_quotes(raw["markets"], raw["books"])
        _validate_polymarket_quote_values(raw["books"], quotes)

    _validate_quotes("polymarket", quotes)
    assert {q.source for q in quotes} == {"polymarket"}
    assert all(q.source_market_id for q in quotes)
    assert all(q.sport != "prediction_market" for q in quotes)


def test_kalshi_smoke_fixture_parses_to_price_quotes():
    raw = json.loads((FIXTURE_DIR / "kalshi_latest.json").read_text(encoding="utf-8"))
    quotes = parse_kalshi_quotes(raw["markets"])

    _validate_quotes("kalshi", quotes)
    _validate_kalshi_quote_values(raw["markets"], quotes)
    assert {q.source for q in quotes} == {"kalshi"}
    assert all(q.source_market_id for q in quotes)
    assert all(q.source_selection_id for q in quotes)
