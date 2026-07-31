"""Unit tests for the promotions / free-EV scraper package."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src.promos.classify import classify_kind
from src.promos.cloudbet import CloudbetPromoAdapter
from src.promos.draftkings import DraftKingsPromoAdapter
from src.promos.registry import PROMO_SOURCES, keys
from src.promos.schema import PromoKind, PromoOffer
from src.promos.store import PromoStore
from src.raw_store import RawResponse

FIXTURES = Path(__file__).parent / "fixtures" / "promos"


def _raw(source: str, endpoint: str, body: str, *, url: str = "https://example.test") -> RawResponse:
    return RawResponse(
        source=source,
        endpoint=endpoint,
        url=url,
        status_code=200,
        body=body,
        fetched_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        content_type="application/json",
    )


def test_classify_kind_rules() -> None:
    assert classify_kind("MLB Home Run No Sweat") is PromoKind.NO_SWEAT
    assert classify_kind("MLB SGP(x) Boost", "Profit Boost") is PromoKind.PROFIT_BOOST
    assert classify_kind("Refer a Friend!") is PromoKind.REFERRAL
    assert classify_kind("Sports Welcome Bonus") is PromoKind.SIGNUP_BONUS
    assert classify_kind("Referee prop boost") is not PromoKind.REFERRAL
    assert classify_kind("Get preferred pricing") is not PromoKind.REFERRAL


def test_registry_covers_major_odds_books() -> None:
    registered = set(keys())
    for key in (
        "fanduel",
        "draftkings",
        "betmgm",
        "betrivers_kambi",
        "bovada",
        "cloudbet",
        "caesars",
        "bet365",
        "pinnacle",
        "onexbet",
        "leovegas_kambi",
    ):
        assert key in registered
    # Every descriptor accepts source_key via the factory partial.
    for entry in PROMO_SOURCES:
        source = entry.factory()()
        assert source.source_key == entry.key
        source.close()


def test_draftkings_parse_fixture() -> None:
    path = FIXTURES / "draftkings_promotions_query.json"
    if not path.exists():
        pytest.skip("fixture not captured yet")
    body = path.read_text()
    adapter = DraftKingsPromoAdapter(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    outcome = adapter.parse([_raw("draftkings", "promotions-query", body)])
    adapter.close()
    assert outcome.offers
    assert all(o.source == "draftkings" for o in outcome.offers)
    kinds = {o.kind for o in outcome.offers}
    assert PromoKind.NO_SWEAT in kinds or PromoKind.PROFIT_BOOST in kinds or PromoKind.PARLAY_BOOST in kinds


def test_cloudbet_parse_fixture() -> None:
    path = FIXTURES / "cloudbet_promotions.html"
    if not path.exists():
        pytest.skip("fixture not captured yet")
    body = path.read_text()
    adapter = CloudbetPromoAdapter(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    outcome = adapter.parse(
        [
            RawResponse(
                source="cloudbet",
                endpoint="promotions-page",
                url="https://www.cloudbet.com/en/promotions",
                status_code=200,
                body=body,
                fetched_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
                content_type="text/html",
            )
        ]
    )
    adapter.close()
    assert len(outcome.offers) >= 3
    assert any("welcome" in o.title.lower() for o in outcome.offers)


def test_promo_store_roundtrip(tmp_path: Path) -> None:
    store = PromoStore(tmp_path / "promos.sqlite3")
    run_id = store.start_run()
    offer = PromoOffer(
        source="draftkings",
        offer_id="1",
        kind=PromoKind.NO_SWEAT,
        title="Test No Sweat",
        description="desc",
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        raw_ref="x",
    )
    from src.promos.base import PromoSourceHealth

    health = PromoSourceHealth(
        source_key="draftkings",
        ok=True,
        checked_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        offer_count=1,
    )
    store.finish_run(run_id, ok=True, offers=[offer], health=[health])
    rows = store.offers_for_run(run_id)
    store.close()
    assert len(rows) == 1
    assert rows[0]["title"] == "Test No Sweat"


def test_promo_offer_rejects_blank_title() -> None:
    with pytest.raises(Exception):
        PromoOffer(
            source="x",
            offer_id="1",
            kind=PromoKind.OTHER,
            title="   ",
            observed_at=datetime(2026, 7, 31, tzinfo=UTC),
        )


def test_dedupe_offers_and_store_tolerates_duplicates(tmp_path: Path) -> None:
    from src.promos.base import PromoSourceHealth
    from src.promos.collector import dedupe_offers

    a = PromoOffer(
        source="cloudbet",
        offer_id="same",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome A",
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    b = PromoOffer(
        source="cloudbet",
        offer_id="same",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome B",
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    assert len(dedupe_offers([a, b])) == 1
    store = PromoStore(tmp_path / "promos.sqlite3")
    run_id = store.start_run()
    store.finish_run(
        run_id,
        ok=True,
        offers=[a, b],
        health=[
            PromoSourceHealth(
                source_key="cloudbet",
                ok=True,
                checked_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
                offer_count=1,
            )
        ],
    )
    assert len(store.offers_for_run(run_id)) == 1
    store.close()


def test_health_ok_empty_merchandising_vs_no_signals() -> None:
    from collections import Counter

    from src.promos.base import PromoParseOutcome, Rejection
    from src.promos.collector import _health_ok

    empty_catalog = PromoParseOutcome(skipped=Counter({"empty_merchandising": 1}))
    assert _health_ok(empty_catalog)[0] is True
    bare = PromoParseOutcome(skipped=Counter({"no_promo_signals": 1}))
    assert _health_ok(bare)[0] is False
    assert _health_ok(bare)[1] == "empty_after_parse"
    # Empty merch + dead landing must not look healthy.
    both = PromoParseOutcome(
        skipped=Counter({"empty_merchandising": 1, "landing_without_promo_copy": 1})
    )
    assert _health_ok(both)[0] is False
    # Offers win over a rejection on another surface.
    with_offer = PromoParseOutcome(
        offers=[
            PromoOffer(
                source="fanduel",
                offer_id="1",
                kind=PromoKind.SIGNUP_BONUS,
                title="Welcome Bonus",
                observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
            )
        ],
        rejections=[Rejection(source="fanduel", reason="bad", detail="x")],
    )
    assert _health_ok(with_offer)[0] is True
