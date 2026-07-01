from __future__ import annotations

from src.market_decomposer import decompose_kalshi_market, decompose_market_text


def test_decompose_polymarket_futures_question():
    decomp = decompose_market_text(
        "Will Spain win the 2026 FIFA World Cup?",
        source="polymarket",
    )

    assert decomp.sport == "soccer"
    assert decomp.market_kind == "futures"
    assert decomp.participant == "Spain"
    assert "2026 FIFA World Cup" in (decomp.counterparty or "")
    assert decomp.confidence > 0.7
    assert decomp.is_reliable


def test_decompose_polymarket_moneyline_question():
    decomp = decompose_market_text(
        "Will the Lakers beat the Celtics?",
        source="polymarket",
    )

    assert decomp.sport == "basketball_nba"
    assert decomp.market_kind == "moneyline"
    assert decomp.participant == "the Lakers"
    assert decomp.counterparty == "the Celtics"


def test_decompose_kalshi_composite_market_marks_low_confidence():
    decomp = decompose_kalshi_market(
        {
            "title": "yes Belgium advances,yes 8+ corners,yes Charles De Ketelaere: 1+,yes Jeremy Doku: 1+",
            "yes_sub_title": "yes Belgium advances,yes 8+ corners,yes Charles De Ketelaere: 1+,yes Jeremy Doku: 1+",
            "ticker": "KXMVECROSSCATEGORY-SAMPLE",
        }
    )

    assert decomp.is_composite
    assert decomp.market_kind == "composite"
    assert len(decomp.legs) == 4
    assert decomp.confidence < 0.7
    assert decomp.notes == "composite:4"


def test_decompose_kalshi_single_clause_future_market():
    decomp = decompose_kalshi_market(
        {
            "title": "New York M wins",
            "yes_sub_title": "New York M",
            "no_sub_title": "Toronto",
            "ticker": "KXMLBGAME-SAMPLE",
        }
    )

    assert not decomp.is_composite
    assert decomp.market_kind == "futures"
    assert decomp.participant == "New York M"
    assert decomp.confidence > 0.5
