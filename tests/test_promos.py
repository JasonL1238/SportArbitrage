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


def test_enrich_is_idempotent() -> None:
    """Enriching twice equals enriching once.

    The first live batch stored two DraftKings offers whose own terms name IL as
    IL-*ineligible*: the collector enriches three times per collect, and each pass
    re-read its own generated "Not available in: X; Eligible in: …" note, whose
    exclusion span (which did not stop at ";") swallowed the eligible list.
    """
    from src.promos.enrich import enrich_offer

    offer = PromoOffer(
        source="draftkings",
        offer_id="idem",
        kind=PromoKind.SIGNUP_BONUS,
        title="Parlay Boost",
        terms="Get $150 in bonus bets. Not available in OR. Available in AZ, IL and NJ.",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    once = enrich_offer(offer)
    assert once.eligible_regions == ["AZ", "IL", "NJ"]
    assert once.ineligible_regions == ["OR"]
    assert once.eligibility_notes.count("Eligible in") == 1
    twice = enrich_offer(once)
    assert twice == once
    assert enrich_offer(twice) == once


def test_fallback_summary_slice_starts_at_a_word_boundary() -> None:
    """The raw money-slice summary must not open mid-word.

    Live case (bovada poker welcome, first deepened terms): the unanchored
    window produced "ited States Dollars) up to a maximum of $500USD…".
    """
    import re as _re

    from src.promos.enrich import extract_mechanics

    terms = (
        "100% Poker Welcome Bonus Poker Bonus will be released in USD "
        "(United States Dollars) up to a maximum of $500USD. The 100% "
        "Welcome Bonus is based on the total of your first deposit. "
        "Released amounts arrive as bonus funds."
    )
    mech = extract_mechanics(
        "Deposit for a 100% Poker Welcome Bonus",
        "Poker at Bovada is better than ever. Deposit for a 100% Poker Welcome Bonus",
        terms,
        "",
    )
    summary = mech["summary"]
    assert summary
    cleaned = _re.sub(r"\s+", " ", terms).strip()
    start = cleaned.find(summary[:40])
    assert start == 0 or cleaned[start - 1] == " "


def test_eligibility_notes_are_never_parser_input() -> None:
    """The feedback loop is closed at the source, not by regex boundaries.

    A generated "Not available in: X; Eligible in: …" note re-parsed by
    `_EXCLUDE_CTX` inverts the eligible list. The fix is that `enrich_offer`
    never feeds `eligibility_notes` into the parse — an offer pre-seeded with
    the poisonous note shape keeps the eligibility its own copy states.
    """
    from src.promos.enrich import enrich_offer

    offer = PromoOffer(
        source="draftkings",
        offer_id="notes-not-input",
        kind=PromoKind.SIGNUP_BONUS,
        title="Boost",
        terms="Get $100 in bonus bets. Available in AZ, IL and NJ.",
        eligibility_notes="Not available in: OR; Eligible in: AZ, IL, NJ",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    enriched = enrich_offer(offer)
    assert enriched.eligible_regions == ["AZ", "IL", "NJ"]
    assert enriched.ineligible_regions == []


def test_semicolon_separated_state_copy_parses_fully() -> None:
    """Legal copy separates jurisdictions with semicolons; grabs must not stop there.

    A ";" boundary once made "Not available in NV; NY residents are also
    excluded" exclude only NV — fail-open in the one direction the
    ineligible-wins policy exists to prevent.
    """
    from src.promos.collector import offer_confirmed_for_state
    from src.promos.enrich import enrich_offer
    from src.promos.geo import parse_eligibility

    eligible, _, _ = parse_eligibility("Offer available in NJ; PA; IL only.")
    assert set(eligible) >= {"NJ", "PA", "IL"}

    offer = enrich_offer(
        PromoOffer(
            source="draftkings",
            offer_id="semicolon-exclusion",
            kind=PromoKind.SIGNUP_BONUS,
            title="Bonus",
            terms=(
                "Get $100 in bonus bets. Offer available in NJ, NY and PA. "
                "Not available in NV; NY residents are also excluded."
            ),
            observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        )
    )
    assert set(offer.ineligible_regions) >= {"NV", "NY"}
    assert not offer_confirmed_for_state(offer, "NY")


def test_note_segments_are_not_duplicated() -> None:
    from src.promos.enrich import enrich_offer

    offer = PromoOffer(
        source="draftkings",
        offer_id="notes-once",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome",
        terms="Get $100 in bonus bets. Not available in OR.",
        eligibility_notes="Not available in: OR",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    enriched = enrich_offer(enrich_offer(offer))
    assert enriched.eligibility_notes.count("Not available in: OR") == 1


def test_state_promo_eligibility_requires_affirmative_confirmation() -> None:
    from src.promos.collector import offer_confirmed_for_state

    base = dict(
        source="draftkings",
        offer_id="state-check",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome",
        observed_at=datetime.now(UTC),
    )
    assert not offer_confirmed_for_state(PromoOffer(**base), "PA")
    assert offer_confirmed_for_state(
        PromoOffer(**base, eligible_regions=["PA"]), "pa"
    )
    assert not offer_confirmed_for_state(
        PromoOffer(**base, eligible_regions=["PA"], ineligible_regions=["PA"]), "PA"
    )


def test_unconfirmed_offers_are_stored_with_the_verdict_not_dropped() -> None:
    """The gate's *predicate* is unchanged; only the consequence moved.

    The first live run filtered 51 of 54 offers here with no audit trail — a
    source that answered with twelve genuine offers graded FAILED because the
    filter emptied it.  Offers now carry ``state_confirmed`` and every one is
    stored; False is a label the surfaces render, not a deletion.
    """
    from src.promos.collector import offer_confirmed_for_state

    base = dict(
        kind=PromoKind.SIGNUP_BONUS,
        observed_at=datetime.now(UTC),
    )
    offers = [
        PromoOffer(source="betmgm", offer_id="national", title="Bet $10 get $150", **base),
        PromoOffer(
            source="fanduel", offer_id="stamped", title="IL welcome",
            eligible_regions=["IL"], **base,
        ),
    ]
    stamped = [
        o.model_copy(update={"state_confirmed": offer_confirmed_for_state(o, "IL")})
        for o in offers
    ]
    assert len(stamped) == 2, "nothing may be dropped"
    verdicts = {o.offer_id: o.state_confirmed for o in stamped}
    assert verdicts == {"national": False, "stamped": True}


def test_store_v3_migration_grows_state_confirmed(tmp_path: Path) -> None:
    """Opening a v2-shaped DB grows the column; a fresh DB has it natively."""
    import sqlite3

    db = tmp_path / "promos.sqlite3"
    # A v2-shaped store: build a current one, then strip the column and stamp
    # the old version, which is exactly what a pre-upgrade file looks like.
    store = PromoStore(db)
    store.close()
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(promo_offers)")]
    assert "state_confirmed" in cols
    con.execute("ALTER TABLE promo_offers DROP COLUMN state_confirmed")
    con.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    con.commit()
    con.close()

    reopened = PromoStore(db)
    run_id = reopened.start_run(jurisdiction="IL")
    offer = PromoOffer(
        source="betmgm",
        offer_id="x",
        kind=PromoKind.BONUS_BET,
        title="Bet $10 get $150",
        observed_at=datetime.now(UTC),
        state_confirmed=True,
    )
    reopened.finish_run(run_id, ok=True, offers=[offer], health=[], notes="")
    rows = reopened.offers_for_run(run_id)
    reopened.close()
    assert rows[0]["state_confirmed"] is True


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


def test_terms_slice_prefers_terms_heading() -> None:
    from src.promos.html_util import terms_slice

    body = (
        "<html><body><nav>Promotions Odds Boosts Betting</nav>"
        "<h1>Bet $5 Get $150</h1><p>Marketing copy about the offer.</p>"
        "<h3>Terms &amp; Conditions</h3>"
        "<p>Must be 21+ and physically located in AZ, IL and NJ. Min odds -200. "
        "Offer ends September 1, 2026.</p></body></html>"
    )
    sliced = terms_slice(body)
    assert sliced is not None
    assert sliced.startswith("Must be 21+")
    assert "Marketing copy" not in sliced

    assert terms_slice("<html><body><p>No conditions here.</p></body></html>") is None


def test_terms_links_absolutized() -> None:
    from src.promos.html_util import terms_links

    body = (
        '<a href="/promos/welcome/full-terms">Full terms</a>'
        '<a href="/legal/terms-of-service">Terms of Service</a>'
        '<a href="#top">T&amp;Cs</a>'
    )
    links = terms_links("https://example.test/promotions", body)
    assert links == ["https://example.test/promos/welcome/full-terms"]


def test_deepen_prefers_terms_url_over_offer_url() -> None:
    """A declared T&C link is fetched before the promo page itself."""
    from src.promos.deepen import deepen_offers

    terms_html = (
        "<html><head><title>Offer Terms</title></head><body>"
        "<h3>Terms and Conditions</h3>"
        "<p>Bet $5, Get $200 in Bonus Bets. Min odds -200. Must be 21+ and "
        "physically located in AZ, IL and NJ.</p></body></html>"
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=terms_html)

    offer = PromoOffer(
        source="betmgm",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome offer",
        url="https://example.test/promotions/welcome",
        metadata={"terms_url": "https://example.test/promotions/welcome/terms"},
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = deepen_offers([offer], client=client, max_details=12)
    client.close()
    assert requested == ["https://example.test/promotions/welcome/terms"]
    deepened = out[0]
    assert deepened.metadata["deepened_terms_from"].endswith("/terms")
    assert deepened.terms.startswith("Bet $5, Get $200")
    assert deepened.is_specific is True
    assert "IL" in deepened.eligible_regions


def test_deepen_writes_raw_envelopes() -> None:
    from src.promos.deepen import deepen_offers

    class _Store:
        def __init__(self) -> None:
            self.written: list[object] = []

        def write(self, raw: object) -> None:
            self.written.append(raw)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "<html><head><title>Bet $5 Get $200 in Bonus Bets</title></head>"
                "<body>Bet $5, Get $200 in Bonus Bets.</body></html>"
            ),
        )

    offer = PromoOffer(
        source="fanduel",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome offer",
        url="https://example.test/promotions",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    store = _Store()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    deepen_offers([offer], client=client, raw_store=store)  # type: ignore[arg-type]
    client.close()
    assert len(store.written) == 1
    envelope = store.written[0]
    assert envelope.source == "fanduel"
    assert envelope.endpoint.startswith("deepen-")
    assert envelope.status_code == 200
    assert "Bonus Bets" in envelope.body


def test_enrich_extracts_absolute_ends_at() -> None:
    from src.promos.enrich import enrich_offer, extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at("Offer ends September 1, 2026.", observed) == datetime(
        2026, 9, 1, 23, 59, tzinfo=UTC
    )
    assert extract_ends_at("Valid through 09/01/2026 only.", observed) == datetime(
        2026, 9, 1, 23, 59, tzinfo=UTC
    )
    # Credit-usage windows are not offer end dates.
    assert extract_ends_at("Both bets expire within seven days if not used.", observed) is None
    assert extract_ends_at("Expiry Date: TBC.", observed) is None

    offer = PromoOffer(
        source="draftkings",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Bet $5 get $150 in Bonus Bets",
        terms="Offer ends September 1, 2026.",
        observed_at=observed,
    )
    assert enrich_offer(offer).ends_at == datetime(2026, 9, 1, 23, 59, tzinfo=UTC)


def test_ends_at_yearless_rolls_forward() -> None:
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at("Offer ends January 5.", observed) == datetime(
        2027, 1, 5, 23, 59, tzinfo=UTC
    )
    assert extract_ends_at("Offer ends September 1.", observed) == datetime(
        2026, 9, 1, 23, 59, tzinfo=UTC
    )


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


def test_collision_merges_state_evidence_into_winner() -> None:
    """The dropped side's state lists fold into the kept row, with provenance."""
    from src.promos.redundancy import prefer_primary_offers

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    primary = PromoOffer(
        source="betmgm",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Offer Bet $10 get $150",
        summary="Bet $10, get $150 in bonus bets",
        bonus_amount=150.0,
        is_specific=True,
        observed_at=observed,
    )
    secondary = PromoOffer(
        source="tl_betmgm",
        offer_id="2",
        kind=PromoKind.SIGNUP_BONUS,
        title="BetMGM Welcome Offer",
        is_specific=False,
        eligible_regions=["MI", "NJ", "PA", "WV"],
        observed_at=observed,
    )
    merged = prefer_primary_offers([primary, secondary])
    assert len(merged) == 1
    kept = merged[0]
    assert kept.source == "betmgm"
    assert kept.title == "Welcome Offer Bet $10 get $150"
    assert kept.eligible_regions == ["MI", "NJ", "PA", "WV"]
    assert kept.metadata["merged_evidence_from"] == "tl_betmgm"


def test_collision_merge_respects_ineligible() -> None:
    """Ineligible wins over eligible — the merge narrows, never widens."""
    from src.promos.redundancy import prefer_primary_offers

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    primary = PromoOffer(
        source="betmgm",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Offer Bet $10 get $150",
        bonus_amount=150.0,
        is_specific=True,
        ineligible_regions=["IL"],
        observed_at=observed,
    )
    secondary = PromoOffer(
        source="tl_betmgm",
        offer_id="2",
        kind=PromoKind.SIGNUP_BONUS,
        title="BetMGM Welcome Offer",
        is_specific=False,
        eligible_regions=["IL", "NJ"],
        observed_at=observed,
    )
    merged = prefer_primary_offers([primary, secondary])
    assert len(merged) == 1
    kept = merged[0]
    assert "IL" not in kept.eligible_regions
    assert kept.eligible_regions == ["NJ"]
    assert kept.ineligible_regions == ["IL"]


def test_secondary_winner_absorbs_primary_terms() -> None:
    """When the aggregator row wins, the dropped first-party terms survive."""
    from src.promos.redundancy import prefer_primary_offers

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    primary = PromoOffer(
        source="fanduel",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Offer",
        is_specific=False,
        terms="Must be 21+ and physically located in IL, NJ or PA.",
        eligible_regions=["IL", "NJ", "PA"],
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
    )
    merged = prefer_primary_offers([primary, secondary])
    assert len(merged) == 1
    kept = merged[0]
    assert kept.source == "tl_fanduel"
    assert kept.terms == "Must be 21+ and physically located in IL, NJ or PA."
    assert kept.eligible_regions == ["IL", "NJ", "PA"]
    assert kept.metadata["merged_evidence_from"] == "fanduel"


def test_clause_fragments_do_not_invent_states() -> None:
    """Semicolon-split fragments must not read English as state codes.

    "ID verification required", "in-app only", "OK to combine", and a
    lowercase "or …" clause invented Idaho/Indiana/Oklahoma/Oregon on the
    first cut of the `;` splitter.
    """
    from src.promos.geo import parse_eligibility

    _, inel, _ = parse_eligibility("Not available in NY; ID verification required at signup.")
    assert inel == ["NY"]
    _, inel, _ = parse_eligibility("Not available in NY; in-app only.")
    assert inel == ["NY"]
    el, _, _ = parse_eligibility("Offer available in NJ; OK to combine with other promotions.")
    assert el == ["NJ"]
    el, _, _ = parse_eligibility("Offer available in IL; or download the app to claim.")
    assert el == ["IL"]
    # The recall case the splitter exists for still works…
    _, inel, _ = parse_eligibility("Not available in NV; NY residents are also excluded.")
    assert set(inel) == {"NV", "NY"}
    # …and Oregon still survives explicit lists.
    el, _, _ = parse_eligibility("Available in OH, OR, PA")
    assert set(el) >= {"OH", "OR", "PA"}


def test_exclusion_clauses_keep_their_own_polarity() -> None:
    """A "; XX customers are excluded" clause must never join the eligible list.

    The semicolon split fed clause fragments into the *surrounding* context,
    so "available in NJ; OR customers are excluded" read Oregon as eligible —
    and confirmed it. Exclusion clauses now parse at the clause level.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility("Offer available in NJ; OR customers are excluded.")
    assert el == ["NJ"]
    assert inel == ["OR"]
    el, inel, _ = parse_eligibility("Offer available in NJ; NY customers are excluded.")
    assert el == ["NJ"]
    assert inel == ["NY"]
    _, inel, _ = parse_eligibility("Offer available in NJ; ME residents are not eligible.")
    assert inel == ["ME"]
    # A requirement clause is not an exclusion — and must not invent a state.
    _, inel, _ = parse_eligibility("Not available in NV; IN players must opt in to claim.")
    assert inel == ["NV"]
    _, inel, _ = parse_eligibility("Not available in NY; ID users must verify age.")
    assert inel == ["NY"]
    # Wider person vocabulary.
    _, inel, _ = parse_eligibility("Not available in NV; OR bettors are also excluded.")
    assert set(inel) == {"NV", "OR"}


def test_suffix_exclusion_captures_the_whole_code_list() -> None:
    """"NY, AZ and OR customers are excluded" excludes all three, not one."""
    from src.promos.geo import parse_eligibility

    _, inel, _ = parse_eligibility(
        "Not available in NV; NY, AZ and OR customers are excluded."
    )
    assert set(inel) == {"NV", "NY", "AZ", "OR"}


def test_verb_fragments_never_join_a_context_list() -> None:
    """A noun-less exclusion fragment must not leak into the eligible list.

    "NY is excluded" carries no person noun, so the suffix pass cannot claim
    it — the fragment filter is the only thing keeping NY out of the
    surrounding eligible context.
    """
    from src.promos.geo import parse_eligibility

    el, _, _ = parse_eligibility("Offer available in NJ; NY is excluded.")
    assert el == ["NJ"]


def test_single_dot_abbreviations_are_not_sentence_ends() -> None:
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "The promo begins August 13 at approx. noon and ends August 15.",
        observed,
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)
    # Two promos joined by a conjunction in one sentence: Promo A's August
    # start postdates the candidate January end, so it is not coherent
    # elapsed evidence and Promo B's end rolls forward — the round-8
    # coherence guard turned the old accepted expired-stamp into the
    # correct next-January reading.
    assert extract_ends_at(
        "Promo A started August 1 and Promo B ends January 5.", observed
    ) == datetime(2027, 1, 5, 23, 59, tzinfo=UTC)


def test_script_embedded_comment_opener_does_not_wipe_the_page() -> None:
    """A "<!--" inside script code is JS, not a comment opener."""
    from src.promos.html_util import visible_text

    body = (
        '<script>var s = "<!--";</script>'
        "<p>Genuine terms: available in IL. Min odds -200.</p>"
    )
    text = visible_text(body)
    assert "Genuine terms" in text
    assert "var s" not in text


def test_window_tolerates_clock_times_and_reversed_order() -> None:
    """a.m./p.m. dots are not sentence ends; the start may follow the end."""
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "The promotion begins August 13 at 9:00 a.m. ET and ends August 15 "
        "at 11:59 p.m. ET.",
        observed,
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)
    assert extract_ends_at(
        "The promotion ends August 15, having started August 13.", observed
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)
    assert extract_ends_at(
        "The offer begins August 10 for U.S. customers and ends August 14.",
        observed,
    ) == datetime(2026, 8, 14, 23, 59, tzinfo=UTC)
    # A different promo's start in another sentence still must not match.
    assert extract_ends_at(
        "Leaderboard Sprint started August 1 and has concluded. Separately, "
        "the New Year Parlay Special ends January 5.",
        observed,
    ) == datetime(2027, 1, 5, 23, 59, tzinfo=UTC)


def test_spelled_out_exclusion_clauses_exclude() -> None:
    """Name-form exclusion clauses carry the same polarity as code-form ones.

    Five rounds hardened "; XX residents are excluded" for two-letter codes
    while "New York residents are also excluded" walked straight into the
    eligible list through the name scanner — the one direction (excluded →
    confirmed) the whole predicate exists to prevent.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Offer available in New Jersey; New York residents are also excluded."
    )
    assert el == ["NJ"]
    assert "NY" in inel

    el, inel, _ = parse_eligibility(
        "Offer is available to residents of New Jersey and Pennsylvania; "
        "New York residents are not eligible."
    )
    assert set(el) == {"NJ", "PA"}
    assert "NY" in inel

    # A stand-alone exclusion sentence lands without any context grab.
    _, inel, _ = parse_eligibility(
        "Bet $5 get $150 in bonus bets. New York residents are excluded."
    )
    assert "NY" in inel

    # Single-word names, name lists, and the three-word district.
    el, inel, _ = parse_eligibility(
        "Available in New Jersey and Pennsylvania; Nevada residents are "
        "also excluded."
    )
    assert "NV" in inel and "NV" not in el
    _, inel, _ = parse_eligibility(
        "New York and West Virginia customers may not participate."
    )
    assert set(inel) >= {"NY", "WV"}
    _, inel, _ = parse_eligibility(
        "District of Columbia residents are ineligible for this offer."
    )
    assert "DC" in inel


def test_suffix_exclusion_survives_an_oxford_comma() -> None:
    """"NY, AZ, and OR residents are excluded" excludes all three."""
    from src.promos.geo import parse_eligibility

    _, inel, _ = parse_eligibility(
        "Available in CO and NJ. NY, AZ, and OR residents are excluded."
    )
    assert set(inel) == {"NY", "AZ", "OR"}
    _, inel, _ = parse_eligibility("NY, AZ, & OR customers are excluded.")
    assert set(inel) == {"NY", "AZ", "OR"}


def test_suffix_exclusion_is_linear_on_conjunction_lists() -> None:
    """An all-caps list joined with uppercase OR must parse fast.

    The old one-regex chain went exponential here — 24 codes took ~18 s and
    30 took minutes, hanging the collector on one legal blob.  Uppercase OR
    still reads as Oregon (over-excluding fails closed).
    """
    import time

    from src.promos.geo import parse_eligibility

    codes = [
        "NY", "AZ", "CO", "CT", "IL", "KS", "KY", "LA", "MA", "MD",
        "MI", "MN", "MO", "NC", "NH", "NV", "NJ", "PA", "TN", "VA",
        "VT", "WV", "WY", "AL", "AK", "AR", "DE", "FL", "GA", "IA",
    ]
    text = " OR ".join(codes) + " CUSTOMERS ARE EXCLUDED."
    t0 = time.perf_counter()
    _, inel, _ = parse_eligibility(text)
    assert time.perf_counter() - t0 < 5.0
    assert set(inel) >= set(codes)


def test_decimal_numbers_are_not_sentence_ends() -> None:
    """"1.5x" / "odds of 1.50" / "$0.50" must not break the same-sentence rule."""
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "The tournament started August 13 with 1.5x profit boosts and "
        "ends August 15.",
        observed,
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)
    assert extract_ends_at(
        "The promo started August 13 at odds of 1.50 and ends August 15.",
        observed,
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)


def test_year_straddling_elapsed_window_stays_elapsed() -> None:
    """"started December 20 … ends January 5" seen in August already ran.

    The past tense proves the start was *last* December; the present tense
    means the upcoming one, so that window still rolls forward.
    """
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "The tournament started December 20 and ends January 5.", observed
    ) == datetime(2026, 1, 5, 23, 59, tzinfo=UTC)
    assert extract_ends_at(
        "The tournament starts December 20 and ends January 5.", observed
    ) == datetime(2027, 1, 5, 23, 59, tzinfo=UTC)


def test_invalid_first_end_date_does_not_hide_a_later_one() -> None:
    """"ends June 31" skips to the next stated date instead of aborting."""
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "Offer ends June 31. Offer ends July 1, 2026.", observed
    ) == datetime(2026, 7, 1, 23, 59, tzinfo=UTC)


def test_sitewide_terms_and_conditions_href_is_house_rules() -> None:
    """A path ending in /terms-and-conditions is the house rules, denied."""
    from src.promos.html_util import terms_links

    body = (
        '<a href="/terms-and-conditions">Terms and Conditions</a>'
        '<a href="/en-us/terms-and-conditions?lang=en">Full terms</a>'
        '<a href="/promotions/summer-boost/full-terms">Full terms</a>'
    )
    assert terms_links("https://example.test/promos", body) == [
        "https://example.test/promotions/summer-boost/full-terms"
    ]


def test_consent_button_is_not_a_terms_heading() -> None:
    """"Accept Terms & Conditions" on a cookie banner marks nothing."""
    from src.promos.html_util import terms_slice

    body = (
        "<button>Accept Terms &amp; Conditions</button>"
        "<p>Page copy that is not conditions at all, just promo chrome "
        "stretching well past the length floor of the slice helper.</p>"
    )
    assert terms_slice(body) is None
    # A genuine heading after the banner still anchors the slice.
    real = body + (
        "<h2>Terms and Conditions</h2>"
        "<p>Must be 21+ and physically located in IL. Min odds -200 apply "
        "to every qualifying wager placed during the promotional window.</p>"
    )
    sliced = terms_slice(real)
    assert sliced is not None
    assert "21+" in sliced


def test_script_token_inside_comment_does_not_truncate() -> None:
    """"<script" inside a closed comment is comment text, not an opener.

    The round-5 scripts-first ordering fixed one mirror hole and opened this
    one: the unclosed-script backstop fired on a commented-out script include
    and silently dropped everything after the comment — eligibility lines
    included.  Constructs now strip left to right by first opener.
    """
    from src.promos.html_util import visible_text

    body = (
        "<p>Real promo copy: Bet $5 get $150 in bonus bets.</p>"
        "<!-- TODO re-enable the <script> loader here -->"
        "<p>Eligible in NJ, PA, MI. 21+.</p>"
    )
    text = visible_text(body)
    assert "Real promo copy" in text
    assert "Eligible in NJ, PA, MI" in text
    assert "TODO" not in text
    # A commented-out script include without its closer, same guarantee.
    body2 = (
        "<p>Header copy.</p>"
        '<!-- <script src="analytics.js"> -->'
        "<p>Available in NJ and PA only.</p>"
    )
    text2 = visible_text(body2)
    assert "Available in NJ and PA only" in text2


def test_index_with_many_terms_links_stamps_none() -> None:
    """A multi-card index's first T&C link is some card's, not the page's."""
    from src.promos.html_catalog import HtmlCatalogPromoAdapter

    def page(links: str) -> str:
        return (
            "<html><head><title>Welcome Offers | ExampleBook</title>"
            '<meta name="description" content="Bonus bets and boosts"></head>'
            f"<body>{links}</body></html>"
        )

    two = page(
        '<a href="/promotions/casino-cashback/full-terms">Full terms</a>'
        '<a href="/promotions/welcome/full-terms">Full terms</a>'
    )
    one = page('<a href="/promotions/welcome/full-terms">Full terms</a>')
    adapter = HtmlCatalogPromoAdapter(
        source_key="betmgm",
        index_url="https://example.test/promotions",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    out_two = adapter.parse([_raw("betmgm", "promotions-index", two)])
    out_one = adapter.parse([_raw("betmgm", "promotions-index", one)])
    adapter.close()
    meta_two = next(o for o in out_two.offers if o.offer_id == "index-meta")
    meta_one = next(o for o in out_one.offers if o.offer_id == "index-meta")
    assert "terms_url" not in meta_two.metadata
    assert meta_one.metadata.get("terms_url") == (
        "https://example.test/promotions/welcome/full-terms"
    )


def test_unparseable_spans_keep_their_own_state_lists() -> None:
    """Marked spans with no money line still stay separate offers.

    The whole-section fallback re-merged both spans' state lists into one
    description — resurrecting the exact merge the ordinal split repaired.
    """
    from src.promos.thelines import _welcome_from_section

    text = (
        "BetMGM runs two offers. The first: a profit boost token for "
        "players located in IL and AZ. The second: a casino spins package "
        "for players located in MI and WV."
    )
    got = _welcome_from_section("BetMGM review", text, ("betmgm",))
    assert len(got) == 2
    first_desc, second_desc = got[0][1], got[1][1]
    assert "IL" in first_desc and "IL" not in second_desc
    assert "MI" in second_desc and "MI" not in first_desc


def test_usage_guidance_formats_millions_with_commas() -> None:
    """usage_guidance is a stored column — no scientific notation in it."""
    from src.promos.strategy import build_usage_guidance

    offer = PromoOffer(
        source="cloudbet",
        offer_id="m",
        kind=PromoKind.NO_SWEAT,
        title="Millionaire No Sweat",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        bonus_amount=1_000_000.0,
    )
    guidance = build_usage_guidance(offer)
    assert "$1,000,000" in guidance
    assert "e+06" not in guidance


def test_second_person_word_does_not_swallow_the_anchor() -> None:
    """The suffix pass anchors on the person word *nearest* the verb.

    "open to players in … and Nevada residents are excluded": anchoring on
    "players" swallowed the whole region list into the gap, lost the
    exclusion, and confirmed NV.  The glued eligible list is forfeited from
    the eligible side and may be over-excluded by the backward walk — both
    fail closed; an earlier comma-stop that kept MI/OH out of ineligible
    also under-excluded "valid for new customers only, NY, NV, and PA
    residents are excluded" to its last member, leaving adapter-stamped
    states confirmable (fail-open).
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Offer open to players in Michigan and Ohio, and Nevada residents "
        "are excluded."
    )
    assert el == []
    assert "NV" in inel


def test_unrecognized_exclusion_verbs_do_not_invert() -> None:
    """"void in" / "excludes" / "prohibited" carry exclusion polarity.

    Every clause with a geographic-exclusion verb is masked from the
    eligible passes, so an exclusion verb the ineligible patterns miss can
    never confirm the state it excludes.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility("Offer available in New Jersey; void in New York.")
    assert el == ["NJ"] and "NY" in inel
    el, inel, _ = parse_eligibility("Offer available in New Jersey; excludes New York.")
    assert el == ["NJ"] and "NY" in inel
    el, inel, _ = parse_eligibility("Offer void in NY, NV, PA.")
    assert el == [] and set(inel) == {"NY", "NV", "PA"}
    el, inel, _ = parse_eligibility("Prohibited in NY, NV, PA.")
    assert el == [] and set(inel) == {"NY", "NV", "PA"}
    # No person word, no recognized ineligible-context verb — only the
    # clause mask keeps the eligible grab from reading NY through the ";".
    el, _, _ = parse_eligibility(
        "Offer available in New Jersey; wagers from New York are not eligible."
    )
    assert el == ["NJ"]


def test_person_first_exclusion_orderings_exclude() -> None:
    """"Customers located in <list> are excluded" — the list follows the noun."""
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Customers physically located in NJ, NY, PA, WV are excluded from "
        "this offer."
    )
    assert el == [] and set(inel) == {"NJ", "NY", "PA", "WV"}
    el, inel, _ = parse_eligibility(
        "Customers physically located in New Jersey are excluded from this offer."
    )
    assert el == [] and inel == ["NJ"]
    el, inel, _ = parse_eligibility("Players located in Nevada may not participate.")
    assert el == [] and inel == ["NV"]
    el, inel, _ = parse_eligibility(
        "Residents of NY, NV, PA are not eligible for this promotion."
    )
    assert el == [] and set(inel) == {"NY", "NV", "PA"}
    el, inel, _ = parse_eligibility("Any resident of New York may not participate.")
    assert el == [] and inel == ["NY"]


def test_conduct_restrictions_are_not_state_exclusions() -> None:
    """"may not participate more than once" caps frequency, not geography."""
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Available in New Jersey and Pennsylvania, residents may not "
        "participate more than once."
    )
    assert set(el) == {"NJ", "PA"} and inel == []
    el, inel, _ = parse_eligibility(
        "Offer available in New Jersey. New Jersey residents may not bet "
        "on in-state college teams."
    )
    assert el == ["NJ"] and inel == []
    el, inel, _ = parse_eligibility(
        "Open to customers in AZ, CO, and IL, users may not redeem more "
        "than once per day."
    )
    assert set(el) >= {"CO", "IL"} and inel == []


def test_year_straddle_mirror_stays_elapsed() -> None:
    """"started December 20 … ends December 30" seen in January already ran.

    The elapsed check used to run only when the anchor-year end date was in
    the *past*; a straddling window whose end lands in the future rolled a
    dead promo forward a year.  A genuinely live straddling window (start
    last December, end this March) must keep its future end.
    """
    from src.promos.enrich import extract_ends_at

    observed = datetime(2027, 1, 5, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "The tournament started December 20 and ends December 30.", observed
    ) == datetime(2026, 12, 30, 23, 59, tzinfo=UTC)
    assert extract_ends_at(
        "The tournament started December 20 and ends March 1.", observed
    ) == datetime(2027, 3, 1, 23, 59, tzinfo=UTC)


def test_abrupt_close_comments_do_not_truncate() -> None:
    """HTML5 treats "<!-->" and "<!--->" as complete empty comments."""
    from src.promos.html_util import visible_text

    text = visible_text("<p>before</p><!--><p>after: Not available in NV.</p>")
    assert "before" in text and "Not available in NV" in text
    text = visible_text("<p>before</p><!---><p>after: 21+ only.</p>")
    assert "before" in text and "21+ only" in text


def test_house_rules_slug_variants_are_denied() -> None:
    """Every common sitewide-terms slug is house rules, not a promo link."""
    from src.promos.html_util import terms_links

    body = (
        '<a href="/general-terms-and-conditions">Full terms</a>'
        '<a href="/legal">T&amp;Cs apply</a>'
        '<a href="/terms.html">Full terms</a>'
        '<a href="/terms_and_conditions">Full terms</a>'
        '<a href="/promo/mlb-boost-terms">Full terms</a>'
    )
    assert terms_links("https://example.test/promos", body) == [
        "https://example.test/promo/mlb-boost-terms"
    ]


def test_relative_href_resolves_before_the_deny_list() -> None:
    """A slash-less href="terms" resolves to the exact denied house-rules URL."""
    from src.promos.html_util import terms_links

    body = '<a href="terms">Full Terms and Conditions</a>'
    assert terms_links("https://example.test/", body) == []


def test_scrub_is_linear_on_marker_heavy_pages() -> None:
    """An SSR page with thousands of hydration comments must scrub fast.

    Re-searching both construct patterns every iteration made the scan
    quadratic: 20k comment markers took seconds and 1MB of scripts ~11 s.
    """
    import time

    from src.promos.html_util import visible_text

    body = "<p>promo copy</p>" + "<!--$-->x" * 40000
    t0 = time.perf_counter()
    text = visible_text(body)
    assert time.perf_counter() - t0 < 5.0
    assert "promo copy" in text


def test_ca_ont_is_ontario_not_california() -> None:
    """DraftKings' live "located in CA-ONT" is Ontario, never California."""
    from src.promos.geo import parse_eligibility

    el, _, _ = parse_eligibility("19+ and physically located in CA-ONT.")
    assert el == ["ON"]
    assert "CA" not in el


def test_unlisted_may_not_tails_still_exclude() -> None:
    """A may-not verb excludes by default; only conduct objects opt out.

    The earlier polarity — a whitelist of acceptable exclusion tails —
    failed open: "may not participate in this promotion due to state
    regulations" fell off the whitelist and NY stayed confirmable.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Customers located in New York may not participate in this "
        "promotion due to state regulations."
    )
    assert el == [] and inel == ["NY"]
    el, inel, _ = parse_eligibility(
        "Players located in NV may not participate in the tournament."
    )
    assert el == [] and inel == ["NV"]
    # A comma inside the verb list no longer hides the conduct object.
    el, inel, _ = parse_eligibility(
        "New Jersey residents may not enter, claim, or redeem this "
        "promotion more than once."
    )
    assert inel == []
    # No person word at all: the suffix pass cannot attribute NY, so only
    # the chunk forfeit stands between the may-not clause and an eligible
    # confirmation.  The glued NJ is forfeited too — fail-closed.
    el, _, _ = parse_eligibility(
        "Offer available in New Jersey and accounts registered in New York "
        "may not claim this offer."
    )
    assert "NY" not in el
    assert el == []


def test_comma_spliced_exclusion_lists_exclude_fully() -> None:
    """An eligible verb sharing the clause must not truncate the list.

    The round-7 comma-stop cut "NY, NV, and PA residents are excluded" to
    its last member whenever "valid"/"open" appeared earlier in the clause —
    under-exclusion that left adapter-stamped states confirmable.
    """
    from src.promos.geo import parse_eligibility

    _, inel, _ = parse_eligibility(
        "Offer valid for new customers only, NY, NV, and PA residents are "
        "excluded."
    )
    assert set(inel) >= {"NY", "NV", "PA"}
    _, inel, _ = parse_eligibility(
        "Open to all players 21+ but NY, AZ, and OR residents are excluded."
    )
    assert set(inel) >= {"NY", "AZ", "OR"}


def test_negated_eligible_verbs_do_not_confirm() -> None:
    """"not open to" / "restricted in" / "not permitted" never confirm.

    The chunk-narrow-and-forfeit rework: nothing beyond a pure region-list
    continuation rides an eligible grab across ";", and a grab whose own
    chunk carries exclusion signal is forfeited whole — partial salvage of
    a mixed-polarity chunk is where every inversion lived.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "The Promotion is open only to residents of New Jersey; it is not "
        "open to residents of New York."
    )
    assert el == ["NJ"] and "NY" in inel
    el, inel, _ = parse_eligibility(
        "Offer available in New Jersey; restricted in New York."
    )
    assert el == ["NJ"] and "NY" in inel
    el, _, _ = parse_eligibility(
        "Offer available in New Jersey; participation from New York is "
        "not permitted."
    )
    assert el == ["NJ"]
    # A ';'-separated genuine state list still rides the grab.
    el, _, _ = parse_eligibility("Offer available in NJ; NY; PA.")
    assert set(el) == {"NJ", "NY", "PA"}
    # "cannot" carries the same forfeit weight as "not permitted".
    el, _, _ = parse_eligibility(
        "Offer available in New Jersey and cannot be claimed from New York."
    )
    assert "NY" not in el


def test_market_exclusions_do_not_touch_state_lists() -> None:
    """"void if the match is abandoned" and "Parlays are excluded" are
    market rules — the comma-spliced eligible list must survive both, and
    no state may be false-excluded from the void clause."""
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "All bets are void if the match is abandoned, offer available in "
        "NJ and PA."
    )
    assert set(el) == {"NJ", "PA"} and inel == []
    el, inel, _ = parse_eligibility(
        "Parlays and teasers are excluded, offer available in NJ and PA."
    )
    assert set(el) == {"NJ", "PA"} and inel == []


def test_live_straddling_window_observed_midrun_stays_live() -> None:
    """"started December 20 and ends January 5" seen December 28 is LIVE.

    Elapsed evidence must cohere — a window's start cannot follow its own
    end, so the past start does not drag the January end into last year.
    """
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 12, 28, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "The promotion started December 20 and ends January 5.", observed
    ) == datetime(2027, 1, 5, 23, 59, tzinfo=UTC)
    # The genuinely elapsed window is untouched.
    assert extract_ends_at(
        "The tournament will start on August 13 and end on August 15.",
        datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)


def test_bare_code_list_keeps_its_and_tail() -> None:
    """"MI, NJ, PA, and WV." keeps WV — the tail was dead on uppercased text.

    Live case: tl_fanatics' eligibility sentence lost WV in every batch
    since run #2.
    """
    from src.promos.geo import parse_eligibility

    el, _, _ = parse_eligibility(
        "You must reside in the following states in order to be eligible: "
        "MI, NJ, PA, and WV."
    )
    assert set(el) == {"MI", "NJ", "PA", "WV"}


def test_percentages_are_not_dollars() -> None:
    """"Up to 100% Parlay Boosts" must not store bonus_amount=100 (or 10).

    Live case: tl_bet365 stored a fabricated $100.  The naive lookahead
    fix backtracked into "10" — the digit guard pins that too.
    """
    from src.promos.enrich import extract_mechanics

    mech = extract_mechanics("Up to 100% Parlay Boosts every day")
    assert mech.get("bonus_amount") is None
    mech = extract_mechanics("Deposit and get up to $100 in site credit")
    assert mech.get("bonus_amount") == 100.0


def test_boosted_is_not_a_boost_reward() -> None:
    """"25% boosted rakeback" is not a profit boost; "25% profit boost" is.

    Live case: cloudbet's welcome package stored reward "boost" and a
    "25% profit boost" summary from post-welcome rakeback copy.
    """
    from src.promos.enrich import extract_mechanics

    mech = extract_mechanics(
        "Welcome package with 25% boosted rakeback on casino play"
    )
    assert mech.get("reward_type") != "boost"
    mech = extract_mechanics("Get a 25% profit boost token")
    assert mech.get("reward_type") == "boost"


def test_bet_and_get_extracts_mechanics() -> None:
    """"Bet $5 and get $300" — the conjunction is the commonest phrasing.

    Live case: tl_draftkings' flagship welcome stored bonus_amount=None.
    """
    from src.promos.enrich import extract_mechanics

    mech = extract_mechanics("Bet $5 and get $300 Instantly in Bonus Bets")
    assert mech.get("bonus_amount") == 300.0
    assert mech.get("min_deposit") == 5.0


def test_fallback_summary_slice_ends_at_a_word_boundary() -> None:
    """The money-slice summary must not cut the trailing word either.

    Live cases, run 35: "…the total of your fi", "…one of the fast-growi".
    """
    from src.promos.enrich import extract_mechanics

    terms = (
        "Bonus released in USD up to a maximum of $500USD and computed "
        "against the cumulative running balance of your first qualifying "
        "deposit contribution only. Released amounts arrive as bonus funds."
    )
    mech = extract_mechanics("Poker welcome", "", terms, "")
    summary = mech["summary"]
    assert summary
    last_word = summary.rstrip(".").rsplit(" ", 1)[-1]
    assert last_word in terms.split()


def test_person_noun_eligible_parts_keep_their_regions() -> None:
    """"Available to residents of KS and MO." keeps both states.

    The exclusion pass learned person-noun lists in round 7; the eligible
    side's leading-token rule still dropped the noun-adjacent code.
    """
    from src.promos.geo import parse_eligibility

    el, _, _ = parse_eligibility("Available to residents of KS and MO.")
    assert set(el) == {"KS", "MO"}
    el, _, _ = parse_eligibility("Available to residents of KS.")
    assert el == ["KS"]


def test_exclusion_phrasing_with_eligible_verbs_still_excludes() -> None:
    """"Excludes players located in Michigan" must veto MI, not confirm it.

    The verb-position truncation cut the grab at any eligible-verb
    appearance — "located" lives inside exclusion phrasing, the emptied
    grab left nothing in ineligible, and the eligible pass then CONFIRMED
    the excluded state.  A flip must open a new clause.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility("Excludes players located in Michigan.")
    assert el == [] and inel == ["MI"]
    _, inel, _ = parse_eligibility(
        "This Promotion is not available to persons physically located in "
        "New Jersey."
    )
    assert inel == ["NJ"]
    _, inel, _ = parse_eligibility(
        "Not available to residents of New York, or customers located in "
        "Nevada."
    )
    assert set(inel) == {"NY", "NV"}
    # A genuine clause-initial flip still cuts.
    _, inel, _ = parse_eligibility(
        "Offer void in New York and only valid in New Jersey."
    )
    assert inel == ["NY"]


def test_except_tails_are_exclusions_not_confirmations() -> None:
    """"available in all states except New York" must veto NY, never confirm.

    Three defenses failed together: the greedy exclusion grab consumed the
    "except" trigger, the flip cut threw the tail away un-rescanned, and
    "except" was in no eligible-side vocabulary.  The exclusion scan now
    restarts inside each chunk, and an eligible chunk stops at an exception
    trigger — so the head stays eligible and the tail stays a veto.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Excludes casino games, available in all states except New York."
    )
    assert el == [] and inel == ["NY"]
    el, inel, _ = parse_eligibility(
        "Excludes casino games, available in New Jersey, Pennsylvania and "
        "West Virginia except New York."
    )
    assert set(el) == {"NJ", "PA", "WV"} and inel == ["NY"]
    # Bare "but not in" keeps the head eligible and never confirms the tail.
    el, inel, _ = parse_eligibility("Offer valid in New Jersey but not in New York.")
    assert el == ["NJ"] and "NY" not in el


def test_flip_glue_reaches_common_auxiliaries() -> None:
    """"but is still available in New Jersey" is a genuine polarity flip.

    The glue list missed still/will/be/currently and demanded a space after
    the comma, so the eligible clause was swallowed into the veto —
    destroying an adapter-stamped state's confirmation on realistic copy.
    """
    from src.promos.geo import parse_eligibility

    for text in (
        "Offer is not available in New York but is still available in New Jersey.",
        "Not available in New York,available in New Jersey.",
        "Not available in New York but will be available in New Jersey.",
    ):
        _, inel, _ = parse_eligibility(text)
        assert inel == ["NY"], text


def test_flip_glue_is_restricted_to_glue_words() -> None:
    """A verb reached across real words is the exclusion's own list.

    Loosening the glue to arbitrary words lets "in any state where the
    offer is not eligible" read as a flip and NJ escape the veto.
    """
    from src.promos.geo import parse_eligibility

    _, inel, _ = parse_eligibility(
        "Not available in New York, in any state where the offer is not "
        "eligible, or in New Jersey."
    )
    assert {"NY", "NJ"} <= set(inel)


def test_severed_exclusion_lists_never_confirm() -> None:
    """A code list severed from its exclusion trigger must not confirm.

    "except: NJ, NY, PA" bypassed the veto (the trigger regex demanded
    whitespace) and the innocent-looking bare list confirmed all three; a
    newline between "Excluded states:" and its list did the same through
    the clause-local mask.  Punctuation-tolerant triggers veto the colon
    shapes; colon-carry masking keeps newline-severed lists out of the
    bare-list fallback (unconfirmed, fail-closed).
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility("Available in all states except: NJ, NY, PA.")
    assert el == [] and set(inel) == {"NJ", "NY", "PA"}
    el, inel, _ = parse_eligibility(
        "Available in all states excepting NJ, NY, PA."
    )
    assert el == [] and set(inel) == {"NJ", "NY", "PA"}
    for text in (
        "Not available in the following states:\nNJ, NY, PA.",
        "Excluded states:\nNJ, NY, PA.",
        "Available in all states but not: NJ, NY, PA.",
        # Wrapped lists: the continuation lines must not confirm either.
        "Excluded states:\nNJ, NY, PA,\nWV, MI, IL.",
        "Excluded states:\nNJ, NY;\nPA, WV, MI.",
    ):
        el, _, _ = parse_eligibility(text)
        assert el == [], text
    # A plain exclusion sentence wrapped mid-list vetoes the WHOLE list —
    # the tail lines used to confirm ("Eligible in: WV, MI, IL" stored
    # from a single "not available in" sentence).
    el, inel, _ = parse_eligibility(
        "This offer is not available in NJ, NY, PA,\nWV, MI, IL."
    )
    assert el == []
    assert set(inel) == {"NJ", "NY", "PA", "WV", "MI", "IL"}
    # A semicolon at the wrap point must not sever the list either — it
    # escaped both the extension tuple and the clause-tail check (a clause
    # can never end in ";"), and the tail half-confirmed.
    el, inel, _ = parse_eligibility(
        "This offer is not available in NJ, NY;\nPA, WV, MI."
    )
    assert el == []
    assert set(inel) == {"NJ", "NY", "PA", "WV", "MI"}
    el, inel, _ = parse_eligibility("Not available in NY, NV;\nON, QC, AB.")
    assert el == []
    assert set(inel) == {"NY", "NV", "ON", "QC", "AB"}


def test_wrapped_lists_join_before_any_parsing() -> None:
    """A newline that merely wraps a list is not a clause boundary.

    Four rounds each found another mechanism severed by a wrap; the join
    normalization closes the class: verb-after-list heads no longer
    confirm, wordless glyph lines no longer break the chain, dash
    introducers behave like colons, and eligible lists keep their tails.
    """
    from src.promos.geo import parse_eligibility

    # Verb-after-list sentences, wrapped mid-list: the head must veto too.
    el, inel, _ = parse_eligibility("Residents of NJ, NY, IL,\nand WV are excluded.")
    assert el == [] and set(inel) == {"NJ", "NY", "IL", "WV"}
    el, inel, _ = parse_eligibility(
        "NJ, NY, IL,\nand WV customers will not be eligible."
    )
    assert el == [] and set(inel) == {"NJ", "NY", "IL", "WV"}
    el, inel, _ = parse_eligibility(
        "Customers located in NJ, NY, IL,\nWV are excluded."
    )
    assert el == [] and set(inel) == {"NJ", "NY", "IL", "WV"}
    # A wordless line between carrier and list breaks nothing.
    el, _, _ = parse_eligibility("Excluded states:\n \nNJ, NY, IL.")
    assert el == []
    el, inel, _ = parse_eligibility("Not available in NJ, NY, IL,\n \nWV, MI, PA.")
    assert el == [] and set(inel) == {"NJ", "NY", "IL", "WV", "MI", "PA"}
    # Dash introducers behave like colons.
    el, inel, _ = parse_eligibility(
        "Not available in the following states —\nNJ, NY, IL."
    )
    assert el == [] and set(inel) == {"NJ", "NY", "IL"}
    # The ";"-separated carrier masks its list clause.
    el, _, _ = parse_eligibility("The following states are excluded;\nNJ, NY, PA.")
    assert el == []
    # …including when no exclusion-context trigger fires at all (bare
    # past-participle "excluded", no code list in the carrier clause): the
    # mask carry is the ONLY defense here, and its revert once survived
    # every test while the shape confirmed all three states.
    el, _, _ = parse_eligibility("Excluded states are as follows;\nNJ, NY, PA.")
    assert el == []
    # The eligible side keeps its wrapped tail.
    el, _, _ = parse_eligibility(
        "Offer available in AZ, CO, CT, IL, IN,\nIA, KS, KY, LA, MA."
    )
    assert set(el) == {"AZ", "CO", "CT", "IL", "IN", "IA", "KS", "KY", "LA", "MA"}


def test_but_not_and_other_than_are_triggers_not_forfeits() -> None:
    """"other than as required by law" must not eat a short code veto.

    Round 13 put "but not"/"other than" in the forfeit vocabulary, and the
    ";"-fragment filter dropped "NY other than as required by law" whole —
    item 79's veto loss verbatim.  As _EXCLUDE_CTX triggers their codes
    land in ineligible first, and "but not NV" tails now veto instead of
    merely masking.
    """
    from src.promos.geo import parse_eligibility

    _, inel, _ = parse_eligibility(
        "Not available in NY other than as required by law."
    )
    assert inel == ["NY"]
    _, inel, _ = parse_eligibility("Ineligible: NY, NV (but not for casino play).")
    assert set(inel) == {"NY", "NV"}
    el, inel, _ = parse_eligibility(
        "Eligible states include NJ, NY, PA but not NV."
    )
    assert set(el) == {"NJ", "NY", "PA"} and inel == ["NV"]


def test_attribute_entities_never_leak_into_text() -> None:
    """An "&gt;" inside a quoted attribute value is not a tag end.

    Unescape-before-strip cut the tag at the decoded ">" and the rest of
    the attribute (Tailwind class soup) survived as visible text — stored
    as promo terms in 92 rows across runs #7–#58.
    """
    from src.promos.html_util import strip_tags, visible_text

    body = (
        '<button class="focus:ring-2 [&amp;&gt;span]:line-clamp-1 h-10" '
        'type="button">Language</button> after'
    )
    assert strip_tags(body) == "Language after"
    assert "line-clamp" not in visible_text(body)


def test_except_boilerplate_does_not_eat_short_code_vetoes() -> None:
    """"Not available in NJ except as otherwise stated" must keep its veto.

    "except" in the forfeit vocabulary made the ";"-fragment filter drop
    the fragment whole — codes and all — and a 1–2-code list has no
    name-form or bare-list fallback, so an adapter-stamped NJ stayed
    confirmed against copy that excludes it.
    """
    from src.promos.geo import parse_eligibility

    _, inel, _ = parse_eligibility(
        "Not available in NJ except as otherwise stated in these terms."
    )
    assert inel == ["NJ"]
    _, inel, _ = parse_eligibility("Excludes NV except where required by law.")
    assert inel == ["NV"]


def test_abbreviation_cleaning_does_not_glue_rewards() -> None:
    """"cash vs. bonus spins" is not a "cash bonus".

    Replacing the abbreviation token with a space glued its neighbors and
    fabricated a reward — item 73's consequence signature reintroduced by
    item 77's own fix.  Only the dots are stripped now.
    """
    from src.promos.enrich import extract_mechanics

    mech = extract_mechanics(
        "Weekly Rewards",
        "",
        "Compare cash vs. bonus spins in your account. Min deposit $10 applies.",
        "",
    )
    assert mech.get("reward_type") == ""
    mech = extract_mechanics("Promo hub", "", "Get bonus approx. Bets settle fast.", "")
    assert mech.get("reward_type") != "bonus_bets"


def test_reward_patterns_do_not_match_across_sentences() -> None:
    """"…$500 Cash. Bonus Spins…" is not a "cash bonus".

    The negation-filter join glued sentences with a space, fabricating a
    reward across the boundary — stored live (leovegas_on, runs #43–#46),
    and the is_specific cascade cost the offer its deepen fetch.
    """
    from src.promos.enrich import extract_mechanics

    mech = extract_mechanics(
        "LeoBoost Weekly Loyalty Rewards",
        "",
        "Climb the tiers and claim $500 Cash. Bonus Spins are valid for "
        "seven days after issue.",
        "",
    )
    assert mech.get("reward_type") != "cash"
    mech = extract_mechanics(
        "Weekend special", "", "This play is risk free. Bets settle at "
        "standard market prices.", "",
    )
    assert mech.get("reward_type") != "free_bet"


def test_thelines_kind_ignores_review_prose() -> None:
    """TheLines' description is review prose naming *other* promos.

    "profit-boost tokens and odds boosts routinely available" stamped
    kind=odds_boost on the $250 bonus-bet welcome, and a parlay label — with
    a false "qualifying leg is a parlay" plan note — on a $10 straight-bet
    offer.  Kind reads the offer's own title only.
    """
    from src.promos.thelines import parse_thelines_offers

    path = FIXTURES / "thelines_sportsbook_promos.html"
    if not path.exists():
        pytest.skip("fixture not captured yet")
    body = path.read_text()
    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    betmgm = parse_thelines_offers(
        body, source_key="tl_betmgm", brand_key="betmgm",
        observed_at=observed, raw_ref="x", page_url="u",
    )
    win150 = next(o for o in betmgm if "$150" in o.title)
    assert win150.kind is PromoKind.BONUS_BET
    fanduel = parse_thelines_offers(
        body, source_key="tl_fanduel", brand_key="fanduel",
        observed_at=observed, raw_ref="x", page_url="u",
    )
    flagship = next(o for o in fanduel if "$250" in o.title)
    assert flagship.kind is not PromoKind.ODDS_BOOST


def test_modal_negations_exclude() -> None:
    """"will not be eligible" is the enumerated vocabulary one auxiliary away.

    The adjacency-bound patterns missed every modal form, so the comma-
    spliced clause confirmed NY for the state its copy excludes.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Available in New Jersey, New York residents will not be eligible."
    )
    assert "NY" not in el and "NY" in inel
    el, _, _ = parse_eligibility(
        "Available in New Jersey; wagers from New York shall not be allowed."
    )
    assert "NY" not in el
    el, inel, _ = parse_eligibility("Residents of New York do not qualify.")
    assert el == [] and inel == ["NY"]


def test_long_exclusion_lists_are_not_truncated() -> None:
    """Every state of a 26-state spelled-out exclusion list is vetoed.

    The 200-char chunk cap ended inside "Massachusetts": seven states past
    the cap escaped the veto, and an adapter-stamped NJ stayed confirmable
    against copy that excludes it.
    """
    from src.promos.geo import parse_eligibility

    text = (
        "This offer is not available to residents of Alabama, Alaska, "
        "Arizona, Arkansas, California, Colorado, Connecticut, Delaware, "
        "Florida, Georgia, Hawaii, Idaho, Indiana, Iowa, Kansas, Kentucky, "
        "Louisiana, Maine, Maryland, Massachusetts, Michigan, Minnesota, "
        "Mississippi, Missouri, New Jersey, New York."
    )
    _, inel, _ = parse_eligibility(text)
    assert {"NJ", "NY", "MA", "MO"} <= set(inel)
    assert len(inel) == 26


def test_restricted_to_is_not_an_exclusion() -> None:
    """"restricted TO New Jersey" means NJ-only — never a fabricated veto."""
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "This offer is restricted to residents of New Jersey."
    )
    assert inel == []
    # "restricted in" keeps its exclusion polarity.
    _, inel, _ = parse_eligibility("Offer restricted in New York.")
    assert inel == ["NY"]


def test_exclusion_grabs_stop_at_the_polarity_flip() -> None:
    """"excludes casino games, available in NJ" must not veto NJ.

    The no-preposition "excludes" family over-grabbed through the comma;
    the chunk now ends at the last list boundary before an eligible verb.
    The old both-directions semicolon swallow heals the same way.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Boost excludes casino games, available to customers in New Jersey "
        "and Pennsylvania."
    )
    assert set(el) == {"NJ", "PA"} and inel == []
    el, inel, _ = parse_eligibility(
        "All markets qualify except for parlays, offer available in New Jersey."
    )
    assert el == ["NJ"] and inel == []
    # The both-directions semicolon swallow no longer *false-excludes* the
    # eligible clause — NY/NJ stay unconfirmed (the span scrub hides them,
    # fail-closed) but never join the ineligible list.
    el, inel, _ = parse_eligibility("Not available in NV; eligible in NY and NJ.")
    assert inel == ["NV"]
    assert "NY" not in inel and "NJ" not in inel


def test_combinability_boilerplate_is_conduct() -> None:
    """"in conjunction with any other promotion" is a stacking rule.

    Item 48's default-exclude read every unlisted may-not tail as
    geography, fabricating vetoes from near-universal boilerplate.
    """
    from src.promos.geo import parse_eligibility

    el, inel, _ = parse_eligibility(
        "Offer available in NJ and PA, customers may not claim this offer "
        "in conjunction with any other promotion."
    )
    assert set(el) == {"NJ", "PA"} and inel == []
    el, inel, _ = parse_eligibility(
        "Available in NJ and PA, residents may not participate if employed "
        "by the operator."
    )
    assert set(el) == {"NJ", "PA"} and inel == []
    el, _, _ = parse_eligibility(
        "Available to NJ and PA customers only and cannot be combined with "
        "any other offer."
    )
    assert set(el) == {"NJ", "PA"}
    # A genuine geographic exclusion whose tail cites a regulator still lands.
    _, inel, _ = parse_eligibility(
        "Available in New York and New Jersey. New York customers may not "
        "participate per order of the gaming commission."
    )
    assert "NY" in inel


def test_modified_person_nouns_keep_their_regions() -> None:
    """"legal residents of KS and MO" — item 58's fix, one modifier deeper."""
    from src.promos.geo import parse_eligibility

    el, _, _ = parse_eligibility("Available to legal residents of KS and MO.")
    assert set(el) == {"KS", "MO"}


def test_reward_amounts_require_dollars() -> None:
    """Percentages and token counts never become bonus dollars.

    "Bet $5 and get 30% profit boost" fabricated $30; "receive 3 bonus
    bets" fabricated $3; "up to 27.5%" backtracked into $27; "100 percent"
    read as $100.
    """
    from src.promos.enrich import extract_mechanics

    assert extract_mechanics("Bet $5 and get 30% profit boost").get("bonus_amount") is None
    assert extract_mechanics("Bet $10 and receive 3 bonus bets").get("bonus_amount") is None
    assert extract_mechanics(
        "Opt in and receive up to 3 bonus bets every week."
    ).get("bonus_amount") is None
    assert extract_mechanics("Parlay boost up to 27.5% profit boost").get("bonus_amount") is None
    assert extract_mechanics("Profit boost up to 100 percent on any parlay").get("bonus_amount") is None
    mech = extract_mechanics("Enjoy a 25.5% profit boost")
    assert not (mech.get("summary") or "").startswith("5%")
    assert extract_mechanics("Bet $5 and get $300 Instantly in Bonus Bets").get("bonus_amount") == 300.0


def test_reward_is_never_read_from_a_negating_sentence() -> None:
    """A sentence that excludes rewards must not vote for reward_type.

    Live case: the confirmed DK profit boost stored bonus_bets from "Bets
    placed with bonus rewards (including … Bonus Bets …) are not valid" —
    and planned at ~5x its value as credit conversion.
    """
    from src.promos.enrich import extract_mechanics

    mech = extract_mechanics(
        "NFL Fast Futures 30% Profit Boost",
        "",
        "Bets placed with bonus rewards (including but not limited to Bonus "
        "Bets, Odds Boosts/Surges, Profit Boosts, etc.) are not valid for "
        "this offer.",
        "",
    )
    assert mech.get("reward_type") == "boost"
    # A genuine bonus-bet sentence still wins when the title is silent.
    mech = extract_mechanics(
        "Welcome offer", "", "Get $150 in Bonus Bets on signup.", ""
    )
    assert mech.get("reward_type") == "bonus_bets"
    # Title silent AND the only mention negated: no reward at all.
    mech = extract_mechanics(
        "Football Special",
        "",
        "Bets placed with Bonus Bets are not valid for this promotion.",
        "",
    )
    assert mech.get("reward_type") != "bonus_bets"
    # An "etc." inside the negating sentence must not sever the reward
    # names from their negation — DraftKings' live sentence shape.
    mech = extract_mechanics(
        "Football Special",
        "",
        "Bets placed with bonus rewards (including but not limited to Bonus "
        "Bets, Odds Boosts/Surges, Profit Boosts, etc.) are not valid for "
        "this offer.",
        "",
    )
    assert mech.get("reward_type") == ""


def test_more_house_rules_slugs_are_denied() -> None:
    """"/termsandconditions" and "/terms.php" are house rules too."""
    from src.promos.html_util import terms_links

    body = (
        '<a href="/termsandconditions">Terms and Conditions</a>'
        '<a href="/terms.php">Full terms</a>'
        '<a href="/promo/mlb-boost-terms">Full terms</a>'
    )
    assert terms_links("https://example.test/promos", body) == [
        "https://example.test/promo/mlb-boost-terms"
    ]


def test_large_amounts_format_with_commas() -> None:
    """$1,000,000 must not print as $1e+06 in a stored summary."""
    from src.promos.enrich import extract_mechanics

    mech = extract_mechanics(
        "Cloudbet Rewards", "cash back rewards up to $1,000,000 every month"
    )
    assert mech["summary"] == "$1,000,000 cash"


def test_comment_interiors_are_not_visible_text() -> None:
    """Commented-out markup's stale copy must not become terms."""
    from src.promos.html_util import visible_text

    body = (
        "<p>Live copy about the current offer.</p>"
        "<!-- <div>Old offer: available only in NY and NJ. Ends August 1.</div> -->"
    )
    text = visible_text(body)
    assert "Live copy" in text
    assert "NY" not in text
    assert "-->" not in text
    # Content AFTER a closed comment survives — the truncation backstop
    # alone would eat it.
    after = visible_text("<!-- old NY copy --><p>Current: available in IL.</p>")
    assert "Current: available in IL." in after
    assert "NY" not in after
    # Unclosed comments truncate rather than leak.
    assert "hidden" not in visible_text("<p>Shown.</p><!-- hidden forever")


def test_ambiguous_codes_in_person_clauses_still_exclude() -> None:
    """"OR residents are also excluded" is Oregon, not English.

    The round-2 gate refused ambiguous codes with any suffix but "only" —
    silently dropping exclusions in the exact clause shape the semicolon
    splitter was built for. A person-word suffix (residents/players/…) reads
    as a state; prose suffixes still read as English.
    """
    from src.promos.geo import parse_eligibility

    for code in ("OR", "ME", "IN"):
        _, inel, _ = parse_eligibility(
            f"Not available in NV; {code} residents are also excluded."
        )
        assert set(inel) == {"NV", code}
    # The English readings stay refused.
    _, inel, _ = parse_eligibility("Not available in NY; ID verification required at signup.")
    assert inel == ["NY"]
    el, _, _ = parse_eligibility("Offer available in NJ; OK to combine with other promotions.")
    assert el == ["NJ"]


def test_one_winner_cannot_absorb_two_promos_evidence() -> None:
    """The mirror of the many-winners rule: two vague primaries, one winner.

    At most one of the two dropped welcomes is the promo the winner
    describes, so their eligible lists must not union onto it.
    """
    from src.promos.collector import offer_confirmed_for_state
    from src.promos.redundancy import prefer_primary_offers

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    p1 = PromoOffer(
        source="fanduel",
        offer_id="p1",
        kind=PromoKind.SIGNUP_BONUS,
        title="FanDuel Welcome Offer",
        is_specific=False,
        eligible_regions=["IL"],
        observed_at=observed,
    )
    p2 = PromoOffer(
        source="fanduel",
        offer_id="p2",
        kind=PromoKind.SIGNUP_BONUS,
        title="FanDuel Welcome Bonus",
        is_specific=False,
        eligible_regions=["NJ"],
        observed_at=observed,
    )
    winner = PromoOffer(
        source="tl_fanduel",
        offer_id="s",
        kind=PromoKind.SIGNUP_BONUS,
        title="FanDuel Welcome Offer Bet $5 get $150 in Bonus Bets",
        summary="Bet $5, get $150 in bonus bets",
        bonus_amount=150.0,
        is_specific=True,
        observed_at=observed,
    )
    merged = prefer_primary_offers([p1, p2, winner])
    assert [o.offer_id for o in merged] == ["s"]
    kept = merged[0]
    assert "IL" not in kept.eligible_regions
    assert "NJ" not in kept.eligible_regions
    assert not offer_confirmed_for_state(kept, "IL")
    assert not offer_confirmed_for_state(kept, "NJ")


def test_window_elapsed_requires_same_sentence_start() -> None:
    """A different promo's start date must not stamp this one expired."""
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "Leaderboard Sprint started August 1 and has concluded. Separately, "
        "the New Year Parlay Special ends January 5.",
        observed,
    ) == datetime(2027, 1, 5, 23, 59, tzinfo=UTC)
    # Same sentence still reads as an elapsed window.
    assert extract_ends_at(
        "The Tournament will start on August 13 and end on August 15.",
        observed,
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)


def test_script_closer_with_whitespace_is_stripped() -> None:
    from src.promos.html_util import terms_slice, visible_text

    body = (
        "<script>var a = 1;</script ><p>Promo copy with $150.</p>"
        "<h3>Full Terms</h3><p>Must be 21+ and physically located in IL. "
        "Minimum odds -200 apply to the qualifying wager.</p>"
    )
    sliced = terms_slice(body)
    assert sliced is not None
    assert sliced.startswith("Must be 21+")
    assert "var a" not in visible_text(body)


def test_deepen_fallback_terms_are_visible_text() -> None:
    """The unmarked fallback must not store CSS or JS as terms."""
    from src.promos.deepen import deepen_offers

    page = (
        "<html><head><title>Welcome Package Bet $5 Get $200</title>"
        "<style>@font-face {font-family: 'Noto Sans Mono';}</style>"
        "<script>(function () { launch(); })();</script></head>"
        "<body>Bet $5, Get $200 in Bonus Bets today.</body></html>"
    )

    offer = PromoOffer(
        source="cloudbet",
        offer_id="1",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Package",
        url="https://example.test/promotions/welcome-package",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))
    )
    out = deepen_offers([offer], client=client)
    client.close()
    deepened = out[0]
    assert "@font-face" not in deepened.terms
    assert "function" not in deepened.terms
    assert "Bet $5, Get $200" in deepened.terms


def test_one_vague_primary_cannot_confirm_two_welcomes() -> None:
    """A loser folding into several winners may only narrow, never widen.

    One vague first-party welcome (adapter-stamped IL) soft-collides with two
    *different* specific TheLines welcomes; at most one of them is the promo
    the IL feed advertised, so neither may absorb the IL stamp.
    """
    from src.promos.collector import offer_confirmed_for_state
    from src.promos.redundancy import prefer_primary_offers

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    primary = PromoOffer(
        source="fanduel",
        offer_id="p",
        kind=PromoKind.SIGNUP_BONUS,
        title="FanDuel Welcome Offer",
        is_specific=False,
        eligible_regions=["IL"],
        observed_at=observed,
    )
    bet_get = PromoOffer(
        source="tl_fanduel",
        offer_id="s1",
        kind=PromoKind.SIGNUP_BONUS,
        title="FanDuel Welcome Offer Bet $5 get $150 in Bonus Bets",
        summary="Bet $5, get $150 in bonus bets",
        bonus_amount=150.0,
        is_specific=True,
        observed_at=observed,
    )
    no_sweat = PromoOffer(
        source="tl_fanduel",
        offer_id="s2",
        kind=PromoKind.RISK_FREE,
        title="FanDuel Welcome Offer No Sweat First Bet up to $1,000",
        summary="No Sweat First Bet up to $1,000",
        bonus_amount=1000.0,
        is_specific=True,
        observed_at=observed,
    )
    merged = prefer_primary_offers([primary, bet_get, no_sweat])
    kept = {o.offer_id: o for o in merged}
    assert set(kept) == {"s1", "s2"}
    for offer in kept.values():
        assert "IL" not in offer.eligible_regions
        assert not offer_confirmed_for_state(offer, "IL")


def test_terms_apply_boilerplate_is_not_a_heading() -> None:
    """Bolded "Terms apply." on a promo card must not mark a slice."""
    from src.promos.html_util import terms_slice

    body = (
        "<p>No Sweat First Bet up to $1,000.</p><strong>Terms apply.</strong>"
        "<div><h4>Casino Welcome Spins</h4>"
        "<p>Casino offer available in NJ, PA, MI and WV only. Wager 10x.</p></div>"
    )
    assert terms_slice(body) is None


def test_unclosed_script_never_leaks_into_terms() -> None:
    from src.promos.html_util import terms_slice

    body = (
        "<p>Promo copy here.</p>"
        '<script>var tpl = "<h3>Terms &amp; Conditions</h3>";'
        ' var stateList = ["NJ", "PA", "MI", "WV"]; analytics.track("promo");'
    )
    assert terms_slice(body) is None


def test_sitewide_terms_links_are_excluded() -> None:
    """A bare /terms path or /legal/ section is the house rules, not offer T&C."""
    from src.promos.html_util import terms_links

    body = (
        '<a href="/en-ca/terms">Terms and Conditions</a>'
        '<a href="/legal/promotions">Full terms</a>'
        '<a href="/promos/welcome/full-terms">Full terms</a>'
    )
    links = terms_links("https://example.test/promotions", body)
    assert links == ["https://example.test/promos/welcome/full-terms"]


def test_terms_url_fetch_never_replaces_parsed_terms() -> None:
    """An offer that already has terms never spends a fetch on its T&C link.

    The first live firing replaced four offers' genuine parsed terms with the
    venue's sitewide house rules — which naturally carry a Terms heading, so
    the marked-slice guard alone cannot protect the replace direction.
    """
    from src.promos.deepen import deepen_offers

    requested: list[str] = []
    house_rules = (
        "<html><body><h2>Terms and Conditions of Use of Services</h2>"
        "<p>Offered only to residents of New Jersey and Pennsylvania. "
        "These house rules govern every promotion.</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=house_rules)

    offer = PromoOffer(
        source="leovegas_on",
        offer_id="leoboost",
        kind=PromoKind.SIGNUP_BONUS,
        title="LeoBoost Weekly Loyalty Rewards",
        terms="The LeoBoost promotion runs in Ontario. Available in ON only.",
        url="https://example.test/promotions/leo-boost",
        metadata={"terms_url": "https://example.test/en-ca/terms"},
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = deepen_offers([offer], client=client)
    client.close()
    assert "https://example.test/en-ca/terms" not in requested
    deepened = out[0]
    assert deepened.terms.startswith("The LeoBoost promotion")
    assert "NJ" not in deepened.eligible_regions
    assert "deepened_terms_from" not in deepened.metadata


def test_elapsed_window_is_not_rolled_forward() -> None:
    """A stated past start date makes a year-less past end date decidable."""
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "The Tournament will start on August 13, 07:00 UTC and end on "
        "August 15, 06:59 UTC.",
        observed,
    ) == datetime(2026, 8, 15, 23, 59, tzinfo=UTC)
    # A future window is untouched…
    assert extract_ends_at(
        "The Tournament will start on September 13 and end on September 15.",
        observed,
    ) == datetime(2026, 9, 15, 23, 59, tzinfo=UTC)
    # …and with no stated start, the roll-forward stands.
    assert extract_ends_at("Offer ends August 15.", observed) == datetime(
        2027, 8, 15, 23, 59, tzinfo=UTC
    )


def test_chained_collision_keeps_all_evidence() -> None:
    """Evidence folded into a winner that is itself dropped follows the drop chain.

    A (vague, carries the brand's only ineligible list) loses to primary P;
    a more specific secondary B then beats P. Without chain re-routing, A's
    IL exclusion vanished with P and the kept row wrongly confirmed IL.
    """
    from src.promos.collector import offer_confirmed_for_state
    from src.promos.redundancy import prefer_primary_offers

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    loser_a = PromoOffer(
        source="tl_betmgm",
        offer_id="a",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Offer Bet $10 get $150",
        is_specific=False,
        ineligible_regions=["IL"],
        observed_at=observed,
    )
    primary = PromoOffer(
        source="betmgm",
        offer_id="p",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Offer Bet $10 get $150",
        summary="Bet $10 get $150",
        bonus_amount=150.0,
        is_specific=True,
        eligible_regions=["NJ"],
        observed_at=observed,
    )
    winner_b = PromoOffer(
        source="tl_betmgm",
        offer_id="b",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome Offer! Bet $10, get $150",
        summary="Bet $10, get $150 in bonus bets guaranteed",
        bonus_amount=150.0,
        is_specific=True,
        eligible_regions=["IL", "NJ"],
        observed_at=observed,
    )
    for ordering in ([loser_a, primary, winner_b], [winner_b, primary, loser_a]):
        merged = prefer_primary_offers(ordering)
        assert len(merged) == 1
        kept = merged[0]
        assert kept.offer_id == "b"
        assert kept.ineligible_regions == ["IL"]
        assert "IL" not in kept.eligible_regions
        assert not offer_confirmed_for_state(kept, "IL")
        assert "betmgm" in kept.metadata["merged_evidence_from"]


def test_ends_at_requires_a_real_end_verb_and_day() -> None:
    """"weekends 9/6/2026" and "Legends September 1" are not end dates."""
    from src.promos.enrich import extract_ends_at

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert extract_ends_at(
        "Profit boosts on weekends 9/6/2026 through 9/27/2026", observed
    ) is None
    assert extract_ends_at("Bet on NBA Legends September 1 matchup", observed) is None
    assert extract_ends_at("The offer extends September 1 pricing", observed) is None
    # A year alone is not a day ("20" out of "2026").
    assert extract_ends_at("Promotion ends September 2026", observed) is None
    # With the false early match gone, the genuine date is found.
    assert extract_ends_at(
        "Profit boost weekends 9/6/2026 and after. Offer ends 12/31/2026.", observed
    ) == datetime(2026, 12, 31, 23, 59, tzinfo=UTC)


def test_single_marker_welcome_section_is_not_split() -> None:
    """One ordinal marker is prose, not a multi-promo list — behavior unchanged."""
    from src.promos.thelines import _welcome_from_section

    text = (
        "BetMGM offers one promotion. The first: Bet $10 get $150 in Bonus "
        "Bets which is available in MI and NJ."
    )
    got = _welcome_from_section("BetMGM Bonus", text, ("betmgm",))
    assert len(got) == 1
    _, description = got[0]
    assert description == text[:500]


def test_split_spans_with_identical_titles_stay_distinct() -> None:
    """Same mechanics under two state lists must yield two offers, not one."""
    from src.promos.enrich import enrich_offer
    from src.promos.thelines import parse_thelines_offers

    body = (
        "<h3>BetMGM Bonus</h3><p>BetMGM offers two promotions. "
        "The first: Bet $10 get $150 in Bonus Bets which is available in "
        "MI, NJ, PA and WV. "
        "The second: Bet $10 get $150 in Bonus Bets which is valid in "
        "IL, AZ and CO only.</p>"
    )
    offers = parse_thelines_offers(
        body,
        source_key="tl_betmgm",
        brand_key="betmgm",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    welcome = [o for o in offers if o.raw_kind == "welcome"]
    assert len(welcome) == 2
    enriched = [enrich_offer(o) for o in welcome]
    regions = {tuple(o.eligible_regions) for o in enriched}
    assert ("MI", "NJ", "PA", "WV") in regions
    assert ("IL", "AZ", "CO") in regions


def test_failed_deepen_fetch_is_cached() -> None:
    """A shared failing URL costs one request, not one per offer."""
    from src.promos.deepen import deepen_offers

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not here")

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    offers = [
        PromoOffer(
            source="betmgm",
            offer_id=str(i),
            kind=PromoKind.SIGNUP_BONUS,
            title="Welcome offer",
            url="https://example.test/promotions",
            metadata={"terms_url": "https://example.test/promotions/terms"},
            observed_at=observed,
        )
        for i in range(3)
    ]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    deepen_offers(offers, client=client, max_details=12)
    client.close()
    assert calls["n"] == 2  # one per distinct URL, failures cached


def test_deepen_by_source_shares_a_passed_cache() -> None:
    """A batch-level cache makes the second state's deepen pass fetch nothing."""
    from src.promos.deepen import deepen_by_source

    calls = {"n": 0}
    page = (
        "<html><head><title>Bet $5 Get $200 in Bonus Bets</title>"
        '<meta name="description" content="Bet $5, Get $200 in Bonus Bets"/>'
        "</head><body>Bet $5, Get $200 in Bonus Bets. Min odds -200.</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=page)

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    def offer() -> PromoOffer:
        return PromoOffer(
            source="fanduel",
            offer_id="1",
            kind=PromoKind.SIGNUP_BONUS,
            title="Welcome offer",
            url="https://example.test/promotions",
            observed_at=observed,
        )

    cache: dict = {}
    client = httpx.Client(transport=httpx.MockTransport(handler))
    deepen_by_source([offer()], client=client, cache=cache)
    assert calls["n"] == 1
    deepen_by_source([offer()], client=client, cache=cache)
    client.close()
    assert calls["n"] == 1  # second pass served entirely from the shared cache


def test_unmarked_terms_body_never_replaces_or_fills_terms() -> None:
    """Only a Terms-heading-marked slice may come back from a terms link."""
    from src.promos.deepen import deepen_offers

    chrome = (
        "<html><head><title>Some Page</title></head>"
        "<body><nav>Promotions Casino Poker</nav>"
        "<p>Generic navigation chrome with $10 mentioned somewhere.</p></body></html>"
    )

    observed = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    with_terms = PromoOffer(
        source="betmgm",
        offer_id="kept",
        kind=PromoKind.SIGNUP_BONUS,
        title="Welcome offer",
        terms="Existing genuine terms. Must be 21+ and located in IL.",
        url="https://example.test/promotions/welcome/terms",
        metadata={"terms_url": "https://example.test/promotions/welcome/terms"},
        observed_at=observed,
    )
    without_terms = with_terms.model_copy(update={"offer_id": "empty", "terms": ""})
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=chrome))
    )
    out = deepen_offers([with_terms, without_terms], client=client)
    client.close()
    kept, empty = out
    assert kept.terms == "Existing genuine terms. Must be 21+ and located in IL."
    assert "deepened_terms_from" not in kept.metadata
    assert empty.terms == ""
    assert "deepened_terms_from" not in empty.metadata


def test_terms_slice_ignores_footer_links_and_scripts() -> None:
    """Non-heading mentions of the phrase must not anchor a slice."""
    from src.promos.html_util import terms_slice

    body = (
        '<p>Bet $5 get $150 in Bonus Bets. Available in AZ, IL and NJ.</p>'
        '<script>var i18n = {"label": "terms and conditions apply to all"};</script>'
        '<footer><a href="/promo-terms">Terms and Conditions</a>'
        "© 2026 ExampleBook. Gambling problem? Call 1-800-GAMBLER.</footer>"
    )
    assert terms_slice(body) is None


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


def test_thelines_welcome_splits_multi_promo_sections() -> None:
    """One review section, two promos, two state lists — two offers.

    The fixture's BetMGM section describes "The first: … $1,500 paid back …
    valid in … IL …" and "The second: Bet $10 get $150 … available in MI, NJ,
    PA and WV". Parsed as one blob, both lists landed on one offer and the
    MI/NJ/PA/WV-only promo stamped IL-confirmed on the first live run.
    """
    from src.promos.enrich import enrich_offer
    from src.promos.thelines import parse_thelines_offers

    body = (FIXTURES / "thelines_sportsbook_promos.html").read_text()
    offers = parse_thelines_offers(
        body,
        source_key="tl_betmgm",
        brand_key="betmgm",
        observed_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    welcome = [o for o in offers if o.raw_kind == "welcome"]
    assert len(welcome) == 2
    enriched = {o.title: enrich_offer(o) for o in welcome}

    win = next(v for k, v in enriched.items() if "if you win" in k)
    assert win.eligible_regions == ["MI", "NJ", "PA", "WV"]
    assert "IL" not in win.eligible_regions

    paid_back = next(v for k, v in enriched.items() if "1,500" in k)
    assert "IL" in paid_back.eligible_regions
    assert "DC" in paid_back.eligible_regions
    assert "MI" not in paid_back.eligible_regions


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


# ── first-contact captures, 2026-08-16 ───────────────────────────────────────
# Genuine bodies from the first live IL collection (batch run #1), extracted
# verbatim from the RawStore envelopes under data/raw_promos/.  Four adapter
# families had never met production HTML before that run; these pin what each
# actually served — including the refusals, which are captures too: a block
# page that ever parses into offers is the failure these exist to catch.


def _first_contact(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip("fixture not captured yet")
    return path.read_text()


def test_fanduel_parses_empty_merchandising_as_empty() -> None:
    """FanDuel's IL merchandising API answered 200 with zero promotions.

    A genuine empty is not a failure and must not crash: the marketing page
    was blocked by PerimeterX on the same run, so this body is the whole of
    what FanDuel serves an anonymous IL request.
    """
    from src.promos.fanduel import FanDuelPromoAdapter

    adapter = FanDuelPromoAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        region="IL",
    )
    outcome = adapter.parse(
        [_raw("fanduel", "merchandising-il", _first_contact("fanduel_merchandising_il.json"))]
    )
    adapter.close()
    assert outcome.offers == []


def test_fanduel_invents_nothing_from_the_perimeterx_page() -> None:
    """The blocked marketing page must parse to zero offers, never to rows."""
    from src.promos.fanduel import FanDuelPromoAdapter

    adapter = FanDuelPromoAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        region="IL",
    )
    body = _first_contact("fanduel_promotions_page_perimeterx.html")
    outcome = adapter.parse([_raw("fanduel", "promotions-page", body)])
    adapter.close()
    assert outcome.offers == []


def test_bovada_parse_fixture_first_contact() -> None:
    from src.promos.bovada import BovadaPromoAdapter

    body = _first_contact("bovada_promotions_index.html")
    adapter = BovadaPromoAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    outcome = adapter.parse([_raw("bovada", "promotions-index", body)])
    adapter.close()
    # The live run parsed five offers from this exact body.
    assert len(outcome.offers) >= 4
    assert all(o.source == "bovada" for o in outcome.offers)
    titles = " ".join(o.title for o in outcome.offers).lower()
    assert "welcome" in titles


def test_betmgm_catalog_parse_fixture_first_contact() -> None:
    """The html_catalog family's first genuine capture — 12 offers live."""
    from src.promos.html_catalog import HtmlCatalogPromoAdapter

    body = _first_contact("betmgm_promotions_index.html")
    adapter = HtmlCatalogPromoAdapter(
        source_key="betmgm",
        index_url="https://sports.betmgm.com/en/promo/sports",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    outcome = adapter.parse([_raw("betmgm", "promotions-index", body)])
    adapter.close()
    assert len(outcome.offers) >= 8
    assert all(o.source == "betmgm" for o in outcome.offers)


def test_detail_page_without_terms_heading_stores_visible_text_not_chrome() -> None:
    """A detail page with no Terms heading falls back to whole-page text —
    through the script-stripped path, so style/script bodies never store as
    terms.  This branch had no pin, which let it ship calling a helper the
    module never imported: every promo test's detail body carried a heading,
    so the first live page without one would have raised ``NameError``."""
    from src.promos.html_catalog import HtmlCatalogPromoAdapter

    body = (
        "<html><head><title>Bet $5 Get $150 in Bonus Bets</title>"
        '<style>@font-face { font-family: "x"; }</style>'
        "<script>var stale = ['MI', 'WV'];</script></head>"
        "<body><p>Opt in and place a $5 bet to receive $150 in bonus bets. "
        "Not available in NV.</p></body></html>"
    )
    adapter = HtmlCatalogPromoAdapter(
        source_key="betmgm",
        index_url="https://sports.betmgm.com/en/promo/sports",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    outcome = adapter.parse([_raw("betmgm", "promotions-detail-x", body)])
    adapter.close()
    assert len(outcome.offers) == 1
    terms = outcome.offers[0].terms
    assert "bonus bets" in terms.lower()
    assert "@font-face" not in terms
    assert "stale" not in terms


def test_bet365_challenge_page_parses_to_nothing() -> None:
    """bet365 served a Cloudflare challenge; the catalog parser must not read
    a bot wall as a promotions list."""
    from src.promos.html_catalog import HtmlCatalogPromoAdapter

    body = _first_contact("bet365_promotions_index_challenge.html")
    adapter = HtmlCatalogPromoAdapter(
        source_key="bet365",
        index_url="https://extra.bet365.com/promotions",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    outcome = adapter.parse([_raw("bet365", "promotions-index", body)])
    adapter.close()
    assert outcome.offers == []


def test_caesars_403_body_is_refused_at_the_client_not_read_as_a_catalog() -> None:
    """Caesars answered 403 with a bot-script body.

    The body itself *would* parse — its navigation links look enough like a
    catalog that the parser reads two phantom offers out of it.  The defense
    is therefore the fetch guard, which is where the live run refused it
    (``FAILED [blocked]``), and this pins that: the same bytes at the same
    status never reach the parser.
    """
    from src.promos.html_catalog import HtmlCatalogPromoAdapter
    from src.sources.guards import SourceError

    body = _first_contact("caesars_promotions_index_403.html")
    adapter = HtmlCatalogPromoAdapter(
        source_key="caesars",
        index_url="https://www.caesars.com/sportsbook-and-casino/promos",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(403, text=body))
        ),
    )
    with pytest.raises(SourceError):
        adapter.fetch_raw()
    adapter.close()


def test_hardrock_404_body_is_refused_at_the_client_not_read_as_a_catalog() -> None:
    """``www.hardrock.bet/promotions`` answers 404 with a 396 KiB page shell.

    The body *would* parse — two phantom casino-blog offers ("Online Casino
    Bonuses Explained", "Bonus Buy Slots Explained") — so the fetch guard's
    404 refusal is the defense, and this pins it: the same bytes at the same
    status never reach the parser.  Captured 2026-08-16 during the path
    rediscovery probe (``docs/evidence/promos.md``); the host is also the
    wrong one — every working Hard Rock module uses ``app.hardrock.bet``.
    """
    from src.promos.html_catalog import HtmlCatalogPromoAdapter
    from src.sources.guards import SourceError

    from src.raw_store import RawResponse

    body = _first_contact("hardrock_promotions_404.html")
    adapter = HtmlCatalogPromoAdapter(
        source_key="hardrock",
        index_url="https://www.hardrock.bet/promotions",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404, text=body))
        ),
    )
    with pytest.raises(SourceError):
        adapter.fetch_raw()
    # The docstring's claim, pinned: at status 200 these bytes would read as
    # two phantom casino-blog offers — which also guards fixture integrity
    # (a truncated body stops producing them).
    outcome = adapter.parse(
        [
            RawResponse(
                source="hardrock",
                endpoint="promotions-index",
                url="https://www.hardrock.bet/promotions",
                status_code=200,
                body=body,
                fetched_at=datetime(2026, 8, 16, 20, 40, tzinfo=UTC),
                content_type="text/html",
            )
        ]
    )
    adapter.close()
    assert {o.title for o in outcome.offers} == {
        "Online Casino Bonuses Explained",
        "Bonus Buy Slots Explained",
    }


def test_hardrock_app_shell_parses_to_nothing() -> None:
    """``app.hardrock.bet/en-us/promotions`` answers 200 with a JS shell.

    The route exists on the venue's real host, but the catalog is
    client-rendered — the shell carries zero promo words and must parse to
    zero offers with ``no_promo_signals``, never phantom cards.  Pinned so a
    future registry switch to this URL grades honestly.
    """
    from datetime import UTC as _UTC

    from src.promos.html_catalog import HtmlCatalogPromoAdapter
    from src.raw_store import RawResponse

    body = _first_contact("hardrock_promotions_app_shell.html")
    # Shell genuineness — an empty body would also parse to nothing.
    assert "You need to enable JavaScript" in body
    adapter = HtmlCatalogPromoAdapter(
        source_key="hardrock",
        index_url="https://app.hardrock.bet/en-us/promotions",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ),
    )
    outcome = adapter.parse(
        [
            RawResponse(
                source="hardrock",
                endpoint="promotions-index",
                url="https://app.hardrock.bet/en-us/promotions",
                status_code=200,
                body=body,
                fetched_at=datetime(2026, 8, 16, 20, 40, tzinfo=_UTC),
                content_type="text/html",
            )
        ]
    )
    adapter.close()
    assert outcome.offers == []
    assert outcome.skipped["no_promo_signals"] == 1


def test_betrivers_landing_parse_fixture_first_contact() -> None:
    """The landing family's first genuine capture.  Zero offers with one
    counted skip is what the live run produced from this body — pinned so a
    silent change in either direction is visible."""
    from src.promos.landing import LandingPromoAdapter, LandingTarget

    body = _first_contact("betrivers_landing_il.html")
    adapter = LandingPromoAdapter(
        source_key="betrivers_kambi",
        targets=(
            LandingTarget(
                "https://il.betrivers.com/", "landing-il", "BetRivers IL", region="IL"
            ),
        ),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        empty_is_ok=True,
    )
    outcome = adapter.parse([_raw("betrivers_kambi", "landing-il", body)])
    adapter.close()
    assert outcome.offers == []
    assert sum(outcome.skipped.values()) >= 1


def test_pinnacle_home_parse_fixture_first_contact() -> None:
    from src.promos.landing import LandingPromoAdapter, LandingTarget

    body = _first_contact("pinnacle_home.html")
    adapter = LandingPromoAdapter(
        source_key="pinnacle",
        targets=(LandingTarget("https://www.pinnacle.com/en/", "home", "Pinnacle"),),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        empty_is_ok=True,
    )
    outcome = adapter.parse([_raw("pinnacle", "home", body)])
    adapter.close()
    assert outcome.offers == []


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
        line = next(line for line in out.splitlines() if "worst " in line)
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

    def test_odds_run_override_chooses_among_same_state_runs_only(
        self, tmp_path, monkeypatch, capsys
    ):
        """``--odds-run`` exists because "latest" can be a thin probe run, and
        it must never defeat state matching: IL promos priced against a PA
        slate would quote hedges a different licence answered for."""
        from src import settings as settings_mod
        from src.store import Store

        self._seed(tmp_path, monkeypatch)
        # The seeded odds run is #1 and ungoverned; name it explicitly.
        assert self._run(["plan", "--odds-run", "1"]) == 0
        out = capsys.readouterr().out
        assert "odds run #1" in out

        assert self._run(["plan", "--odds-run", "999"]) == 1
        assert "no odds run #999 is stored" in capsys.readouterr().err

        # A governed promo run against a differently-governed odds run refuses.
        odds = Store(settings_mod.DB_PATH)
        pa_odds = odds.start_run(datetime.now(UTC), jurisdiction="PA")
        odds.close()
        promos = PromoStore(settings_mod.PROMO_DB_PATH)
        il_promos = promos.start_run(jurisdiction="IL")
        promos.finish_run(il_promos, ok=True, offers=[], health=[])
        promos.close()
        assert self._run(["plan", "--run", str(il_promos), "--odds-run", str(pa_odds)]) == 1
        err = capsys.readouterr().err
        assert "refusing to price one state's offers against another's slate" in err

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

    def test_the_cli_measures_the_gate_under_the_odds_runs_state(
        self, tmp_path, monkeypatch,
    ):
        """The promos CLI's gate site, held by capture.

        Round 5 threaded the odds run's jurisdiction into this site and a
        round-7 reviewer proved the revert left 1,500 tests green — the
        existing gate test's seeded run is state-blind, so it cannot tell the
        run's set from the ambient one.  A PA odds run can: PA's view-only set
        differs from the ungoverned fallback (it adds hardrock), and that
        premise is asserted rather than assumed.
        """
        import sqlite3 as _sqlite3

        import src.arb as arb_mod
        from src import settings as settings_mod
        from src.sources import registry

        self._seed(tmp_path, monkeypatch)
        with _sqlite3.connect(settings_mod.DB_PATH) as connection:
            connection.execute(
                "UPDATE collection_run SET jurisdiction='PA', route_scope='state'"
            )

        pa_set = registry.view_only_for_run("PA")
        assert pa_set != registry.view_only_for_run(""), (
            "the discriminating premise of this pin"
        )
        # ...and the ambient set is forced to a sentinel, or the pin goes
        # blind under ODDS_STATE=PA: with the reader configured for PA the
        # ambient set *equals* pa_set, and a regression to ambient grading —
        # the exact class this pin holds — passed unseen.  A set no state
        # resolver can produce makes call-time ambient reads distinguishable
        # under every reader configuration.
        #
        # The planner is imported FIRST, because the CLI imports it lazily and
        # the planner binds the ambient set by value at module top: if this
        # test is the first in the process to trigger that import, the module
        # body executes inside the patched window and the sentinel freezes
        # into the planner's copy forever — monkeypatch restores the registry,
        # not another module's cache.
        import src.promos.planner  # noqa: F401 — bind its ambient copy pre-sentinel

        sentinel = frozenset({"__ambient_sentinel__"})
        monkeypatch.setattr(registry, "VIEW_ONLY_SOURCES", sentinel)
        assert pa_set != sentinel

        captured: list = []
        real = arb_mod.counterparty_groups

        def recording(quotes, *, mirrors=None, view_only=None):
            captured.append(view_only)
            return real(quotes, mirrors=mirrors, view_only=view_only)

        # The CLI imports the symbol function-locally from src.arb at call
        # time, so patching the arb namespace intercepts it.
        monkeypatch.setattr(arb_mod, "counterparty_groups", recording)
        assert self._run(["plan"]) == 0
        assert captured == [pa_set], (
            "the promos CLI must measure its gate under the odds run's own set"
        )
        # The sentinel intercepts only call-time reads.  The regression form
        # the sentinel cannot see — and the dominant style in this codebase —
        # is a module-top `from ... import VIEW_ONLY_SOURCES`, which binds the
        # real ambient set before any patch exists whenever the module was
        # already imported.  That form's signature is the name appearing in
        # the module's own globals, which this refuses directly.
        import sys

        assert "VIEW_ONLY_SOURCES" not in vars(sys.modules["src.promos.collector"]), (
            "the promos CLI gained an import-time ambient binding, which no "
            "call-time sentinel can distinguish from the run's set on a "
            "matching-state box"
        )

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
        header = next(line for line in out.splitlines() if " at " in line and "—" in line)
        away_team = header.split(" at ")[0].strip()
        assert away_team == "Philadelphia Phillies", header
        promo = next(line for line in out.splitlines()
                     if line.startswith("    promo ") and "@" in line)
        assert " away " in promo, promo
        assert "3.000" in promo, promo


# ── betPARX and theScore Bet: the two Pennsylvania-only books, first-party ────


def test_betparx_parse_fixture_pa() -> None:
    """The Playtech configuration captured from ``pa.betparx.com`` on 2026-08-23.

    Six promotions on the lobby: four sportsbook (the bet-insurance sign-up
    offer and three weekly Eagles boosts), one casino, one promo-code stub with
    no public terms.  The casino tile is a counted skip, never an offer; every
    offer carries PA as its region because the host is the licence; the
    sign-up offer's terms come from the web-content block the page itself
    fetches, and its window is read off the scheduler.
    """
    from src.promos.betparx import BetParxPromoAdapter

    config = _first_contact("betparx_promotions_configuration_pa.json")
    learn_more = _first_contact("betparx_webcontent_bet_insurance_learn_more_pa.html")
    terms = _first_contact("betparx_webcontent_bet_insurance_terms_pa.html")
    adapter = BetParxPromoAdapter(
        base_url="https://pa.betparx.com",
        region="PA",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    outcome = adapter.parse(
        [
            _raw("betparx_kambi", "promotions-configuration", config),
            _raw("betparx_kambi", "webcontent-BET_INSURANCE_SIGNUP_OFFER_LEARN_MORE", learn_more),
            _raw("betparx_kambi", "webcontent-TC_BET_INSURANCE_SIGNUP_OFFER", terms),
        ]
    )
    adapter.close()
    assert outcome.rejections == []
    assert outcome.skipped == {"product:casino": 1}
    assert len(outcome.offers) == 5
    assert all(o.source == "betparx_kambi" for o in outcome.offers)
    assert all(o.eligible_regions == ["PA"] for o in outcome.offers)
    signup = next(o for o in outcome.offers if "Bet Insurance" in o.title)
    assert signup.kind is PromoKind.RISK_FREE
    assert "first eligible sports bet loses" in signup.terms
    assert "deposit of at least $10" in signup.description
    assert signup.url == "https://pa.betparx.com/bet_insurance_signup"
    assert signup.starts_at is not None and signup.ends_at is not None
    assert signup.starts_at.isoformat().startswith("2026-07-20")
    assert signup.ends_at.isoformat().startswith("2026-08-20")
    # The boosts had no web content in this capture: title and window only.
    boost = next(o for o in outcome.offers if "Birds +50" in o.title)
    assert boost.terms == "" and boost.url == "https://pa.betparx.com/birds_plus50"


def test_betparx_skips_login_only_and_refuses_a_configuration_without_promotions() -> None:
    from src.promos.betparx import BetParxPromoAdapter
    from src.sources.guards import FormatChangeError

    adapter = BetParxPromoAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    body = json.dumps(
        {
            "promotions": {
                "Members": {
                    "id": "m1", "name": "Members only", "description": "Bet $5 get $20",
                    "product": "sportsbook", "isEnabledForGuest": False,
                },
                "Open": {
                    "id": "o1", "name": "Open", "description": "Bet $5 get $20 in bonus bets",
                    "product": "sportsbook", "isEnabledForGuest": True, "scheduler": [],
                    "tileSettings": {"imageActionPageURL": "/open"},
                },
            }
        }
    )
    outcome = adapter.parse([_raw("betparx_kambi", "promotions-configuration", body)])
    assert [o.offer_id for o in outcome.offers] == ["o1"]
    assert outcome.skipped == {"login_only": 1}
    with pytest.raises(FormatChangeError):
        adapter.parse([_raw("betparx_kambi", "promotions-configuration", json.dumps({"x": 1}))])
    adapter.close()


def test_thescore_parse_fixture() -> None:
    """Two articles from the help centre's promotional-terms section, 2026-08-23.

    The bet-and-get is an invitation-only casino cross-sell and says so in its
    first paragraph; the live bet-and-get is a $10 Bet Reset.  Both carry the
    state clause enrich reads eligibility from, and neither needs the index
    to parse — the index only names the articles to fetch.
    """
    from src.promos.thescore import TheScorePromoAdapter

    adapter = TheScorePromoAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    index = _first_contact("thescore_promo_terms_index.html")
    urls = adapter._article_urls(index)
    assert len(urls) == 10, [u for u, _ in urls]
    assert all(u.startswith("https://sportsbook.thescore.bet/hc/en-us/articles/") for u, _ in urls)
    assert not any("Responsible-Gaming" in u or "Promotional-Terms-and-Conditions" in u for u, _ in urls)

    bet10 = _first_contact("thescore_promo_terms_article_bet10_get30.html")
    live = _first_contact("thescore_promo_terms_article_live_bet_get.html")
    outcome = adapter.parse(
        [
            _raw("thescore", "promo-terms-index", index),
            _raw("thescore", "promo-terms-article-48263461087117", bet10,
                 url="https://sportsbook.thescore.bet/hc/en-us/articles/48263461087117-Bet-10-Get-30-Bonus-Bet"),
            _raw("thescore", "promo-terms-article-47882363775501", live,
                 url="https://sportsbook.thescore.bet/hc/en-us/articles/47882363775501-Live-Bet-Get"),
        ]
    )
    adapter.close()
    assert outcome.rejections == []
    by_id = {o.offer_id: o for o in outcome.offers}
    assert set(by_id) == {"48263461087117", "47882363775501"}
    first, second = by_id["48263461087117"], by_id["47882363775501"]
    assert first.title == "Bet $10, Get $30 Bonus Bet"
    assert first.kind is PromoKind.BONUS_BET
    assert second.kind is PromoKind.NO_SWEAT  # a Bet Reset, classified off the terms
    assert first.eligibility_notes == "invited players only"
    assert "physically present in MI, NJ, PA, or WV" in first.terms
    assert second.title == "Live Bet & Get"
    assert "Bet Reset" in second.terms
    assert second.url.endswith("/47882363775501-Live-Bet-Get")


def test_registry_routes_betparx_per_state_and_thescore_globally() -> None:
    from src.promos.registry import global_promo_sources, state_promo_sources_for_state

    assert any(entry.key == "thescore" for entry in global_promo_sources())
    assert not any(entry.key == "betparx_kambi" for entry in global_promo_sources())
    pa = {entry.key: entry.config for entry in state_promo_sources_for_state("PA")}
    nj = {entry.key: entry.config for entry in state_promo_sources_for_state("NJ")}
    assert pa["betparx_kambi"] == {"base_url": "https://pa.betparx.com/", "region": "PA"}
    assert nj["betparx_kambi"] == {"base_url": "https://nj.betparx.com/", "region": "NJ"}
    # No Illinois or DC licence, so no lobby to read and no entry built.
    assert "betparx_kambi" not in {e.key for e in state_promo_sources_for_state("IL")}
    assert "betparx_kambi" not in {e.key for e in state_promo_sources_for_state("DC")}
