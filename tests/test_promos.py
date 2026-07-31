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
        "betrivers_kambi",
        "bovada",
        "cloudbet",
        "pinnacle",
        "onexbet",
        "leovegas_kambi",
        "betmgm",
        "caesars",
        "fanatics",
        "hardrock",
        "bet365",
        "leovegas_on",
        "betmgm_on",
        # TheLines failover tenants.
        "tl_betmgm",
        "tl_caesars",
        "tl_bet365",
        "tl_fanduel",
        "tl_draftkings",
        "tl_hardrock",
        "tl_fanatics",
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
        summary="Bet $10, get $150 in bonus bets",
        eligible_regions=["IL", "NJ"],
        ineligible_regions=["CA"],
        eligibility_notes="New customers only",
        bonus_amount=150.0,
        min_deposit=10.0,
        reward_type="bonus_bets",
        usage_guidance="Hedge the bonus bet.",
        is_specific=True,
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
    health_rows = store.health_for_run(run_id)
    store.close()
    assert len(rows) == 1
    assert rows[0]["title"] == "Test No Sweat"
    assert rows[0]["summary"].startswith("Bet $10")
    assert rows[0]["eligible_regions"] == ["IL", "NJ"]
    assert rows[0]["ineligible_regions"] == ["CA"]
    assert rows[0]["bonus_amount"] == 150.0
    assert rows[0]["is_specific"] is True
    assert rows[0]["usage_guidance"]
    assert len(health_rows) == 1
    assert health_rows[0]["source_key"] == "draftkings"
    assert health_rows[0]["ok"] is True


def test_enrich_extracts_bet_get_bonus_bets() -> None:
    from src.promos.enrich import enrich_offer, extract_mechanics, is_vague_text
    from src.promos.geo import parse_eligibility, regions_from_text

    eligible, ineligible, _ = parse_eligibility(
        "Available in New Jersey and Illinois. Not available in California."
    )
    assert set(eligible) == {"NJ", "IL"}
    assert ineligible == ["CA"]

    # English words must not become Indiana / Oregon / Ontario.
    assert regions_from_text("Bet $5 get $150 in Bonus Bets or Free Bets on the app") == []
    loc_eligible, _, _ = parse_eligibility(
        "Must be physically located in AR, AZ, CO, CT, IL, IN, NJ, NY, PA, WV"
    )
    assert "AR" in loc_eligible and "IN" in loc_eligible and "NJ" in loc_eligible
    assert "OR" not in loc_eligible

    assert is_vague_text("Daily Boost Hub") is True
    assert is_vague_text("Cash out anytime") is True
    assert is_vague_text("Cash Bonus") is True
    assert is_vague_text("free bets") is True
    eligible_or, _, _ = parse_eligibility("Available in NJ or PA only")
    assert set(eligible_or) == {"NJ", "PA"}
    assert "OR" not in eligible_or
    _, ineligible_or, _ = parse_eligibility("Not available in NY or NJ")
    assert set(ineligible_or) == {"NY", "NJ"}
    eligible_ca, _, _ = parse_eligibility(
        "physically located in AR, AZ, CA-AB (18+), CA-ON, NJ"
    )
    assert "CA" not in eligible_ca
    assert "AB" in eligible_ca and "ON" in eligible_ca and "NJ" in eligible_ca
    eligible_or_state, _, _ = parse_eligibility("Available in OH, OR, PA")
    assert set(eligible_or_state) >= {"OH", "OR", "PA"}

    mechanics = extract_mechanics("Bet $5, Get $150 in Bonus Bets")
    assert mechanics["bonus_amount"] == 150.0
    assert mechanics["min_deposit"] == 5.0

    offer = PromoOffer(
        source="fanduel",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome offer",
        description="Bet $5 get $150 in Bonus Bets. Available in New Jersey and Illinois.",
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    enriched = enrich_offer(offer)
    assert enriched.is_specific is True
    assert enriched.bonus_amount == 150.0
    assert enriched.reward_type == "bonus_bets"
    assert "IL" in enriched.eligible_regions
    assert "NJ" in enriched.eligible_regions
    assert "IN" not in enriched.eligible_regions
    assert enriched.summary


def test_strategy_bonus_bet_mentions_hedge() -> None:
    from src.promos.strategy import build_usage_guidance

    offer = PromoOffer(
        source="draftkings",
        offer_id="1",
        kind=PromoKind.BONUS_BET,
        title="Bonus Bets",
        summary="Bet $5, get $150 in bonus bets",
        bonus_amount=150.0,
        reward_type="bonus_bets",
        is_specific=True,
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    text = build_usage_guidance(offer)
    assert "hedge" in text.lower()
    assert "stake-not-returned" in text.lower()


def test_strategy_routes_on_reward_type_when_kind_is_generic() -> None:
    """``reward_type`` is a real routing signal, not only ``kind``.

    The branches matched a space-normalised copy of the value (``"bonus bets"``)
    against the stored vocabulary (``"bonus_bets"``), so every underscored
    reward type — all of them but ``boost`` and ``cash`` — could never fire and
    an offer whose ``kind`` was generic fell to the "public copy is vague"
    catch-all even with concrete mechanics parsed out of it.
    """
    from src.promos.strategy import build_usage_guidance

    def guidance(reward_type: str) -> str:
        return build_usage_guidance(
            PromoOffer(
                source="betmgm",
                offer_id=reward_type,
                kind=PromoKind.OTHER,
                title="Promotion",
                reward_type=reward_type,
                bonus_amount=100.0,
                is_specific=True,
                observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
            )
        )

    assert "stake-not-returned" in guidance("bonus_bets").lower()
    assert "stake-not-returned" in guidance("free_bet").lower()
    assert "site credit" in guidance("site_credit").lower()
    assert "refund/bonus bet" in guidance("no_sweat").lower()
    assert "refund/bonus bet" in guidance("risk_free").lower()
    assert "no-vig" in guidance("boost").lower()


def test_deepen_reuses_shared_url_cache() -> None:
    from src.promos.deepen import deepen_offers

    html = """
    <html><head><title>Bet $5 Get $200 in Bonus Bets</title>
    <meta name="description" content="New customers: Bet $5, Get $200 in Bonus Bets"/>
    </head><body>Bet $5, Get $200 in Bonus Bets. Min odds -200.</body></html>
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=html)

    observed = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    offers = [
        PromoOffer(
            source="fanduel",
            offer_id=str(i),
            kind=PromoKind.SIGNUP_BONUS,
            title="Welcome offer",
            url="https://example.test/promotions",
            observed_at=observed,
        )
        for i in range(3)
    ]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = deepen_offers(offers, client=client, max_details=12)
    client.close()
    assert calls["n"] == 1
    assert all(o.is_specific for o in out)
    assert all(o.bonus_amount == 200.0 for o in out)


def test_prefer_primary_keeps_more_specific_secondary() -> None:
    from src.promos.redundancy import prefer_primary_offers

    observed = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    primary = PromoOffer(
        source="fanduel",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Offer",
        is_specific=False,
        observed_at=observed,
    )
    secondary = PromoOffer(
        source="tl_fanduel",
        offer_id="2",
        kind=PromoKind.SIGNUP_BONUS,
        title="FanDuel Welcome Offer Bet $5 get $150 in Bonus Bets",
        summary="Bet $5, get $150 in bonus bets",
        bonus_amount=150.0,
        is_specific=True,
        observed_at=observed,
        metadata={"feed": "thelines", "brand_key": "fanduel"},
    )
    merged = prefer_primary_offers([primary, secondary])
    assert any(o.source == "tl_fanduel" for o in merged)
    assert not any(o.source == "fanduel" for o in merged)


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
    no_catalog = PromoParseOutcome(skipped=Counter({"no_public_catalog": 1}))
    assert _health_ok(no_catalog)[0] is True
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
    cookie = PromoParseOutcome(
        rejections=[Rejection(source="smarkets", reason="cookie_wall", detail="cookies")]
    )
    assert _health_ok(cookie)[0] is False
    assert _health_ok(cookie)[1] == "cookie_wall"


def test_thelines_parse_fixture_maps_brands() -> None:
    from src.promos.thelines import TheLinesPromoAdapter, parse_thelines_offers

    path = FIXTURES / "thelines_sportsbook_promos.html"
    body = path.read_text()
    observed = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    mgm = parse_thelines_offers(
        body, source_key="tl_betmgm", brand_key="betmgm", observed_at=observed
    )
    assert mgm, "expected BetMGM offers from TheLines fixture"
    assert any("bonus" in o.title.lower() or "$" in o.title for o in mgm)
    assert all(o.metadata.get("feed") == "thelines" for o in mgm)
    assert all(o.metadata.get("brand_key") == "betmgm" for o in mgm)

    adapter = TheLinesPromoAdapter(
        source_key="tl_fanduel",
        brand_key="fanduel",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    outcome = adapter.parse(
        [
            RawResponse(
                source="tl_fanduel",
                endpoint="sportsbook-promos",
                url="https://www.thelines.com/betting/sportsbook-promos/",
                status_code=200,
                body=body,
                fetched_at=observed,
                content_type="text/html",
            )
        ]
    )
    adapter.close()
    assert outcome.offers
    assert all(o.source == "tl_fanduel" for o in outcome.offers)


def test_leovegas_parse_next_data_fixture() -> None:
    from src.promos.leovegas import LeoVegasPromoAdapter

    body = (FIXTURES / "leovegas_promotions.html").read_text()
    adapter = LeoVegasPromoAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    outcome = adapter.parse(
        [
            RawResponse(
                source="leovegas_kambi",
                endpoint="promotions-next",
                url="https://www.leovegas.com/en-row/promotions",
                status_code=200,
                body=body,
                fetched_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
                content_type="text/html",
            )
        ]
    )
    adapter.close()
    assert len(outcome.offers) >= 2
    titles = {o.title for o in outcome.offers}
    assert "Lunch Free Spins" in titles
    assert "Sports Welcome Boost" in titles
    assert not any("EN + ROW" in t for t in titles)


def test_prefer_primary_keeps_secondary_only_titles() -> None:
    from src.promos.redundancy import brand_coverage, prefer_primary_offers
    from src.promos.base import PromoSourceHealth

    observed = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    primary = PromoOffer(
        source="draftkings",
        offer_id="1",
        kind=PromoKind.PROFIT_BOOST,
        title="MLB SGP(x) Boost",
        observed_at=observed,
    )
    dup = PromoOffer(
        source="tl_draftkings",
        offer_id="2",
        kind=PromoKind.PROFIT_BOOST,
        title="MLB SGP(x) Boost",
        observed_at=observed,
        metadata={"feed": "thelines", "brand_key": "draftkings"},
    )
    unique = PromoOffer(
        source="tl_draftkings",
        offer_id="3",
        kind=PromoKind.BONUS_BET,
        title="Bet $5 Get $200 in Bonus Bets",
        observed_at=observed,
        metadata={"feed": "thelines", "brand_key": "draftkings"},
    )
    gap = PromoOffer(
        source="tl_betmgm",
        offer_id="4",
        kind=PromoKind.BONUS_BET,
        title="$1500 Bonus Bets If Lose on Rangers vs Astros",
        observed_at=observed,
        metadata={"feed": "thelines", "brand_key": "betmgm"},
    )
    merged = prefer_primary_offers([primary, dup, unique, gap])
    titles = {o.title for o in merged}
    assert "MLB SGP(x) Boost" in titles
    assert "Bet $5 Get $200 in Bonus Bets" in titles
    assert "$1500 Bonus Bets If Lose on Rangers vs Astros" in titles
    assert sum(1 for o in merged if o.title == "MLB SGP(x) Boost") == 1

    health = [
        PromoSourceHealth(source_key="draftkings", ok=True, checked_at=observed, offer_count=1),
        PromoSourceHealth(source_key="tl_draftkings", ok=True, checked_at=observed, offer_count=2),
        PromoSourceHealth(source_key="tl_betmgm", ok=True, checked_at=observed, offer_count=1),
        PromoSourceHealth(source_key="fanduel", ok=False, checked_at=observed, error_kind="bot_wall"),
        PromoSourceHealth(source_key="tl_fanduel", ok=True, checked_at=observed, offer_count=1),
    ]
    ok_n, total_n, ok_keys = brand_coverage(health)
    assert ok_n == 3  # draftkings, betmgm, fanduel
    assert total_n == 3
    assert "fanduel" in ok_keys


def test_draftkings_filters_racing_and_chrome_noise() -> None:
    payload = {
        "zones": [
            {
                "promotions": [
                    {
                        "promotionId": 1,
                        "category": "Profit Boost",
                        "productName": "Sportsbook",
                        "merchandisingData": {
                            "promotionHeadline": "WNBA SGP(x) Boost",
                            "promotionDescription": "Get a Profit Boost",
                        },
                        "userData": {
                            "promotionActionButton": {
                                "webRedirectUrl": "https://sportsbook.draftkings.com/leagues/2/94682"
                            }
                        },
                    },
                    {
                        "promotionId": 2,
                        "category": "Other",
                        "productName": "Racing",
                        "merchandisingData": {
                            "promotionHeadline": "DRAFTKINGS RACING IS LIVE",
                            "promotionDescription": "Horse racing",
                        },
                    },
                    {
                        "promotionId": 3,
                        "category": "Other",
                        "productName": "Sportsbook",
                        "merchandisingData": {
                            "promotionHeadline": "Join our Sportsbook Discord",
                            "promotionDescription": "chat",
                        },
                    },
                    {
                        "promotionId": 4,
                        "category": "Refer a Friend",
                        "productName": "Sportsbook",
                        "merchandisingData": {
                            "promotionHeadline": "Refer a Friend!",
                            "promotionDescription": "Refer",
                        },
                    },
                    {
                        "promotionId": 5,
                        "category": "Refer a Friend",
                        "productName": "Sportsbook",
                        "merchandisingData": {
                            "promotionHeadline": "Refer a Friend!",
                            "promotionDescription": "Refer again",
                        },
                    },
                ]
            }
        ]
    }
    adapter = DraftKingsPromoAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    outcome = adapter.parse([_raw("draftkings", "promotions-query", json.dumps(payload))])
    adapter.close()
    titles = [o.title for o in outcome.offers]
    assert titles == ["WNBA SGP(x) Boost", "Refer a Friend!"]
    assert outcome.skipped.get("non_sports_product", 0) >= 1
    assert outcome.skipped.get("non_offer_chrome", 0) >= 1
    assert outcome.skipped.get("duplicate_referral", 0) >= 1
