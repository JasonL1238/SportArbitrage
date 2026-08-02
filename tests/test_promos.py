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


# ── the `plan` command ───────────────────────────────────────────────────────


class TestThePlanCommand:
    """``python -m src.promos plan`` — the CLI had no coverage at all.

    Emptying its offer list before the print loop left the whole suite green,
    so every one of its refusals, filters and exit codes was unpinned.
    """

    def _seed(self, tmp_path, monkeypatch, *, market="moneyline",
              hedge_source="fanduel", promo_source="smarkets"):
        """One promo run at smarkets, one odds run its book can be hedged in.

        Parameterised on the market so the CLI can be exercised on a spread and
        a team total, not only the moneyline every earlier test used.
        """
        from datetime import timedelta

        from src import settings as settings_mod
        from src.promos.base import PromoSourceHealth
        from src.schema import Market, Selection, Side
        from src.sources.base import SourceHealth
        from src.store import Store
        from tests.conftest import make_quote

        promo_db = tmp_path / "promos.sqlite3"
        odds_db = tmp_path / "odds.sqlite3"
        monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
        monkeypatch.setattr(settings_mod, "DB_PATH", odds_db)

        observed = datetime.now(UTC)
        commence = observed + timedelta(hours=6)
        shared = dict(observed_at=observed, commence_time=commence)
        if market == "spread":
            quotes = [
                make_quote(source=promo_source, market=Market.SPREAD,
                           selection=Selection.AWAY, line=1.5, decimal_odds=3.0, **shared),
                make_quote(source=hedge_source, market=Market.SPREAD,
                           selection=Selection.HOME, line=-1.5, decimal_odds=1.5, **shared),
            ]
        elif market == "push":
            # An NFL two-way moneyline: a tie after overtime voids both legs, so
            # the all-outcomes floor is zero while the settled floor is real.
            # Without a market where they differ, swapping the two labels is an
            # equivalent transformation and cannot be tested.
            from src.schema import Sport
            quotes = [
                make_quote(source=promo_source, sport=Sport.FOOTBALL, league="NFL",
                           event_key="NFL-NYJ@NFL-NE:2026-07-28",
                           home_participant="NFL-NE", away_participant="NFL-NYJ",
                           home_team="New England", away_team="New York Jets",
                           selection=Selection.AWAY, decimal_odds=3.0, **shared),
                make_quote(source=hedge_source, sport=Sport.FOOTBALL, league="NFL",
                           event_key="NFL-NYJ@NFL-NE:2026-07-28",
                           home_participant="NFL-NE", away_participant="NFL-NYJ",
                           home_team="New England", away_team="New York Jets",
                           selection=Selection.HOME, decimal_odds=1.5, **shared),
            ]
        elif market == "team_total":
            quotes = [
                make_quote(source=promo_source, market=Market.TEAM_TOTAL, side=Side.HOME,
                           selection=Selection.OVER, line=4.5, decimal_odds=3.0, **shared),
                make_quote(source=hedge_source, market=Market.TEAM_TOTAL, side=Side.HOME,
                           selection=Selection.UNDER, line=4.5, decimal_odds=1.5, **shared),
            ]
        else:
            quotes = [
                make_quote(source=promo_source, selection=Selection.AWAY,
                           decimal_odds=3.0, **shared),
                make_quote(source=hedge_source, selection=Selection.HOME,
                           decimal_odds=1.5, **shared),
            ]

        class _Findings:
            """Storage reads attributes off whatever it is handed."""

            def __init__(self, rows):
                self.quote_count = len(rows)
                self.event_count = len({q.event_key for q in rows})
                self.errors = []
                self.warnings = []
                self.findings = []
                self.ok = True

        odds = Store(odds_db)
        run_id = odds.start_run(observed)
        odds.save_quotes(run_id, quotes)
        for key in (promo_source, hedge_source):
            odds.save_health(run_id, SourceHealth(source_key=key, ok=True, checked_at=observed))
        odds.finish_run(run_id, finished_at=observed, report=_Findings(quotes))
        odds.close()

        promos = PromoStore(promo_db)
        promo_run = promos.start_run()
        promos.finish_run(
            promo_run, ok=True,
            offers=[PromoOffer(
                source=promo_source, offer_id="credit", kind=PromoKind.BONUS_BET,
                title="Bonus bets", observed_at=observed,
                bonus_amount=100.0, reward_type="bonus_bets",
            )],
            health=[PromoSourceHealth(source_key=promo_source, ok=True,
                                      checked_at=observed, offer_count=1)],
        )
        promos.close()
        return promo_run

    @pytest.fixture()
    def seeded(self, tmp_path, monkeypatch):
        return self._seed(tmp_path, monkeypatch)

    def _run(self, argv):
        from src.promos.collector import main
        return main(argv)

    def test_every_leg_is_printed_with_its_stake_role_and_money_kind(
        self, seeded, capsys,
    ):
        """The lines the operator actually acts on.

        The only CLI assertion was on the header and the strategy name, both of
        which sit above this block — so the whole leg table could be emptied,
        the stakes printed as odds, or the credit/cash tag inverted, with the
        suite green.
        """
        assert self._run(["plan"]) == 0
        out = capsys.readouterr().out
        legs = [line for line in out.splitlines() if line.startswith("    promo ")
                or line.startswith("    hedge ")]
        assert legs, out
        promo = next(line for line in legs if line.startswith("    promo "))
        hedge = next(line for line in legs if line.startswith("    hedge "))
        # The promo leg is the issuing book, staked as credit; the hedge is
        # elsewhere, staked as cash.  Inverting the tag sends real money to the
        # wrong book.
        assert "smarkets" in promo
        assert "(credit)" in promo and "(credit)" not in hedge
        assert "(cash)" in hedge
        # $100 of credit at smarkets' 3.0 pays net 2.96 after its commission,
        # so the profit is $196 and the hedge is 196/1.5 = $130.67.  The CLI
        # runs with the real fee table, unlike the planner's own unit tests —
        # the stake, not the price, which is what a stake/odds mix-up prints.
        assert "stake 100.00" in promo, promo
        assert "stake 130.67" in hedge, hedge
        assert "@ 3.000" in promo and "@ 1.500" in hedge
        # The two floors are different numbers and must not swap: "worst" is
        # the all-outcomes floor, "settles" the floor where the promo leg
        # actually settles.  On a pushable market they differ by the whole
        # position, and printing one under the other's label reverses which
        # guarantee the operator is reading.
        assert "worst " in out and "settles " in out
        worst_line = next(line for line in out.splitlines() if "worst " in line)
        import re as _re
        worst, settles = _re.search(
            r"worst ([+-][\d.]+), settles ([+-][\d.]+)", worst_line
        ).groups()
        assert float(worst) <= float(settles) + 1e-9, worst_line

    def test_the_two_floors_are_not_interchangeable(
        self, tmp_path, monkeypatch, capsys,
    ):
        """On a pushable market they differ by the whole position.

        "worst" is the floor over every outcome; "settles" is the floor over
        the outcomes where the promo leg actually settles.  Printing one under
        the other's label reverses which guarantee is being read, and every
        earlier fixture had them equal.
        """
        self._seed(tmp_path, monkeypatch, market="push")
        assert self._run(["plan"]) == 0
        out = capsys.readouterr().out
        line = next(l for l in out.splitlines() if "worst " in l)
        import re as _re
        worst, settles = _re.search(
            r"worst ([+-][\d.]+), settles ([+-][\d.]+)", line
        ).groups()
        assert float(worst) < float(settles), line
        assert float(worst) == pytest.approx(0.0, abs=0.01), line

    def test_a_spread_plan_prints_each_sides_own_signed_line(
        self, tmp_path, monkeypatch, capsys,
    ):
        """No CLI test had ever used a spread, a total or a team total.

        The two sides of one spread carry opposite signs; printing the group's
        canonical line on both would send the hedge to the same side of the
        game.
        """
        self._seed(tmp_path, monkeypatch, market="spread")
        assert self._run(["plan"]) == 0
        out = capsys.readouterr().out
        assert "spread" in out
        legs = [line for line in out.splitlines() if line.startswith("    promo ")
                or line.startswith("    hedge ")]
        assert len(legs) >= 2, out
        promo = next(line for line in legs if line.startswith("    promo "))
        hedge = next(line for line in legs if line.startswith("    hedge "))
        # Opposite signs, each on its own row.  "+1.5 or -1.5 appears somewhere"
        # was satisfied by the group's canonical line alone, while the operator
        # still could not see which side each stake went on.
        assert "+1.5" in promo, promo
        assert "-1.5" in hedge, hedge
        assert "-1.5" not in promo, promo

    def test_a_team_total_plan_names_whose_total_it_is(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Without the side, the two sides of one fixture print identically and
        neither can be placed."""
        self._seed(tmp_path, monkeypatch, market="team_total")
        assert self._run(["plan"]) == 0
        out = capsys.readouterr().out
        assert "team_total" in out
        # The fixture prices the HOME team's total, and Miami is home.  An
        # `or` over both names accepted the branch inversion it exists to
        # catch — the away team's name on the home team's total.
        assert "(Miami Marlins)" in out, out
        assert "(Philadelphia Phillies)" not in out.split("—", 1)[-1].split("\n", 1)[0], out

    def test_verbose_shows_offers_that_produced_nothing(
        self, tmp_path, monkeypatch, capsys,
    ):
        """``--verbose`` is the "why did nothing come back" mode and no test
        passed it, so its branch had one arm only."""
        # FanDuel's only "hedge" here is Action Network's view of FanDuel —
        # the same counterparty, so nothing is placeable and the offer is
        # invisible without --verbose.
        self._seed(tmp_path, monkeypatch, promo_source="fanduel",
                   hedge_source="an_fanduel")
        assert self._run(["plan"]) == 0
        quiet = capsys.readouterr().out
        assert "strategy:" not in quiet, quiet
        assert self._run(["plan", "--verbose"]) == 0
        loud = capsys.readouterr().out
        assert "strategy:" in loud, loud
        # And it says why, which is the whole point of the mode.
        assert "gated out:" in loud or "note:" in loud, loud

    def test_a_plan_is_printed_for_a_covered_book(self, seeded, capsys):
        assert self._run(["plan"]) == 0
        out = capsys.readouterr().out
        assert "smarkets" in out
        assert "bonus_conversion" in out

    def test_an_unknown_source_is_refused_not_answered_with_silence(self, seeded, capsys):
        """A typo — or the zsh trap where `--source 'a b'` is one argument —
        used to filter every offer away and print "no offer produced a concrete
        plan", which reads as a verdict on the promos rather than on the
        command line."""
        assert self._run(["plan", "--source", "bogus"]) == 1
        captured = capsys.readouterr()
        assert "no offers from: bogus" in captured.err
        assert "smarkets" in captured.err, "the message must say what is available"

    def test_the_one_argument_quoting_trap_is_refused(self, seeded, capsys):
        assert self._run(["plan", "--source", "smarkets fanduel"]) == 1
        assert "no offers from: smarkets fanduel" in capsys.readouterr().err

    def test_a_known_source_still_works(self, seeded, capsys):
        assert self._run(["plan", "--source", "smarkets"]) == 0
        assert "smarkets" in capsys.readouterr().out

    def test_a_nonexistent_run_is_refused(self, seeded, capsys):
        assert self._run(["plan", "--run", "999"]) == 1
        captured = capsys.readouterr()
        assert "no promo run #999" in captured.err
        assert f"#{seeded}" in captured.err, "the message must name the runs that exist"

    def test_run_zero_is_answered_not_silently_replaced(self, seeded, capsys):
        """``--run 0`` is a value the operator typed; a falsy test swapped it
        for the latest run and answered a question nobody asked."""
        assert self._run(["plan", "--run", "0"]) == 1
        assert "no promo run #0" in capsys.readouterr().err

    def test_limit_zero_prints_nothing_rather_than_everything(self, seeded, capsys):
        assert self._run(["plan", "--limit", "0"]) == 0
        out = capsys.readouterr().out
        assert "promo run #" in out, "the header still reports what was planned"
        assert "bonus_conversion" not in out, "--limit 0 must print no offers"
        # Asking for zero lines is not a verdict on the offers, which planned
        # perfectly well — the run above proves it.
        assert "no offer produced a concrete plan" not in out, out

    def test_a_run_older_than_the_listing_window_is_still_accepted(
        self, seeded, capsys, tmp_path,
    ):
        """``--run`` validation must be an existence check, not a top-N listing.

        Validating against ``list_runs(limit=N)`` — ``ORDER BY id DESC LIMIT N``
        — reported every run outside the newest N as "not stored", the exact
        false verdict the validation was added to prevent.
        """
        from src import settings as settings_mod

        store = PromoStore(settings_mod.PROMO_DB_PATH)
        try:
            for _ in range(30):
                extra = store.start_run()
                store.finish_run(extra, ok=True, offers=[], health=[])
        finally:
            store.close()

        # ``seeded`` is now far outside any plausible listing window.
        assert self._run(["plan", "--run", str(seeded)]) == 0
        captured = capsys.readouterr()
        assert "no promo run" not in captured.err, captured.err
        assert f"promo run #{seeded}" in captured.out

    def _seed_with_mirrors(self, tmp_path, monkeypatch):
        """A run whose slate holds a *measurable* mirror.

        The ordinary seed has two sources and one game, so the derived groups
        are empty and `{}` is indistinguishable from the real gate.  Two Kambi
        skins over twelve games give 24 shared selections, past
        ``MIN_SHARED_SELECTIONS``, so the mirror is measured.
        """
        from datetime import timedelta

        from src import settings as settings_mod
        from src.promos.base import PromoSourceHealth
        from src.schema import Selection
        from src.sources.base import SourceHealth
        from src.store import Store
        from tests.conftest import make_quote

        promo_db = tmp_path / "promos.sqlite3"
        odds_db = tmp_path / "odds.sqlite3"
        monkeypatch.setattr(settings_mod, "PROMO_DB_PATH", promo_db)
        monkeypatch.setattr(settings_mod, "DB_PATH", odds_db)

        observed = datetime.now(UTC)
        commence = observed + timedelta(hours=6)
        quotes = []
        for i in range(12):
            shared = dict(
                event_key=f"MLB-A{i}@MLB-H{i}:2026-07-28",
                home_participant=f"MLB-H{i}", away_participant=f"MLB-A{i}",
                observed_at=observed, commence_time=commence,
            )
            for source in ("betrivers_kambi", "leovegas_kambi"):
                quotes.append(make_quote(source=source, selection=Selection.AWAY,
                                         decimal_odds=3.00, **shared))
                quotes.append(make_quote(source=source, selection=Selection.HOME,
                                         decimal_odds=1.50, **shared))
            quotes.append(make_quote(source="fanduel", selection=Selection.HOME,
                                     decimal_odds=1.40, **shared))
            quotes.append(make_quote(source="fanduel", selection=Selection.AWAY,
                                     decimal_odds=3.20, **shared))

        class _Findings:
            def __init__(self, rows):
                self.quote_count = len(rows)
                self.event_count = len({q.event_key for q in rows})
                self.errors = []
                self.warnings = []
                self.findings = []
                self.ok = True

        odds = Store(odds_db)
        run_id = odds.start_run(observed)
        odds.save_quotes(run_id, quotes)
        for key in ("betrivers_kambi", "leovegas_kambi", "fanduel"):
            odds.save_health(run_id, SourceHealth(source_key=key, ok=True,
                                                  checked_at=observed))
        odds.finish_run(run_id, finished_at=observed, report=_Findings(quotes))
        odds.close()

        promos = PromoStore(promo_db)
        promo_run = promos.start_run()
        promos.finish_run(
            promo_run, ok=True,
            offers=[PromoOffer(
                source="betrivers_kambi", offer_id="credit", kind=PromoKind.BONUS_BET,
                title="Bonus bets", observed_at=observed,
                bonus_amount=100.0, reward_type="bonus_bets",
            )],
            health=[PromoSourceHealth(source_key="betrivers_kambi", ok=True,
                                      checked_at=observed, offer_count=1)],
        )
        promos.close()
        return promo_run

    def test_the_cli_plans_behind_a_counterparty_gate(self, tmp_path, monkeypatch):
        """The gate at the CLI's call site, not the planner's default.

        ``build_promo_plans`` derives the groups when handed ``None``, so the
        planner is safe either way — but this caller passes the measured groups
        explicitly and nothing asserted it passed anything.  Replacing the
        argument with ``{}`` left the suite green while the CLI could print a
        hedge at the same licence as the promo book.
        """
        import src.promos.planner as planner_module

        seen: dict = {}
        real = planner_module.build_promo_plans

        def spy(offers, quotes, **kwargs):
            seen.update(kwargs)
            return real(offers, quotes, **kwargs)

        self._seed_with_mirrors(tmp_path, monkeypatch)
        monkeypatch.setattr(planner_module, "build_promo_plans", spy)
        assert self._run(["plan"]) == 0
        gate = seen.get("one_counterparty")
        # Not `is not None`: `{}` is not None either, and `{}` is precisely the
        # switched-off value this test exists to catch.
        assert gate, f"the CLI planned with the gate switched off: {seen!r}"
        pairs = [frozenset(group) for groups in gate.values() for group in groups]
        assert frozenset({"betrivers_kambi", "leovegas_kambi"}) in pairs, gate

    def test_the_header_names_the_home_and_away_teams_the_right_way_round(
        self, seeded, capsys,
    ):
        """The header is the CLI's *only* mapping from side to team name.

        Each leg row prints a bare ``away`` / ``home``, so if the header reads
        "Miami at Philadelphia" when the truth is "Philadelphia at Miami", the
        operator puts the credit on the wrong club.  Swapping the two was green
        across the whole suite: priced with the project's own fee table the
        position becomes -$42.47 / +$261.34 against a printed floor of +$65.33.
        """
        assert self._run(["plan"]) == 0
        out = capsys.readouterr().out
        assert "Philadelphia Phillies at Miami Marlins" in out, out
        assert "Miami Marlins at Philadelphia Phillies" not in out, out

    def test_the_promo_leg_names_the_side_the_price_belongs_to(self, seeded, capsys):
        """The header and the leg rows must agree about which side is which.

        The fixture prices the *away* side at 3.00 and the home side nowhere
        near it, so a leg row saying ``away`` beside a header naming the home
        team first would send the stake to a price that does not exist.
        """
        assert self._run(["plan"]) == 0
        out = capsys.readouterr().out
        header = next(l for l in out.splitlines() if " at " in l and "—" in l)
        away_team = header.split(" at ")[0].strip()
        assert away_team == "Philadelphia Phillies", header
        promo = next(l for l in out.splitlines()
                     if l.startswith("    promo ") and "@" in l)
        assert " away " in promo, promo
        assert "3.000" in promo, promo
