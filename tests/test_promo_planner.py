"""The promo planner: concrete usage plans from the book's own scraped games.

Every dollar figure asserted here is hand-computed from the closed forms in
:mod:`src.promos.planner`'s mode branches, never read back from the code under
test.  The gates are asserted through the counted ``skipped`` reasons, because
"no plan" and "no plan, for a stated reason" are different facts.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from src.arb import find_opportunities
from src.promos.planner import (
    MAX_PLANS_PER_OFFER,
    PLAN_UNIT,
    build_promo_plans,
    parse_boost_percent,
    parse_min_odds,
    parse_qualifying_stake,
    parse_wagering_multiple,
    stakeable_odds_sources,
)
from src.promos.schema import PromoKind, PromoOffer
from src.schema import Market, Period, Selection, Sport, QuoteStatus

from .conftest import make_quote

AS_OF = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)


def _offer(**overrides) -> dict:
    base = dict(
        source="draftkings",
        offer_id="offer-1",
        kind="bonus_bet",
        title="Bonus bet drop",
        summary="",
        description="",
        reward_type="bonus_bets",
        bonus_amount=100.0,
        min_odds=None,
        wagering_requirement=None,
    )
    base.update(overrides)
    return base


def _plans(offers, quotes, **kwargs):
    kwargs.setdefault("as_of", AS_OF)
    kwargs.setdefault("commissions", {})
    kwargs.setdefault("one_counterparty", {})
    return build_promo_plans(offers, quotes, **kwargs)


def _the_plan(out, key="draftkings|offer-1"):
    return out["plans"][key]


class TestOfferTextParsers:
    def test_min_odds_american_negative(self):
        assert parse_min_odds("-200") == pytest.approx(1.5)

    def test_min_odds_american_positive(self):
        assert parse_min_odds("+150") == pytest.approx(2.5)

    def test_min_odds_decimal(self):
        assert parse_min_odds("1.5") == pytest.approx(1.5)

    def test_min_odds_bare_american_shorthand(self):
        # Venues write "min odds 200" meaning +200.
        assert parse_min_odds("200") == pytest.approx(3.0)

    def test_min_odds_rejects_junk_and_subunity(self):
        assert parse_min_odds("evens or better") is None
        assert parse_min_odds("0.5") is None
        assert parse_min_odds(None) is None
        assert parse_min_odds("") is None

    def test_qualifying_stake_from_canonical_summary(self):
        assert parse_qualifying_stake("Bet $5, get $150 in bonus bets") == pytest.approx(5.0)
        assert parse_qualifying_stake("Bet $1,000, get $200 in bonus") == pytest.approx(1000.0)

    def test_qualifying_stake_rejects_other_shapes(self):
        assert parse_qualifying_stake("Get $150 in bonus bets") is None
        assert parse_qualifying_stake("") is None
        assert parse_qualifying_stake(None) is None

    def test_boost_percent(self):
        assert parse_boost_percent("50% profit boost") == pytest.approx(50.0)
        assert parse_boost_percent(None, "No-brainer 33% odds boost today") == pytest.approx(33.0)
        assert parse_boost_percent("boost your winnings") is None
        # A "400% boost" is promo copy for a deposit match, not a price boost.
        assert parse_boost_percent("400% boost") is None

    def test_wagering_multiple(self):
        assert parse_wagering_multiple("1x") == pytest.approx(1.0)
        assert parse_wagering_multiple("10x deposit") == pytest.approx(10.0)
        assert parse_wagering_multiple("none stated") is None


class TestStakeableSources:
    """The promo is only usable at its own book — the source mapping is the rule."""

    def test_thelines_tenant_maps_to_the_brand(self):
        assert stakeable_odds_sources("tl_draftkings") == ("draftkings", "an_draftkings")

    def test_primary_comes_before_failover(self):
        assert stakeable_odds_sources("fanduel") == ("fanduel", "an_fanduel")

    def test_promo_only_brand_uses_its_action_network_view(self):
        assert stakeable_odds_sources("bet365") == ("an_bet365",)
        assert stakeable_odds_sources("fanatics") == ("an_fanatics",)

    def test_ontario_tenant_has_no_feed_and_says_so(self):
        # betmgm_on is a different licence with a different catalog; borrowing
        # the US feed's games would name bets the Ontario book may not offer.
        assert stakeable_odds_sources("betmgm_on") == ()

    def test_view_only_sources_are_never_stakeable(self):
        assert "an_open" not in stakeable_odds_sources("open")


class TestBonusConversionArithmetic:
    """$100 credit at 3.0 hedged at 1.5 → h = 200/1.5 = $133.33, floor $66.66."""

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    def test_two_way_conversion_hand_case(self):
        out = _plans([_offer()], self._slate())
        plan = _the_plan(out)
        assert plan["strategy"] == "bonus_conversion"
        assert len(plan["plans"]) == 1
        best = plan["plans"][0]
        promo, hedge = best["legs"]
        assert promo["role"] == "promo"
        assert promo["source"] == "draftkings"
        assert promo["stake_kind"] == "bonus"
        assert promo["stake"] == pytest.approx(100.0)
        assert hedge["source"] == "fanduel"
        assert hedge["stake"] == pytest.approx(133.33)
        # Floor is measured from the rounded stakes: min(200-133.33, 133.33*0.5)
        assert best["guaranteed_cash"] == pytest.approx(66.66)
        assert best["conversion_pct"] == pytest.approx(66.66)

    def test_unstated_amount_scales_per_100_and_says_so(self):
        out = _plans([_offer(bonus_amount=None)], self._slate())
        plan = _the_plan(out)
        assert plan["unit"] == {"kind": "bonus_credit", "amount": 100.0, "assumed": True}
        assert any("per $100" in c for c in plan["caveats"])

    def test_stated_amount_rescales_the_stakes(self):
        out = _plans([_offer(bonus_amount=50.0)], self._slate())
        best = _the_plan(out)["plans"][0]
        assert best["legs"][0]["stake"] == pytest.approx(50.0)
        assert best["legs"][1]["stake"] == pytest.approx(66.67)
        # min(100 - 66.67, 66.67*1.5 - 66.67) = min(33.33, 33.335)
        assert best["guaranteed_cash"] == pytest.approx(33.33)
        # The rate is a property of the prices, not of the amount.
        assert best["conversion_pct"] == pytest.approx(66.66)

    def test_three_way_conversion_hedges_both_other_outcomes(self):
        """Soccer: the longest side converts best, and both others get hedged.

        Credit on DRAW 4.4 → T = 340; h_home = 340/1.88 = 180.85, h_away =
        340/3.6 = 94.44; every outcome pays 340 − 275.29 = 64.71.  That beats
        putting the credit on AWAY 3.7 (63.59), and the planner must discover it
        rather than take the first side it saw.

        Both books hold an edge on their own three-way market — a book whose own
        prices sum below 1.0 has been mispaired by the parser and is excluded,
        so an unrealistic fixture would test nothing but that exclusion.
        """
        quotes = [
            make_quote(
                source="draftkings", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.AWAY, decimal_odds=3.7,
            ),
            make_quote(
                source="draftkings", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.HOME, decimal_odds=1.90,
            ),
            make_quote(
                source="draftkings", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.DRAW, decimal_odds=4.4,
            ),
            make_quote(
                source="fanduel", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.HOME, decimal_odds=1.88,
            ),
            make_quote(
                source="fanduel", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.DRAW, decimal_odds=4.3,
            ),
            make_quote(
                source="fanduel", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.AWAY, decimal_odds=3.6,
            ),
        ]
        out = _plans([_offer()], quotes)
        ranked = _the_plan(out)["plans"]
        best = ranked[0]
        assert best["legs"][0]["role"] == "promo"
        assert best["legs"][0]["source"] == "draftkings"
        assert best["legs"][0]["selection"] == "draw"
        stakes = {leg["selection"]: leg["stake"] for leg in best["legs"]}
        assert stakes["home"] == pytest.approx(180.85)
        assert stakes["away"] == pytest.approx(94.44)
        assert best["guaranteed_cash"] == pytest.approx(64.69)
        assert {label for label, _ in best["outcome_profits"]} == {"away", "draw", "home"}
        # The runner-up is the credit on AWAY 3.7: T = 270, hedges 143.62 at
        # 1.88 and 62.79 at 4.3, every outcome 270 − 206.41 = 63.59.
        away = next(p for p in ranked if p["legs"][0]["selection"] == "away")
        assert away["guaranteed_cash"] == pytest.approx(63.59)

    def test_push_window_floor_is_zero_but_ranked_on_settled(self):
        """An NFL two-way moneyline can void on a tie: guaranteed 0, settled 66.66.

        Ranking on the all-outcomes floor would score every pushable market as
        zero and bury the true ordering; the panel shows both numbers.
        """
        quotes = [
            make_quote(
                source="draftkings", sport=Sport.FOOTBALL, league="NFL",
                event_key="NFL-PHI@NFL-DAL:2026-07-28",
                selection=Selection.AWAY, decimal_odds=3.0,
            ),
            make_quote(
                source="fanduel", sport=Sport.FOOTBALL, league="NFL",
                event_key="NFL-PHI@NFL-DAL:2026-07-28",
                selection=Selection.HOME, decimal_odds=1.5,
            ),
        ]
        out = _plans([_offer()], quotes)
        best = _the_plan(out)["plans"][0]
        labels = dict(best["outcome_profits"])
        assert labels["push"] == pytest.approx(0.0)
        assert best["guaranteed_cash"] == pytest.approx(0.0)
        assert best["settled_cash"] == pytest.approx(66.66)
        assert best["conversion_pct"] == pytest.approx(66.66)


class TestThePromoLegStaysOnItsOwnBook:
    def test_a_better_price_elsewhere_is_never_the_promo_leg(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            # Pinnacle prices the same side longer — tempting, and not usable:
            # the credit only exists at DraftKings.
            make_quote(source="pinnacle", selection=Selection.AWAY, decimal_odds=3.4),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        out = _plans([_offer()], quotes)
        best = _the_plan(out)["plans"][0]
        assert best["legs"][0]["role"] == "promo"
        assert best["legs"][0]["source"] == "draftkings"
        assert best["legs"][0]["decimal_odds"] == pytest.approx(3.0)

    def test_failover_rows_stand_in_when_the_first_party_feed_is_dry(self):
        quotes = [
            make_quote(source="an_bet365", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        offer = _offer(source="bet365", offer_id="b1")
        out = _plans([offer], quotes)
        plan = out["plans"]["bet365|b1"]
        assert plan["plans"], plan
        assert plan["plans"][0]["legs"][0]["source"] == "an_bet365"
        assert any("Action Network" in c for c in plan["caveats"])

    def test_a_book_with_no_feed_falls_back_to_text(self):
        out = _plans([_offer(source="betmgm_on", offer_id="on1")],
                     [make_quote(source="fanduel", selection=Selection.HOME)])
        plan = out["plans"]["betmgm_on|on1"]
        assert plan["strategy"] == "no_odds_coverage"
        assert plan["plans"] == []

    def test_a_book_absent_from_this_run_falls_back_to_text(self):
        out = _plans([_offer()], [make_quote(source="fanduel", selection=Selection.HOME)])
        plan = _the_plan(out)
        assert plan["strategy"] == "no_odds_coverage"
        assert plan["plans"] == []


class TestHedgeGates:
    """A hedge that can be voided, mispaired, or is the same house is not a hedge."""

    def test_the_promo_books_own_failover_feed_is_never_a_hedge(self):
        # an_draftkings is DraftKings' own failover feed. Its rows are
        # promo-side prices, so when it alone holds the other side there is no
        # hedge at all — a fact counted as ``no_hedge_price`` rather than
        # silently producing a "hedge" at the same house.
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="an_draftkings", selection=Selection.HOME, decimal_odds=1.5),
        ]
        out = _plans([_offer()], quotes)
        plan = _the_plan(out)
        assert plan["plans"] == []
        assert plan["skipped"].get("no_hedge_price", 0) >= 1

    def test_a_measured_mirror_is_refused_as_a_hedge(self):
        quotes = [
            make_quote(source="betrivers_kambi", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="leovegas_kambi", selection=Selection.HOME, decimal_odds=1.5),
        ]
        out = _plans(
            [_offer(source="betrivers_kambi", offer_id="br1")],
            quotes,
            one_counterparty={"MLB": [frozenset({"betrivers_kambi", "leovegas_kambi"})]},
        )
        plan = out["plans"]["betrivers_kambi|br1"]
        assert plan["plans"] == []
        assert plan["skipped"].get("same_counterparty", 0) >= 1

    def test_stale_pairs_are_refused_with_a_counted_reason(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(
                source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                observed_at=AS_OF - timedelta(seconds=400),
            ),
        ]
        out = _plans([_offer()], quotes)
        plan = _the_plan(out)
        assert plan["plans"] == []
        assert plan["skipped"].get("observation_spread", 0) >= 1

    def test_a_started_game_is_never_planned(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       commence_time=AS_OF - timedelta(minutes=5)),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       commence_time=AS_OF - timedelta(minutes=5)),
        ]
        out = _plans([_offer()], quotes)
        assert _the_plan(out)["plans"] == []
        assert out["meta"]["unusable_groups"].get("already_started", 0) >= 1

    def test_an_implausible_margin_is_a_stale_quote_not_a_plan(self):
        # 1/20 + 1/1.5 = 0.717 — a 28% cash margin. The arb detector refuses
        # this as a palpable error; converting credit through it is the same
        # mistake with the same money.
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=20.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        out = _plans([_offer()], quotes)
        plan = _the_plan(out)
        assert plan["plans"] == []
        assert plan["skipped"].get("implausible_price", 0) >= 1

    def test_two_way_book_in_a_draw_window_is_ambiguous(self):
        # Soccer full game prices the draw. A book showing only home/away there
        # is ambiguous about ties — either it voids or the draw leg was dropped
        # — and the two readings differ by the whole stake.
        quotes = [
            make_quote(
                source="draftkings", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.AWAY, decimal_odds=3.0,
            ),
            make_quote(
                source="fanduel", sport=Sport.SOCCER, league="EPL",
                event_key="SOCCER-ars@SOCCER-che:2026-07-28",
                selection=Selection.HOME, decimal_odds=1.5,
            ),
        ]
        out = _plans([_offer()], quotes)
        plan = _the_plan(out)
        assert plan["plans"] == []
        assert plan["skipped"].get("ambiguous_tie_settlement", 0) >= 1

    def test_min_odds_excludes_short_promo_legs(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.4),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.8),
        ]
        out = _plans([_offer(min_odds="-200")], quotes)  # needs >= 1.5
        plan = _the_plan(out)
        assert plan["plans"] == []
        assert plan["skipped"].get("below_min_odds", 0) >= 1
        assert any("minimum odds" in c for c in plan["caveats"])

    def test_an_exchange_hedge_is_priced_at_its_net_odds(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="matchbook", selection=Selection.HOME, decimal_odds=2.0),
        ]
        # Real commission table: Matchbook keeps 2% of winnings → net 1.98.
        out = _plans([_offer()], quotes, commissions=None)
        best = _the_plan(out)["plans"][0]
        hedge = best["legs"][1]
        assert hedge["decimal_odds"] == pytest.approx(2.0)
        assert hedge["net_odds"] == pytest.approx(1.98)
        # h = 200/1.98 = 101.01; floor = min(200-101.01, 101.01*0.98) = 98.99
        assert hedge["stake"] == pytest.approx(101.01)
        assert best["guaranteed_cash"] == pytest.approx(98.99)


class TestOtherStrategies:
    def _conversion_slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    def test_no_sweat_uses_the_measured_refund_conversion(self):
        """Protect $100 at 3.0 with r = 66.66%: h = 100(3−0.6666)/1.5 = $155.56.

        Win: 200 − 155.56 = 44.44.  Lose: 155.56·0.5 − 100 + 66.66 = 44.44.
        """
        out = _plans([_offer(kind="no_sweat", reward_type="no_sweat")],
                     self._conversion_slate())
        plan = _the_plan(out)
        assert plan["strategy"] == "no_sweat_hedge"
        assert plan["refund_conversion_pct"] == pytest.approx(66.66)
        best = plan["plans"][0]
        assert best["legs"][0]["stake_kind"] == "cash"
        assert best["legs"][1]["stake"] == pytest.approx(155.56, abs=0.01)
        for _, profit in best["outcome_profits"]:
            assert profit == pytest.approx(44.44, abs=0.05)

    def test_no_sweat_without_a_measurable_conversion_stays_text(self):
        quotes = [make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0)]
        out = _plans([_offer(kind="no_sweat", reward_type="no_sweat")], quotes)
        plan = _the_plan(out)
        assert plan["strategy"] == "text_only"
        assert plan["plans"] == []

    def test_boost_with_a_stated_percent_locks_or_says_nothing(self):
        """50% boost on 2.0 → 2.5 net; hedge 2.2: T = 250, h = 113.64, +36.36."""
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.2),
        ]
        offer = _offer(kind="profit_boost", reward_type="boost", bonus_amount=None,
                       title="50% profit boost")
        out = _plans([offer], quotes)
        plan = _the_plan(out)
        assert plan["strategy"] == "boost_locked"
        best = plan["plans"][0]
        assert best["legs"][0]["net_odds"] == pytest.approx(2.5)
        assert best["legs"][1]["stake"] == pytest.approx(113.64, abs=0.01)
        assert best["guaranteed_cash"] == pytest.approx(36.36, abs=0.02)
        assert any("50% boost" in note for note in best["notes"])

    def test_boost_without_a_percent_reports_the_breakeven(self):
        """1.9 vs 1.9: H = 0.5263 → n_be = 2.1111 → breakeven boost 23.46%."""
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.9),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9),
        ]
        offer = _offer(kind="odds_boost", reward_type="boost", bonus_amount=None,
                       title="Odds boost token")
        out = _plans([offer], quotes)
        plan = _the_plan(out)
        assert plan["strategy"] == "boost_breakeven"
        best = plan["plans"][0]
        assert best["breakeven_boost_pct"] == pytest.approx(23.46, abs=0.02)

    def test_qualify_then_convert_prices_both_steps(self):
        offer = _offer(
            kind="signup_bonus",
            summary="Bet $5, get $150 in bonus bets",
            bonus_amount=150.0,
        )
        out = _plans([offer], self._conversion_slate())
        plan = _the_plan(out)
        assert plan["strategy"] == "qualify_then_convert"
        steps = [p.get("step") for p in plan["plans"]]
        assert "qualify" in steps and "convert" in steps
        qualify = next(p for p in plan["plans"] if p.get("step") == "qualify")
        convert = next(p for p in plan["plans"] if p.get("step") == "convert")
        # Qualifying $5 at 3.0: h = 15/1.5 = 10; both outcomes 15 − 15 = 0 —
        # this slate's qualifying round-trip happens to be free.
        assert qualify["qualifying_cost"] == pytest.approx(0.0)
        assert convert["legs"][0]["stake"] == pytest.approx(150.0)
        # EV = 150 × 0.6666 − 0
        assert plan["expected_value"] == pytest.approx(150 * 0.6666, abs=0.1)

    def test_rollover_grind_prices_the_vig(self):
        """1.9/1.9 round trip: stake 100, hedge 100, both outcomes −10."""
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.9),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9),
        ]
        offer = _offer(kind="deposit_match", reward_type="site_credit",
                       bonus_amount=200.0, wagering_requirement="10x")
        out = _plans([offer], quotes)
        plan = _the_plan(out)
        assert plan["strategy"] == "rollover_grind"
        best = plan["plans"][0]
        assert best["cost_per_100_wagered"] == pytest.approx(10.0)
        # 200 × 10x = $2,000 wagered at 10% cost = $200; EV = 200 − 200 = 0.
        assert plan["expected_value"] == pytest.approx(0.0)

    def test_parlay_boost_stays_text_only(self):
        out = _plans(
            [_offer(kind="parlay_boost", reward_type="boost")],
            self._conversion_slate(),
        )
        assert _the_plan(out)["strategy"] == "text_only"

    def test_unknown_kind_string_degrades_to_text(self):
        out = _plans([_offer(kind="mystery_meat")], self._conversion_slate())
        assert _the_plan(out)["strategy"] in {"text_only", "bonus_conversion"}


class TestPayloadDiscipline:
    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
            make_quote(source="pinnacle", selection=Selection.HOME, decimal_odds=1.52),
        ]

    def test_payload_is_json_serializable(self):
        out = _plans([_offer()], self._slate())
        json.dumps(out)

    def test_quote_order_does_not_change_the_answer(self):
        quotes = self._slate()
        baseline = _plans([_offer()], quotes)
        for seed in (1, 7, 42):
            shuffled = quotes[:]
            random.Random(seed).shuffle(shuffled)
            assert _plans([_offer()], shuffled) == baseline

    def test_promo_offer_models_are_accepted(self):
        offer = PromoOffer(
            source="draftkings", offer_id="offer-1", kind=PromoKind.BONUS_BET,
            title="Bonus bet drop", reward_type="bonus_bets", bonus_amount=100.0,
            observed_at=AS_OF,
        )
        out = _plans([offer], self._slate())
        assert _the_plan(out)["strategy"] == "bonus_conversion"

    def test_at_most_max_plans_per_offer(self):
        quotes = []
        for day in range(1, 7):
            quotes.append(make_quote(
                source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                event_key=f"MLB-PHI@MLB-MIA:2026-08-{day:02d}",
                source_event_id=f"evt-{day}",
                commence_time=AS_OF + timedelta(days=day),
            ))
            quotes.append(make_quote(
                source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                event_key=f"MLB-PHI@MLB-MIA:2026-08-{day:02d}",
                source_event_id=f"evt-{day}",
                commence_time=AS_OF + timedelta(days=day),
            ))
        out = _plans([_offer()], quotes)
        assert len(_the_plan(out)["plans"]) == MAX_PLANS_PER_OFFER

    def test_quote_age_is_measured_from_the_oldest_leg(self):
        """The oldest leg is the *hedge* here, on purpose.

        With the promo leg oldest, reading the promo leg's own timestamp gives
        the same answer as taking the minimum, so the rule under test was
        indistinguishable from a much weaker one.
        """
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       observed_at=AS_OF - timedelta(seconds=30)),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       observed_at=AS_OF - timedelta(seconds=120)),
        ]
        out = _plans([_offer()], quotes)
        assert _the_plan(out)["plans"][0]["quote_age_seconds"] == 120

    def test_suspended_promo_rows_produce_no_plan(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       status=QuoteStatus.SUSPENDED),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        out = _plans([_offer()], quotes)
        assert _the_plan(out)["plans"] == []


class TestAgreementWithTheArbDetector:
    """A cash-mode plan on an actual arb prices the same edge the detector prices.

    The conventions differ — the planner fixes the promo stake and equalises,
    the detector splits a fixed bankroll and floor-maximises in whole units —
    so per-dollar return is the number they must agree on.
    """

    def test_cash_mode_matches_the_detected_roi_per_dollar(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.2),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9),
        ]
        report = find_opportunities(
            quotes, commissions={}, one_counterparty={}, as_of=AS_OF
        )
        assert report.opportunities, [d.code for d in report.diagnostics]
        opportunity = report.opportunities[0]

        offer = _offer(kind="deposit_match", reward_type="site_credit",
                       bonus_amount=None, wagering_requirement=None)
        out = _plans([offer], quotes)
        best = _the_plan(out)["plans"][0]
        total_outlay = sum(leg["stake"] for leg in best["legs"])
        planner_roi = best["settled_cash"] / total_outlay
        assert planner_roi == pytest.approx(opportunity.roi, abs=0.002)


# ── round 1 adversarial findings ─────────────────────────────────────────────


class TestBothFeedsOfOneFailoverPairCannotBeStitched:
    """A position may not quote both a book's first-party and AN feed.

    The two are one book seen twice: they can only differ by one being stale,
    so no operator can place both prices.  Before the fix the gate was
    promo-vs-hedge only, so on a three-way market the *two hedges* could be
    ``betmgm`` and ``an_betmgm`` — a synthetic book.  Cost, measured: a printed
    floor of $109.52 against an executable $98.41, settling $40.00 under the
    printed guarantee when the stale feed was the generous one.
    """

    EVENT = "SOCCER-ars@SOCCER-che:2026-07-28"

    def _slate(self):
        def q(source, selection, odds):
            return make_quote(
                source=source, selection=selection, decimal_odds=odds,
                sport=Sport.SOCCER, league="EPL", event_key=self.EVENT,
                home_participant="SOCCER-che", away_participant="SOCCER-ars",
                home_team="Chelsea", away_team="Arsenal",
            )
        # All three books price all three sides: a two-way book in a
        # draw-priced window is refused earlier, for a different reason, and
        # would hide the gate this class is about.
        return [
            q("fanduel", Selection.DRAW, 4.4),
            q("fanduel", Selection.HOME, 1.90),
            q("fanduel", Selection.AWAY, 3.7),
            # One book, two feeds, disagreeing — exactly one of these is live.
            q("betmgm", Selection.HOME, 1.92),
            q("betmgm", Selection.AWAY, 3.55),
            q("betmgm", Selection.DRAW, 4.2),
            q("an_betmgm", Selection.HOME, 1.88),
            q("an_betmgm", Selection.AWAY, 3.75),
            q("an_betmgm", Selection.DRAW, 4.1),
        ]

    def test_the_two_hedge_legs_never_come_from_both_feeds(self):
        offer = _offer(source="fanduel", offer_id="fd-1")
        out = build_promo_plans(
            [offer], self._slate(), as_of=AS_OF, commissions={}, one_counterparty={},
        )
        plan = out["plans"]["fanduel|fd-1"]
        assert plan["plans"], plan
        for concrete in plan["plans"]:
            hedges = sorted(
                leg["source"] for leg in concrete["legs"] if leg["role"] == "hedge"
            )
            assert hedges != ["an_betmgm", "betmgm"], (
                "stitched both feeds of one book into one position"
            )

    def test_the_refusal_is_counted(self):
        offer = _offer(source="fanduel", offer_id="fd-1")
        out = build_promo_plans(
            [offer], self._slate(), as_of=AS_OF, commissions={}, one_counterparty={},
        )
        assert out["plans"]["fanduel|fd-1"]["skipped"].get("both_failover_feeds", 0) > 0

    def test_the_surviving_plan_uses_one_feed_and_is_executable(self):
        offer = _offer(source="fanduel", offer_id="fd-1")
        out = build_promo_plans(
            [offer], self._slate(), as_of=AS_OF, commissions={}, one_counterparty={},
        )
        best = out["plans"]["fanduel|fd-1"]["plans"][0]
        hedges = {leg["source"] for leg in best["legs"] if leg["role"] == "hedge"}
        assert len(hedges) == 1, hedges
        # an_betmgm's pair (1.88 / 3.75) is the better single feed: $100 credit
        # on the 4.4 draw returns $340 profit, hedged 340/3.75 = $90.67 on away
        # and 340/1.88 = $180.85 on home, so every settled outcome pays
        # 340 − 271.52 = $68.48.
        assert best["guaranteed_cash"] == pytest.approx(68.48, abs=0.01)


class TestTiedPricesPickTheSameRowWhateverTheOrder:
    """A main line and an alternate at the same price must not race.

    Books routinely quote both, and the tie was resolved by whichever row the
    list held first.  That decided the ``observed_at`` printed beside the plan
    and — because the chosen row's freshness feeds ``MAX_OBSERVATION_SPREAD`` —
    whether the plan existed at all.
    """

    def _rows(self, hedge_age_seconds: int):
        stale = AS_OF - timedelta(seconds=400)
        fresh = AS_OF - timedelta(seconds=5)
        return [
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=3.0, observed_at=stale, source_market_id="main"),
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=3.0, observed_at=fresh, is_alternate=True,
                       source_market_id="alt"),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       observed_at=AS_OF - timedelta(seconds=hedge_age_seconds)),
        ]

    def test_the_payload_is_identical_in_both_orders(self):
        rows = self._rows(5)
        forward = _plans([_offer()], rows)
        backward = _plans([_offer()], [rows[1], rows[0], rows[2]])
        assert json.dumps(forward, sort_keys=True) == json.dumps(backward, sort_keys=True)

    def test_row_order_cannot_decide_whether_a_plan_exists(self):
        # The hedge is 180s old: pairing it with the 400s-old main row spans
        # 220s and breaks the 180s limit, while the 5s-old alternate spans 175s
        # and clears it.  Before the fix the tie winner — and so the plan's
        # existence — was decided by which row the list held first.
        rows = self._rows(180)
        forward = _the_plan(_plans([_offer()], rows))
        backward = _the_plan(_plans([_offer()], [rows[1], rows[0], rows[2]]))
        assert len(forward["plans"]) == len(backward["plans"]) == 1
        assert forward["skipped"] == backward["skipped"] == {}

    def test_the_fresher_row_wins_a_tied_price(self):
        plan = _the_plan(_plans([_offer()], self._rows(5)))["plans"][0]
        promo = next(leg for leg in plan["legs"] if leg["role"] == "promo")
        assert promo["is_alternate"] is True
        assert plan["quote_age_seconds"] == 5


class TestEveryOfferForOneBookReportsTheSameGateCounts:
    """The scan cache must carry its counts, not just its candidates.

    Books publish many offers; the second one onward hit the cache and got an
    empty ``skipped`` map printed next to a caveat saying those counts explain
    the refusal.  Which offer held the real counts depended on list order.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            # Only "hedge" available is the promo book's own failover feed.
            make_quote(source="an_draftkings", selection=Selection.HOME, decimal_odds=1.5),
        ]

    def test_both_offers_carry_the_counts(self):
        offers = [_offer(offer_id="a"), _offer(offer_id="b")]
        out = _plans(offers, self._slate())
        first = out["plans"]["draftkings|a"]["skipped"]
        second = out["plans"]["draftkings|b"]["skipped"]
        assert first == second
        assert first.get("no_hedge_price", 0) > 0, first

    def test_offer_order_does_not_move_the_counts(self):
        slate = self._slate()
        forward = _plans([_offer(offer_id="a"), _offer(offer_id="b")], slate)
        backward = _plans([_offer(offer_id="b"), _offer(offer_id="a")], slate)
        for key in ("draftkings|a", "draftkings|b"):
            assert forward["plans"][key] == backward["plans"][key]


class TestNoSweatNeverPrintsAPlanThatLosesInEveryOutcome:
    """A free-insurance offer that cannot beat doing nothing has no plan.

    Every other mode filtered to a positive floor; this one sliced the list
    unconditionally, so ordinary wide vig produced a top plan locking −$1.56 in
    every outcome, formatted exactly like a profitable one, with no caveat.
    """

    def _quotes(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.6),
        ]

    def test_a_negative_floor_is_not_printed(self):
        """On a slate where the hedge *is* profitable, every printed floor is
        positive — and the loop that checks it actually runs.

        The original ran over an empty list on green: replacing its body with a
        raise left the test passing, so it asserted nothing at all.
        """
        good = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        offer = _offer(kind="no_sweat", reward_type="no_sweat", bonus_amount=None)
        plan = _the_plan(_plans([offer], good))
        assert plan["plans"], "the fixture must produce a plan or this checks nothing"
        for concrete in plan["plans"]:
            assert concrete["guaranteed_cash"] > 0, concrete

    def test_the_refusal_is_explained(self):
        offer = _offer(kind="no_sweat", reward_type="no_sweat", bonus_amount=None)
        plan = _the_plan(_plans([offer], self._quotes()))
        assert plan["plans"] == []
        assert any("locked profit" in caveat for caveat in plan["caveats"]), plan["caveats"]

    def test_the_refund_conversion_is_still_reported(self):
        """The rate is what the credit is worth; it survives having no plan."""
        offer = _offer(kind="no_sweat", reward_type="no_sweat", bonus_amount=None)
        plan = _the_plan(_plans([offer], self._quotes()))
        assert plan["refund_conversion_pct"] == pytest.approx(37.5, abs=0.01)


class TestAParseFaultAtThePromoBookIsCountedNotSwallowed:
    """Two prices for one selection at one book excludes it — and says so.

    The faulted source is popped from the group, so it never entered the
    by-source index and its markets vanished with no count anywhere.  A book
    whose every market faulted was reported as having produced no rows at all,
    pointing the operator at the collector instead of at the parse.
    """

    def _faulted_pair(self, event_key: str):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=3.0, event_key=event_key, source_market_id="m1"),
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=2.4, event_key=event_key, source_market_id="m2"),
            make_quote(source="fanduel", selection=Selection.HOME,
                       decimal_odds=1.5, event_key=event_key),
        ]

    def test_a_wholly_faulted_book_is_not_called_quiet(self):
        plan = _the_plan(_plans([_offer()], self._faulted_pair("MLB-PHI@MLB-MIA:2026-07-28")))
        assert plan["skipped"].get("duplicate_selection") == 1
        assert not any("no rows from this book" in c for c in plan["caveats"]), plan["caveats"]
        assert any("two prices for the same selection" in c for c in plan["caveats"])

    def test_a_faulted_market_is_counted_beside_a_clean_one(self):
        clean_key = "MLB-NYY@MLB-BOS:2026-07-29"
        clean = [
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=3.0, event_key=clean_key,
                       home_participant="MLB-BOS", away_participant="MLB-NYY"),
            make_quote(source="fanduel", selection=Selection.HOME,
                       decimal_odds=1.5, event_key=clean_key,
                       home_participant="MLB-BOS", away_participant="MLB-NYY"),
        ]
        quotes = self._faulted_pair("MLB-PHI@MLB-MIA:2026-07-28") + clean
        plan = _the_plan(_plans([_offer()], quotes))
        assert plan["skipped"].get("duplicate_selection") == 1
        assert [p["event_key"] for p in plan["plans"]] == [clean_key]


class TestTheHedgePoolIsGatedBeforeItIsTruncated:
    """Ineligible sources must not fill the best-N window.

    Four measured mirrors of the promo book, all quoting better than the one
    real hedge on the board, occupied the four-deep pool and pushed a genuine
    $66.66-floor plan out — the offer then reported zero plans and blamed
    ``same_counterparty`` for every one of them.
    """

    def _slate(self):
        rows = [make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0)]
        for index, mirror in enumerate(("dk_a", "dk_b", "dk_c", "dk_d")):
            rows.append(
                make_quote(source=mirror, selection=Selection.HOME,
                           decimal_odds=1.80 + index * 0.01)
            )
        rows.append(make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5))
        return rows

    def _mirrors(self):
        from src.arb import EVERY_LEAGUE
        return {EVERY_LEAGUE: [frozenset({"draftkings", "dk_a", "dk_b", "dk_c", "dk_d"})]}

    def test_the_real_hedge_survives_four_better_ineligible_ones(self):
        out = build_promo_plans(
            [_offer()], self._slate(), as_of=AS_OF, commissions={},
            one_counterparty=self._mirrors(),
        )
        plan = _the_plan(out)
        assert plan["plans"], plan["skipped"]
        best = plan["plans"][0]
        hedges = {leg["source"] for leg in best["legs"] if leg["role"] == "hedge"}
        assert hedges == {"fanduel"}
        # $100 credit at 3.00 returns $200 profit; hedged 200/1.5 = $133.33.
        assert best["guaranteed_cash"] == pytest.approx(66.66, abs=0.02)

    def test_the_excluded_mirrors_are_still_counted(self):
        out = build_promo_plans(
            [_offer()], self._slate(), as_of=AS_OF, commissions={},
            one_counterparty=self._mirrors(),
        )
        assert _the_plan(out)["skipped"].get("same_counterparty") == 4


class TestQualifyingStakeReadsTheVenuesOwnWording:
    """``enrich`` keeps a specific venue summary, so its wording reaches here.

    "Bet $5 or more, get $150 in bonus bets" is the commonest welcome phrasing
    in the wild, and a pattern demanding the comma immediately after the amount
    silently dropped the qualifying leg, its cost, and the expected-value line.
    """

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            ("Bet $5, get $150 in bonus bets", 5.0),
            ("Bet $5 or more, get $150 in bonus bets", 5.0),
            ("Bet $10 and get $200 in bonus bets", 10.0),
            ("Bet $5 to get $150", 5.0),
            ("bet $1,000, get $100 in bonus bets", 1000.0),
            ("Bet $5 or more, receive up to $150", 5.0),
            # Not a bet-and-get: no qualifying stake to name.
            ("Get $150 in bonus bets", None),
            ("100% deposit match up to $1000", None),
            ("Bet $5", None),
            # Too much between the two amounts to be sure they are a pair.
            ("Bet $5 on any market in the app this weekend only, get $150", None),
        ],
    )
    def test_the_qualifying_stake_is_read(self, summary, expected):
        assert parse_qualifying_stake(summary) == expected

    def test_the_venue_wording_still_produces_a_two_step_plan(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        offer = _offer(summary="Bet $5 or more, get $150 in bonus bets", bonus_amount=150.0)
        plan = _the_plan(_plans([offer], quotes))
        assert plan["strategy"] == "qualify_then_convert"
        assert [p.get("step") for p in plan["plans"]] == ["qualify", "convert"]
        assert plan["expected_value"] is not None


class TestAnEmptySkippedMapIsNeverCitedAsAnExplanation:
    """The generic refusal caveat only appears when a gate really refused.

    Pointing at "the counts in 'skipped'" when that map is empty is the same
    defect as the cache dropping the counts: a sentence about evidence that is
    not there.
    """

    def test_no_sweat_with_nothing_gated_does_not_cite_the_counts(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.6),
        ]
        offer = _offer(kind="no_sweat", reward_type="no_sweat", bonus_amount=None)
        plan = _the_plan(_plans([offer], quotes))
        assert plan["plans"] == []
        assert plan["skipped"] == {}
        assert not any("counts in 'skipped'" in c for c in plan["caveats"]), plan["caveats"]


# ── round 3 adversarial findings ─────────────────────────────────────────────


class TestGateCountsAreNotDoubledByATwoScanStrategy:
    """Counting the same slate twice is not two refusals.

    ``no_sweat`` and ``qualify_then_convert`` run two scans; the shared
    ``Counter.update`` **added** them, so a book showing ``regime_mismatch ×90``
    under a one-scan strategy showed ``×180`` under a two-scan one on the same
    run — beside a caveat promising the counts say what was refused.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            # The only counterparty available is the promo book's own feed, so
            # every market refuses for the same reason in every scan.
            make_quote(source="an_draftkings", selection=Selection.HOME, decimal_odds=1.5),
        ]

    def _counts(self, **overrides):
        offer = _offer(**overrides)
        return _the_plan(_plans([offer], self._slate()))["skipped"]

    def test_one_scan_and_two_scan_strategies_agree(self):
        one = self._counts()
        two_no_sweat = self._counts(kind="no_sweat", reward_type="no_sweat")
        two_qualify = self._counts(summary="Bet $5, get $150 in bonus bets")
        assert one.get("no_hedge_price"), one
        assert two_no_sweat == one, (two_no_sweat, one)
        assert two_qualify == one, (two_qualify, one)

    def test_a_faulted_market_is_counted_once_per_scan_not_per_pass(self):
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=3.0, source_market_id="m1"),
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=2.4, source_market_id="m2"),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        one = _the_plan(_plans([_offer()], quotes))["skipped"]
        two = _the_plan(_plans(
            [_offer(summary="Bet $5, get $150 in bonus bets")], quotes
        ))["skipped"]
        assert one.get("duplicate_selection") == 1
        assert two.get("duplicate_selection") == 1


class TestAStaleRunIsNotReportedAsAMissingBook:
    """"Every game has started" and "this book was not collected" differ.

    Both produced the identical sentence "the latest odds collection has no rows
    from this book", which sends the operator to the collector for a run that is
    merely old.  On live data four hours past its scrape that was 16 of 60
    offers, while the panel header still read "plans priced from odds run #10".
    """

    def _started_slate(self):
        started = AS_OF - timedelta(hours=1)
        return [
            make_quote(source="draftkings", selection=Selection.AWAY,
                       decimal_odds=3.0, commence_time=started),
            make_quote(source="fanduel", selection=Selection.HOME,
                       decimal_odds=1.5, commence_time=started),
        ]

    def test_a_started_slate_says_so_rather_than_claiming_no_rows(self):
        plan = _the_plan(_plans([_offer()], self._started_slate()))
        assert plan["strategy"] == "no_odds_coverage"
        assert not any("no rows from this book" in c for c in plan["caveats"]), plan["caveats"]
        # The specific remedy, not merely the reason's name: the generic
        # "set aside before pricing" fallback also contains "already started",
        # so asserting on that alone passed with this branch disabled.
        assert any("too old to plan from" in c for c in plan["caveats"]), plan["caveats"]
        assert any("collect again" in c for c in plan["caveats"]), plan["caveats"]
        assert plan["skipped"].get("already_started") == 1

    def test_a_genuinely_absent_book_still_says_no_rows(self):
        """The other half of the distinction, so the fix cannot collapse both."""
        quotes = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
            make_quote(source="pinnacle", selection=Selection.AWAY, decimal_odds=3.0),
        ]
        plan = _the_plan(_plans([_offer()], quotes))
        assert plan["strategy"] == "no_odds_coverage"
        assert any("no rows from this book" in c for c in plan["caveats"]), plan["caveats"]
        assert plan["skipped"] == {}

    def test_a_partly_stale_book_carries_the_count(self):
        started = AS_OF - timedelta(hours=1)
        live_key = "MLB-NYY@MLB-BOS:2026-07-29"
        quotes = self._started_slate() + [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       event_key=live_key, home_participant="MLB-BOS",
                       away_participant="MLB-NYY"),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       event_key=live_key, home_participant="MLB-BOS",
                       away_participant="MLB-NYY"),
        ]
        plan = _the_plan(_plans([_offer()], quotes))
        assert plan["plans"], plan["skipped"]
        assert plan["skipped"].get("already_started") == 1


class TestAFaultedMarketIsCountedOncePerMarketNotPerFeed:
    """A market faulted at both a brand's feeds is one market.

    The no-coverage caveat summed the per-source lists while ``_scan`` deduped
    them, so the two disagreed by construction.
    """

    def test_both_feeds_faulting_one_market_counts_one(self):
        quotes = []
        for source in ("draftkings", "an_draftkings"):
            quotes += [
                make_quote(source=source, selection=Selection.AWAY,
                           decimal_odds=3.0, source_market_id=f"{source}-1"),
                make_quote(source=source, selection=Selection.AWAY,
                           decimal_odds=2.4, source_market_id=f"{source}-2"),
            ]
        plan = _the_plan(_plans([_offer()], quotes))
        assert plan["skipped"].get("duplicate_selection") == 1, plan["skipped"]
        assert "1 market(s)" in " ".join(plan["caveats"]), plan["caveats"]


class TestTheStatedMinimumAppliesToEveryPromoLegItClaims:
    """A caveat saying the minimum was applied must be true of every card.

    ``qualify_then_convert`` passed ``None`` to its conversion scan while
    printing "stated minimum odds X applied to the promo-side leg", so convert
    cards could sit below the stated floor.
    """

    def test_no_convert_card_sits_below_the_stated_minimum(self):
        live_key = "MLB-NYY@MLB-BOS:2026-07-29"
        quotes = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
            # A short-priced market the conversion scan would otherwise take.
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.4,
                       event_key=live_key, home_participant="MLB-BOS",
                       away_participant="MLB-NYY"),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=3.6,
                       event_key=live_key, home_participant="MLB-BOS",
                       away_participant="MLB-NYY"),
        ]
        offer = _offer(summary="Bet $5, get $150 in bonus bets",
                       bonus_amount=150.0, min_odds="-200")  # 1.50
        plan = _the_plan(_plans([offer], quotes))
        assert plan["strategy"] == "qualify_then_convert"
        assert any("minimum odds" in c for c in plan["caveats"])
        for concrete in plan["plans"]:
            promo = next(leg for leg in concrete["legs"] if leg["role"] == "promo")
            assert promo["decimal_odds"] >= 1.5 - 1e-9, (concrete["step"], promo)


class TestTheQualifyingPatternDoesNotSwallowRefundCopy:
    """A conditional refund is not a guaranteed bet-and-get.

    Widening the gap between the two amounts to 24 arbitrary characters let
    "Bet $50 first bet, if it loses get $50 back" parse as a bet-and-get, whose
    ``expected_value`` prices the refund as credit that always arrives.
    """

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            ("Bet $5, get $150 in bonus bets", 5.0),
            ("Bet $5 or more, get $150 in bonus bets", 5.0),
            ("Bet $10 and get $200 in bonus bets", 10.0),
            ("Bet $5 to get $150", 5.0),
            ("Bet $5 or more, receive up to $150", 5.0),
            # Conditional refunds: not a bet-and-get.
            ("Bet $50 first bet, if it loses get $50 back", None),
            ("Bet $25 and if your bet loses get $25 in bonus bets", None),
            # A recurring weekly offer is not a one-off welcome either.
            ("Bet $20 weekly and earn $5 in bonus bets every Monday", None),
        ],
    )
    def test_only_real_bet_and_get_copy_parses(self, summary, expected):
        assert parse_qualifying_stake(summary) == expected


# ── round 3 review B: the paths nothing exercised ────────────────────────────


class TestViewOnlySourcesAreNeverAHedge:
    """``an_open`` is a consensus column, not a book you can bet at.

    ``stakeable_odds_sources`` keeps it off the *promo* side, and a test covered
    that; nothing covered the *hedge* side.  Dropping the filter let the panel
    print a $94.73 guarantee resting on a leg no operator can place.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
            # Better than the real hedge, and unplaceable.
            make_quote(source="an_open", selection=Selection.HOME, decimal_odds=1.9),
        ]

    def test_the_consensus_column_is_not_used_as_a_hedge(self):
        best = _the_plan(_plans([_offer()], self._slate()))["plans"][0]
        assert all(leg["source"] != "an_open" for leg in best["legs"]), best["legs"]

    def test_the_real_hedge_and_its_floor_are_what_is_printed(self):
        best = _the_plan(_plans([_offer()], self._slate()))["plans"][0]
        assert best["legs"][1]["source"] == "fanduel"
        assert best["guaranteed_cash"] == pytest.approx(66.66, abs=0.02)


class TestSpreadPlansNameTheSideEachBookActuallyShows:
    """No planner test used a spread or a total, so the whole line pathway —
    per-leg signs, quarter-line settlement, granularity refusal — was free."""

    def _spread(self, line_home=-1.5):
        return [
            make_quote(source="draftkings", market=Market.SPREAD,
                       selection=Selection.AWAY, line=-line_home, decimal_odds=3.0),
            make_quote(source="fanduel", market=Market.SPREAD,
                       selection=Selection.HOME, line=line_home, decimal_odds=1.5),
        ]

    def test_each_leg_carries_its_own_signed_line(self):
        best = _the_plan(_plans([_offer()], self._spread()))["plans"][0]
        promo = next(leg for leg in best["legs"] if leg["role"] == "promo")
        hedge = next(leg for leg in best["legs"] if leg["role"] == "hedge")
        # The two sides of one spread carry opposite signs; printing the group's
        # canonical line on both would send the hedge to the same side.
        assert promo["line"] == pytest.approx(1.5)
        assert hedge["line"] == pytest.approx(-1.5)
        assert promo["selection"] == "away"
        assert hedge["selection"] == "home"

    def test_a_whole_number_spread_enumerates_its_push(self):
        labels = [
            label for label, _ in
            _the_plan(_plans([_offer()], self._spread(line_home=-1.0)))["plans"][0]["outcome_profits"]
        ]
        assert "push" in labels, labels

    def test_an_unsupported_granularity_is_refused_and_counted(self):
        plan = _the_plan(_plans([_offer()], self._spread(line_home=-1.3)))
        assert plan["plans"] == []
        assert plan["skipped"].get("unsupported_line_granularity") == 1


class TestQuarterLineCreditPaysHalfOnAHalfWin:
    """A bonus bet that half-wins converts half as much credit.

    ``_bonus_cash``'s HALF_WIN branch never executed under any test, so paying
    the full ``net - 1`` there — doubling the printed payout of a credit on a
    quarter line — was invisible.
    """

    def _quarter(self):
        return [
            make_quote(source="draftkings", market=Market.SPREAD,
                       selection=Selection.AWAY, line=1.25, decimal_odds=3.0),
            make_quote(source="fanduel", market=Market.SPREAD,
                       selection=Selection.HOME, line=-1.25, decimal_odds=1.5),
        ]

    def test_the_half_push_outcome_is_enumerated_and_costs_half(self):
        best = _the_plan(_plans([_offer()], self._quarter()))["plans"][0]
        outcomes = dict(best["outcome_profits"])
        half = [label for label in outcomes if label.startswith("half_push")]
        assert half, outcomes
        # $100 credit at 3.0 → $200 profit on a win, $133.33 hedge at 1.5.
        # On the half-push the credit half-wins: (3.0-1)/2 * 100 = $100 back,
        # and the hedge's surviving half refunds — floor is materially below
        # the clean two-way figure, not equal to it.
        assert outcomes[half[0]] < 66.66
        assert best["guaranteed_cash"] == pytest.approx(min(outcomes.values()))


class TestRankingUsesTheSettledFloorAcrossSeveralMarkets:
    """The claim is about *ordering*, so the fixture needs more than one market.

    A one-candidate fixture pins the reported values and leaves the ordering —
    the thing the docstring says matters — unobservable.
    """

    def _slate(self):
        # A pushable NFL moneyline (all-outcomes floor 0, settled floor real)
        # against a clean baseball moneyline with a smaller settled floor.
        push_key = "NFL-NYJ@NFL-NE:2026-07-29"
        rows = [
            make_quote(source="draftkings", sport=Sport.FOOTBALL, league="NFL",
                       event_key=push_key, home_participant="NFL-NE",
                       away_participant="NFL-NYJ", home_team="New England",
                       away_team="New York Jets",
                       selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", sport=Sport.FOOTBALL, league="NFL",
                       event_key=push_key, home_participant="NFL-NE",
                       away_participant="NFL-NYJ", home_team="New England",
                       away_team="New York Jets",
                       selection=Selection.HOME, decimal_odds=1.5),
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.2),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.75),
        ]
        return rows

    def test_the_pushable_market_is_not_buried_by_its_zero_floor(self):
        plans = _the_plan(_plans([_offer()], self._slate()))["plans"]
        assert len(plans) >= 2, plans
        first = plans[0]
        # Ranked on the settled floor, the NFL market leads on conversion even
        # though its all-outcomes floor is zero.
        assert first["event_key"].startswith("NFL-"), [p["event_key"] for p in plans]
        assert first["guaranteed_cash"] == pytest.approx(0.0, abs=0.01)
        assert first["settled_cash"] > plans[1]["settled_cash"]


class TestTiedHedgeBooksDoNotRaceEachOther:
    """Two books at the identical hedge price must resolve the same way always.

    The within-one-book tie was pinned; the across-books tie was not, and
    dropping the source-name tiebreaker produced six different payloads over six
    row orderings.
    """

    def _slate(self):
        rows = [make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0)]
        for source in ("fanduel", "pinnacle", "bovada", "caesars"):
            rows.append(make_quote(source=source, selection=Selection.HOME, decimal_odds=1.5))
        return rows

    def test_every_ordering_produces_one_answer(self):
        slate = self._slate()
        seen = set()
        for seed in range(6):
            shuffled = list(slate)
            random.Random(seed).shuffle(shuffled)
            seen.add(json.dumps(_plans([_offer()], shuffled), sort_keys=True))
        assert len(seen) == 1, f"{len(seen)} distinct payloads over 6 orderings"


class TestTheMainLineWinsATiedAlternate:
    """``_row_rank``'s documented order, each key pinned in its own direction.

    The original fixture's fresh row was *also* the alternate, so preferring the
    alternate ahead of freshness was indistinguishable from preferring the
    fresher row.
    """

    def test_at_equal_freshness_the_main_line_wins(self):
        observed = AS_OF - timedelta(seconds=5)
        rows = [
            # The alternate sorts *after* the main row on market id, so the
            # market-id key alone would pick it — only the main-line key can
            # produce the right answer here.  Named the other way round, the
            # last-resort key silently stood in for the rule under test.
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       observed_at=observed, is_alternate=True, source_market_id="zzz"),
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       observed_at=observed, source_market_id="aaa"),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       observed_at=observed),
        ]
        best = _the_plan(_plans([_offer()], rows))["plans"][0]
        promo = next(leg for leg in best["legs"] if leg["role"] == "promo")
        assert promo["is_alternate"] is False, "the main line must win a dead tie"

    def test_a_tied_pair_resolves_the_same_way_in_either_order(self):
        """Price, freshness and alternate status are a total order here.

        A fourth (market-id) key used to sit below them looking load-bearing;
        it was unreachable, because two rows sharing (source, selection,
        alternate status) are caught as duplicates and never compared.
        """
        observed = AS_OF - timedelta(seconds=5)
        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       observed_at=observed, source_market_id="bbb"),
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       observed_at=observed, is_alternate=True, source_market_id="aaa"),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       observed_at=observed),
        ]
        forward = _plans([_offer()], rows)
        backward = _plans([_offer()], list(reversed(rows)))
        assert json.dumps(forward, sort_keys=True) == json.dumps(backward, sort_keys=True)
        promo = next(
            leg for leg in _the_plan(forward)["plans"][0]["legs"]
            if leg["role"] == "promo"
        )
        assert promo["is_alternate"] is False


class TestTheStatedMinimumIsReadAgainstThePriceTheBookShows:
    """A book's "min odds -200" is about its own displayed price, not the net.

    On an exchange the two differ by the commission, so reading the net would
    silently drop legs that meet the published rule.
    """

    def test_an_exchange_leg_at_the_stated_minimum_survives_its_commission(self):
        rows = [
            # Smarkets charges commission, so net < quoted.
            make_quote(source="smarkets", selection=Selection.AWAY, decimal_odds=2.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.1),
        ]
        offer = _offer(source="smarkets", offer_id="ex-1", min_odds="2.0")
        out = build_promo_plans([offer], rows, as_of=AS_OF, one_counterparty={})
        plan = out["plans"]["smarkets|ex-1"]
        assert plan["skipped"].get("below_min_odds", 0) == 0, plan["skipped"]
        assert plan["plans"], plan


# ── round 4 adversarial findings ─────────────────────────────────────────────


class TestTwoLegsNeverBackTheSameTeam:
    """The detector's fixture-outlier gate, which this module claimed parity with.

    With one book's home and away inverted, the "hedge" backs the same team as
    the promo leg — measured at a $110 swing per $100 of credit against a
    printed floor of +$57.62.  Reachable through the real reconciler whenever a
    league is unknown to it.
    """

    def _inverted(self):
        key = "MLB-PHI@MLB-MIA:2026-07-28"
        return [
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=2.1,
                       event_key=key),
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.9,
                       event_key=key),
            # bovada has the two participants the other way round.
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=2.1,
                       event_key=key, home_participant="MLB-PHI",
                       away_participant="MLB-MIA", home_team="Philadelphia Phillies",
                       away_team="Miami Marlins"),
            make_quote(source="bovada", selection=Selection.HOME, decimal_odds=1.9,
                       event_key=key, home_participant="MLB-PHI",
                       away_participant="MLB-MIA", home_team="Philadelphia Phillies",
                       away_team="Miami Marlins"),
        ]

    def test_the_outlier_book_is_not_used_as_a_hedge(self):
        plan = _the_plan(_plans([_offer()], self._inverted()))
        for concrete in plan["plans"]:
            assert all(leg["source"] != "bovada" for leg in concrete["legs"]), concrete

    def test_the_exclusion_is_counted(self):
        plan = _the_plan(_plans([_offer()], self._inverted()))
        assert plan["skipped"].get("legs_disagree_on_the_game", 0) >= 1, plan["skipped"]


class TestABookPricingItselfToLoseIsExcluded:
    """A book whose own complete market sums below 1.0 was mispaired by the parser.

    Left in, it inflated a printed conversion from ~44% to 58%.  The detector
    deletes such a book and carries on; so does this.
    """

    def _crossed(self):
        return [
            # draftkings' own two-way sums to 0.909 — impossible for a real book.
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=2.2),
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.2),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=1.95),
            make_quote(source="bovada", selection=Selection.HOME, decimal_odds=1.95),
        ]

    def test_the_mispaired_book_produces_no_plan(self):
        plan = _the_plan(_plans([_offer()], self._crossed()))
        assert plan["plans"] == [], plan["plans"]
        assert plan["skipped"].get("source_prices_itself_to_lose", 0) >= 1, plan["skipped"]


class TestAHedgeAboveTheBooksStatedSizeIsRefused:
    """A leg the venue will not accept is not a hedge, and its floor is not a floor.

    Measured on live data: a $481.44 "worst case" resting on a $1018.56 leg at a
    book advertising $40, with an empty caveat list.
    """

    def _slate(self, limit):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="matchbook", selection=Selection.HOME, decimal_odds=1.5,
                       limit_amount=limit),
        ]

    def test_a_hedge_over_the_published_size_is_not_printed(self):
        plan = _the_plan(_plans([_offer()], self._slate(limit=40.0)))
        assert plan["plans"] == [], plan["plans"]
        # Counted, not silently dropped: the summary caveat is guarded on this
        # map being non-empty, so an uncounted refusal left the offer with no
        # plan, no count and no sentence at all.
        assert plan["skipped"].get("hedge_over_stated_limit", 0) >= 1, plan["skipped"]

    def test_the_same_market_plans_when_the_size_is_there(self):
        plan = _the_plan(_plans([_offer()], self._slate(limit=500.0)))
        assert plan["plans"], plan["skipped"]
        assert plan["plans"][0]["legs"][1]["stake"] == pytest.approx(133.33)

    def test_an_unstated_limit_is_not_treated_as_zero(self):
        plan = _the_plan(_plans([_offer()], self._slate(limit=None)))
        assert plan["plans"], plan["skipped"]


class TestDropCountsSpanBothFeedsOfABrand:
    """Two feeds price overlapping but different slates.

    Maxing the per-feed counters got the shared markets right and under-counted
    everything only one feed saw — 54 reported against 82 real on live data.
    """

    def _split_stale(self):
        started = AS_OF - timedelta(hours=1)
        rows = []
        for index, source in enumerate(("betmgm", "betmgm", "an_betmgm", "an_betmgm")):
            key = f"MLB-A{index}@MLB-B{index}:2026-07-28"
            rows += [
                make_quote(source=source, selection=Selection.AWAY, decimal_odds=3.0,
                           event_key=key, home_participant=f"MLB-B{index}",
                           away_participant=f"MLB-A{index}", commence_time=started),
                make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                           event_key=key, home_participant=f"MLB-B{index}",
                           away_participant=f"MLB-A{index}", commence_time=started),
            ]
        return rows

    def test_markets_only_one_feed_saw_are_still_counted(self):
        plan = _the_plan(
            _plans([_offer(source="betmgm")], self._split_stale()),
            key="betmgm|offer-1",
        )
        # Four distinct stale markets: two at each feed, none shared.
        assert plan["skipped"].get("already_started") == 4, plan["skipped"]


class TestAMixedCauseNeverClaimsEveryOne:
    """"Every one of this book's 5" printed beside a map reading 5 + 2.

    A sentence contradicting its own evidence; mixed causes now enumerate.
    """

    def _mixed(self):
        started = AS_OF - timedelta(hours=1)
        rows = []
        for index in range(2):
            key = f"MLB-S{index}@MLB-T{index}:2026-07-28"
            rows += [
                make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                           event_key=key, home_participant=f"MLB-T{index}",
                           away_participant=f"MLB-S{index}", commence_time=started),
                make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                           event_key=key, home_participant=f"MLB-T{index}",
                           away_participant=f"MLB-S{index}", commence_time=started),
            ]
        # A live market where the promo book's only price is suspended.
        live = "MLB-NYY@MLB-BOS:2026-07-29"
        rows += [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       event_key=live, home_participant="MLB-BOS",
                       away_participant="MLB-NYY", status=QuoteStatus.SUSPENDED),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       event_key=live, home_participant="MLB-BOS",
                       away_participant="MLB-NYY"),
        ]
        return rows

    def test_both_causes_are_named_and_neither_claims_all(self):
        plan = _the_plan(_plans([_offer()], self._mixed()))
        assert plan["strategy"] == "no_odds_coverage"
        assert plan["skipped"].get("already_started") == 2
        assert plan["skipped"].get("no_active_price") == 1
        joined = " ".join(plan["caveats"])
        assert "every one" not in joined, joined
        assert "already started ×2" in joined and "no active price ×1" in joined, joined


class TestAStaleCrowdCannotHideAViableHedge:
    """Freshness is a per-source fact and belongs with the other pre-filters.

    Left to the combo loop it let four stale-but-better books fill the four-deep
    window and hide a viable plan, reporting only ``observation_spread ×4``.
    """

    def _slate(self):
        stale = AS_OF - timedelta(seconds=600)
        rows = [make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.1)]
        for source in ("bovada", "caesars", "hardrock", "betmgm"):
            rows.append(make_quote(source=source, selection=Selection.HOME,
                                   decimal_odds=2.5, observed_at=stale))
        rows.append(make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.05))
        return rows

    def test_the_fresh_hedge_survives_four_stale_better_ones(self):
        plan = _the_plan(_plans([_offer()], self._slate()))
        assert plan["plans"], plan["skipped"]
        assert plan["plans"][0]["legs"][1]["source"] == "fanduel"

    def test_the_stale_books_are_still_counted(self):
        plan = _the_plan(_plans([_offer()], self._slate()))
        assert plan["skipped"].get("observation_spread") == 4, plan["skipped"]


class TestEveryKindAndRewardCombinationSaysSomething:
    """"No plan" and "no plan, for this reason" are different facts.

    Nineteen (kind, reward) pairs the enricher really produces returned no plan,
    no caveat and an empty skipped map — indistinguishable from a bug.
    """

    REWARDS = ("bonus_bets", "free_bet", "site_credit", "cash", "boost",
               "no_sweat", "risk_free", "")

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    @pytest.mark.parametrize("kind", [k.value for k in PromoKind])
    @pytest.mark.parametrize("reward", REWARDS)
    def test_a_plan_or_a_reason_always_comes_back(self, kind, reward):
        plan = _the_plan(_plans([_offer(kind=kind, reward_type=reward)], self._slate()))
        assert plan["plans"] or plan["caveats"], (kind, reward, plan)

    def test_a_risk_free_reward_reaches_the_no_sweat_planner(self):
        """The kind was handled and the reward was forgotten — the enricher
        writes ``risk_free`` for "risk-free bet" copy."""
        plan = _the_plan(_plans(
            [_offer(kind="other", reward_type="risk_free")], self._slate()
        ))
        assert plan["strategy"] in {"no_sweat_hedge", "text_only"}, plan["strategy"]
        # Either a priced insurance plan or a stated reason — never the silence
        # this combination used to answer with.
        assert plan["plans"] or plan["caveats"], plan

    def test_a_parlay_boost_with_no_credit_is_not_priced_at_all(self):
        """Correlated legs are not modelled, so a pure parlay boost gets no card."""
        plan = _the_plan(_plans(
            [_offer(kind="parlay_boost", reward_type="boost", bonus_amount=None)],
            self._slate(),
        ))
        assert plan["plans"] == [], plan["plans"]
        assert any("correlated" in c for c in plan["caveats"]), plan["caveats"]

    def test_a_parlay_that_pays_credit_prices_only_the_conversion(self):
        """The credit is an ordinary bonus bet once it lands; the parlay is not.

        Discarding both was the wrong half to throw away — and pricing the
        parlay as a qualifying leg would be inventing a settlement model.  The
        card prices the conversion and says which half is missing.
        """
        plan = _the_plan(_plans(
            [_offer(kind="parlay_boost", reward_type="bonus_bets", bonus_amount=300.0)],
            self._slate(),
        ))
        assert plan["strategy"] == "bonus_conversion"
        assert plan["plans"], plan
        assert plan["unit"]["kind"] == "bonus_credit"
        assert plan["unit"]["amount"] == pytest.approx(300.0)
        # No qualifying step is offered — that is the part not modelled.
        assert all(p.get("step") is None for p in plan["plans"]), plan["plans"]
        assert any("parlay" in c and "not" in c for c in plan["caveats"]), plan["caveats"]

    def test_a_boost_reward_on_another_kind_still_reaches_the_boost_planner(self):
        """Reward and kind each route independently; neither may swallow the other."""
        plan = _the_plan(_plans(
            [_offer(kind="other", reward_type="boost", bonus_amount=None,
                    title="50% profit boost")],
            self._slate(),
        ))
        assert plan["strategy"] in {"boost_locked", "boost_breakeven"}, plan["strategy"]

    def test_a_boost_kind_paying_credit_is_priced_as_credit(self):
        """A live shape: profit_boost carrying reward_type=bonus_bets.

        The boost branch tested the kind unconditionally, so a $300 bonus-bet
        drop was presented as a boost hunt with a negative worst case and no
        mention of the $300 — and a $250 *credit* was sized as $250 of the
        operator's own money.
        """
        plan = _the_plan(_plans(
            [_offer(kind="profit_boost", reward_type="bonus_bets", bonus_amount=250.0)],
            self._slate(),
        ))
        assert plan["strategy"] == "bonus_conversion", plan["strategy"]
        assert plan["unit"]["kind"] == "bonus_credit"
        assert plan["unit"]["amount"] == pytest.approx(250.0)
        promo = plan["plans"][0]["legs"][0]
        assert promo["stake_kind"] == "bonus", "credit was staked as cash"


class TestTheMinimumOddsCaveatOnlyAppearsWhenItWasApplied:
    """A sentence about a promo-side leg that does not exist is worse than none."""

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    def test_a_referral_offer_does_not_claim_a_promo_leg(self):
        plan = _the_plan(_plans(
            [_offer(kind="referral", reward_type="", min_odds="-200")], self._slate()
        ))
        assert not any("minimum odds" in c for c in plan["caveats"]), plan["caveats"]

    def test_a_scanning_strategy_still_says_so(self):
        plan = _the_plan(_plans([_offer(min_odds="-200")], self._slate()))
        assert any("minimum odds" in c for c in plan["caveats"]), plan["caveats"]


class TestTheQualifyingPatternReadsTheFormsVenuesWrite:
    """The narrowed pattern lost "$5+" and "&" — a silently downgraded strategy."""

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            ("Bet $5+, get $150 in bonus bets", 5.0),
            ("Bet $5+ and get $150 in bonus bets", 5.0),
            ("Bet $5 & get $150 in bonus bets", 5.0),
            ("Bet $5 minimum, get $200 in bonus bets", 5.0),
            ("Bet $5 - get $200 in bonus bets", 5.0),
            ("Bet $5 or more, get $150 in bonus bets", 5.0),
            # Still refused: a conditional refund is not a bet-and-get.
            ("Bet $50 first bet, if it loses get $50 back", None),
        ],
    )
    def test_the_written_forms_parse(self, summary, expected):
        assert parse_qualifying_stake(summary) == expected


class TestAnotherBooksParseFaultIsNotChargedToThisOne:
    """Exclusions are attributed per book and per market.

    A single global count meant a book with no coverage of its own reported
    other venues' parse faults, from games it never priced — noise presented as
    its own diagnosis.
    """

    def _slate(self):
        # Market 1: draftkings absent; bovada is a fixture outlier there.
        other = "MLB-NYY@MLB-BOS:2026-07-29"
        rows = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9,
                       event_key=other, home_participant="MLB-BOS",
                       away_participant="MLB-NYY"),
            make_quote(source="fanduel", selection=Selection.AWAY, decimal_odds=2.0,
                       event_key=other, home_participant="MLB-BOS",
                       away_participant="MLB-NYY"),
            make_quote(source="bovada", selection=Selection.HOME, decimal_odds=1.9,
                       event_key=other, home_participant="MLB-NYY",
                       away_participant="MLB-BOS"),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=2.0,
                       event_key=other, home_participant="MLB-NYY",
                       away_participant="MLB-BOS"),
        ]
        return rows

    def test_a_book_with_no_coverage_reports_no_one_elses_faults(self):
        plan = _the_plan(_plans([_offer()], self._slate()))
        assert plan["strategy"] == "no_odds_coverage"
        assert "legs_disagree_on_the_game" not in plan["skipped"], plan["skipped"]
        assert any("no rows from this book" in c for c in plan["caveats"]), plan["caveats"]


class TestThePayloadAndTheRendererAgreeOnFieldNames:
    """The one seam with two authors and no checker.

    The page's JS reads plan fields by name; the planner writes them by name;
    nothing compared the two.  The dashboard harness drives ``promoPlanHtml``
    from hand-written JS fixtures, and the autouse promo-DB isolation means the
    rendered page carries zero real offers — so a rename or a drop on either
    side ships silently and the card quietly loses a column.
    """

    #: Every plan-level key the renderer reads, read off the JS source below.
    RENDERED_PLAN_FIELDS = frozenset({
        "market", "period", "line", "side", "home_team", "away_team",
        "commence_time", "legs", "outcome_profits", "guaranteed_cash",
        "settled_cash", "conversion_pct", "quote_age_seconds", "notes", "step",
        "breakeven_boost_pct", "cost_per_100_wagered", "qualifying_cost",
    })
    RENDERED_LEG_FIELDS = frozenset({
        "role", "source", "selection", "line", "american_odds", "stake",
        "stake_kind",
    })

    def _quotes(self):
        return [
            make_quote(source="draftkings", market=Market.SPREAD,
                       selection=Selection.AWAY, line=1.5, decimal_odds=3.0),
            make_quote(source="fanduel", market=Market.SPREAD,
                       selection=Selection.HOME, line=-1.5, decimal_odds=1.5),
        ]

    def _a_plan(self):
        return _the_plan(_plans([_offer()], self._quotes()))["plans"][0]

    def _every_plan(self):
        """One plan from each strategy that produces cards.

        Several fields are strategy-specific — a step badge exists only on a
        two-step plan, a breakeven percentage only on an unstated boost — so
        the contract is over the *union*, and checking one plan would demand
        fields no single plan can carry.
        """
        quotes = self._quotes()
        offers = [
            _offer(offer_id="conv"),
            _offer(offer_id="qual", summary="Bet $5, get $150 in bonus bets",
                   bonus_amount=150.0),
            _offer(offer_id="sweat", kind="no_sweat", reward_type="no_sweat"),
            _offer(offer_id="boost", kind="odds_boost", reward_type="boost",
                   title="50% profit boost"),
            _offer(offer_id="brk", kind="odds_boost", reward_type="boost"),
            _offer(offer_id="roll", kind="deposit_match", reward_type="site_credit",
                   wagering_requirement="1x"),
        ]
        out = _plans(offers, quotes)["plans"]
        plans = [p for entry in out.values() for p in entry["plans"]]
        assert len(plans) >= 5, f"only {len(plans)} plans; the union is too thin"
        return plans

    def test_every_field_the_page_reads_is_one_the_planner_writes(self):
        from src.report_assets import JS

        produced = set().union(*(set(plan) for plan in self._every_plan()))
        missing = sorted(
            name for name in self.RENDERED_PLAN_FIELDS
            if name not in produced and f"plan.{name}" in JS
        )
        assert missing == [], (
            f"the page reads plan fields the planner never writes: {missing}"
        )

    def test_every_leg_field_the_page_reads_is_one_the_planner_writes(self):
        from src.report_assets import JS

        produced = set(self._a_plan()["legs"][0])
        missing = sorted(
            name for name in self.RENDERED_LEG_FIELDS
            if name not in produced and f"leg.{name}" in JS
        )
        assert missing == [], (
            f"the page reads leg fields the planner does not write: {missing}"
        )

    def test_the_guard_is_not_inert(self):
        """If the JS stops naming these fields, the checks above check nothing."""
        from src.report_assets import JS

        named = {n for n in self.RENDERED_PLAN_FIELDS if f"plan.{n}" in JS}
        assert len(named) >= 12, (
            f"only {len(named)} plan fields are still referenced as plan.<name>; "
            "the extraction stopped matching and this guard is inert"
        )
        leg_named = {n for n in self.RENDERED_LEG_FIELDS if f"leg.{n}" in JS}
        assert len(leg_named) >= 6, (
            f"only {len(leg_named)} leg fields matched; this guard is inert"
        )

    def test_no_rendered_field_is_silently_none_on_a_real_plan(self):
        """A field the page prints must carry a value on an ordinary spread."""
        plan = self._a_plan()
        for name in ("market", "period", "commence_time", "home_team", "away_team",
                     "guaranteed_cash", "settled_cash", "quote_age_seconds"):
            assert plan.get(name) is not None, f"{name} is None on a real plan"
        for leg in plan["legs"]:
            for name in ("role", "source", "selection", "american_odds", "stake"):
                assert leg.get(name) is not None, f"leg {name} is None"


# ── round 5 adversarial findings ─────────────────────────────────────────────


class TestTheStatedLimitSurvivesRescaling:
    """Conversions are solved at $100 and scaled to the offer's real amount.

    The limit check lived only in the solver, so it was applied at $100 and
    never again — a hedge fitting a $200 limit at $100 of credit became a
    $1333 leg at $1000 of credit, under a "+$666.65 worst case".  Every offer
    whose bonus is not exactly $100 bypassed round 4's fix, which is nearly all
    of them.
    """

    def _slate(self, limit=200.0):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="matchbook", selection=Selection.HOME, decimal_odds=1.5,
                       limit_amount=limit),
        ]

    @pytest.mark.parametrize("bonus", [200.0, 1000.0, 5000.0])
    def test_a_scaled_hedge_over_the_limit_is_not_printed(self, bonus):
        plan = _the_plan(_plans([_offer(bonus_amount=bonus)], self._slate()))
        for concrete in plan["plans"]:
            for leg in concrete["legs"]:
                if leg["role"] != "hedge":
                    continue
                assert leg["stake"] <= 200.0 + 1e-9, (bonus, leg)

    def test_the_refusal_is_counted_rather_than_silent(self):
        plan = _the_plan(_plans([_offer(bonus_amount=1000.0)], self._slate()))
        assert plan["plans"] == []
        assert plan["skipped"].get("hedge_over_stated_limit", 0) >= 1, plan["skipped"]

    def test_the_same_offer_plans_when_the_size_is_there(self):
        plan = _the_plan(_plans([_offer(bonus_amount=1000.0)],
                                self._slate(limit=5000.0)))
        assert plan["plans"], plan["skipped"]
        assert plan["plans"][0]["legs"][1]["stake"] == pytest.approx(1333.30, abs=0.02)


class TestABreakevenCardsStakesReachItsOwnClaim:
    """"needs a 23.5%+ boost" beside "worst case −$10.00" is self-contradicting.

    The legs came from the cash scan, so the hedge was sized for the unboosted
    price and the hedge-wins outcome contained no boost at all — the printed
    floor was invariant in the percentage being advertised.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.9),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9),
        ]

    def test_the_printed_stakes_break_even_at_the_printed_boost(self):
        offer = _offer(kind="odds_boost", reward_type="boost", bonus_amount=None)
        plan = _the_plan(_plans([offer], self._slate()))
        assert plan["strategy"] == "boost_breakeven"
        assert plan["plans"], plan
        card = plan["plans"][0]
        # 1/(1 − 1/1.9) = 2.111; needed = (1.111/0.9 − 1) = 23.46%.
        assert card["breakeven_boost_pct"] == pytest.approx(23.46, abs=0.01)
        # And the stakes now actually reach it: $100 at the boosted 2.111
        # returns $211.11, hedged 211.11/1.9 = $111.11, floor exactly zero.
        assert card["legs"][1]["stake"] == pytest.approx(111.11, abs=0.02)
        assert card["guaranteed_cash"] == pytest.approx(0.0, abs=0.02)

    def test_the_floor_is_not_invariant_in_the_advertised_boost(self):
        """The defect's signature: a floor that no boost could move."""
        offer = _offer(kind="odds_boost", reward_type="boost", bonus_amount=None)
        card = _the_plan(_plans([offer], self._slate()))["plans"][0]
        cash_only_floor = -10.0  # what the unboosted stakes lock at 1.9 vs 1.9
        assert card["guaranteed_cash"] != pytest.approx(cash_only_floor, abs=0.01)


class TestAnExcludedBookIsNotAlsoCalledSuspended:
    """One market, one cause, reported once.

    Round 4's exclusions pop a source before round 3's suspended bookkeeping
    runs, and that guard only knew about duplicate-price faults — so a book
    thrown out for a parse fault was also counted as having had every price
    suspended, under a caveat that says the opposite.
    """

    def _crossed_only(self):
        return [
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=2.2),
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.2),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=1.95),
            make_quote(source="bovada", selection=Selection.HOME, decimal_odds=1.95),
        ]

    def test_the_parse_fault_is_not_double_reported(self):
        plan = _the_plan(_plans([_offer()], self._crossed_only()))
        assert plan["skipped"].get("source_prices_itself_to_lose") == 1
        assert "no_active_price" not in plan["skipped"], plan["skipped"]

    def test_the_caveat_does_not_claim_suspension(self):
        plan = _the_plan(_plans([_offer()], self._crossed_only()))
        joined = " ".join(plan["caveats"])
        assert "suspended" not in joined, joined


class TestAnOrderBookIsJudgedNetOfItsOwnFee:
    """A resting book at 49c/49c is not a parse fault.

    It sums to 0.98 gross and above 1.0 net of the contract fee, and makers
    legitimately sit there — ``src/arb.py`` names that exact shape.  Judging
    every venue on the gross sum deleted the slate's second-largest feed from
    markets the detector still prices.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            # Kalshi's gross pair sums under 1.0; net of its fee it does not.
            make_quote(source="kalshi", selection=Selection.HOME, decimal_odds=2.04),
            make_quote(source="kalshi", selection=Selection.AWAY, decimal_odds=2.04),
        ]

    def test_the_exchange_is_not_deleted_for_a_gross_sum_under_one(self):
        out = build_promo_plans([_offer()], self._slate(), as_of=AS_OF,
                                one_counterparty={})
        plan = out["plans"]["draftkings|offer-1"]
        assert plan["skipped"].get("source_prices_itself_to_lose", 0) == 0, plan["skipped"]

    def test_a_sportsbook_is_still_judged_on_the_quoted_price(self):
        rows = [
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=2.2),
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.2),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=1.95),
            make_quote(source="bovada", selection=Selection.HOME, decimal_odds=1.95),
        ]
        plan = _the_plan(_plans([_offer()], rows))
        assert plan["skipped"].get("source_prices_itself_to_lose") == 1


class TestATrailingConditionalIsNotAGuaranteedGet:
    """"get $50 back … if your first bet loses" is a refund, not a bet-and-get.

    Priced as credit that always arrives it doubles the offer's real value: the
    credit lands only when the qualifying bet loses.
    """

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            ("Bet $5, get $150 in bonus bets", 5.0),
            ("Bet $5+ and get $150 in bonus bets", 5.0),
            # Conditional, in both orderings and both wordings.
            ("Bet $50, get $50 back in bonus bets if your first bet loses", None),
            ("Bet $50 first bet, if it loses get $50 back", None),
            ("Bet $25, get $25 money back", None),
            ("Bet $20, get $20 as a second chance bet", None),
        ],
    )
    def test_only_unconditional_copy_parses(self, summary, expected):
        assert parse_qualifying_stake(summary) == expected


class TestOneStaleBookCountsOnce:
    """A three-way market considered three promo sides counted one stale book six
    times, so a single refusal read as six."""

    def test_a_single_stale_book_is_one_refusal(self):
        stale = AS_OF - timedelta(seconds=600)
        event = "SOCCER-ars@SOCCER-che:2026-07-28"

        def q(source, selection, odds, observed=AS_OF):
            return make_quote(source=source, sport=Sport.SOCCER, league="EPL",
                              event_key=event, selection=selection,
                              decimal_odds=odds, observed_at=observed)

        rows = [
            q("draftkings", Selection.HOME, 1.90), q("draftkings", Selection.AWAY, 3.70),
            q("draftkings", Selection.DRAW, 4.40),
            q("fanduel", Selection.HOME, 1.88), q("fanduel", Selection.AWAY, 3.60),
            q("fanduel", Selection.DRAW, 4.30),
            # Prices that hold an edge: a book whose own three-way sums below
            # 1.0 is excluded as a parse fault before freshness is consulted.
            q("bovada", Selection.HOME, 1.85, stale),
            q("bovada", Selection.AWAY, 3.50, stale),
            q("bovada", Selection.DRAW, 4.20, stale),
        ]
        plan = _the_plan(_plans([_offer()], rows))
        # One book, three selections — three (book, selection) refusals, not the
        # nine that counting per promo side produced.
        assert plan["skipped"].get("observation_spread") == 3, plan["skipped"]


# ── round 6 adversarial findings ─────────────────────────────────────────────


def _many_markets(hedge_limit=None, hedge_source="bovada", spare_source="onexbet"):
    """Four markets: three whose hedge carries a limit, one whose hedge does not.

    The three limited ones rank above the spare on conversion, so anything that
    truncates before gating loses the only market that survives scaling.
    """
    rows = []
    for index, (promo_odds, hedge_odds) in enumerate(
        [(3.5, 1.40), (3.4, 1.42), (3.3, 1.44)]
    ):
        key = f"MLB-L{index}@MLB-M{index}:2026-07-28"
        rows += [
            make_quote(source="draftkings", selection=Selection.HOME,
                       decimal_odds=promo_odds, event_key=key,
                       home_participant=f"MLB-M{index}", away_participant=f"MLB-L{index}"),
            make_quote(source=hedge_source, selection=Selection.AWAY,
                       decimal_odds=hedge_odds, event_key=key, limit_amount=hedge_limit,
                       home_participant=f"MLB-M{index}", away_participant=f"MLB-L{index}"),
        ]
    spare = "MLB-SP@MLB-SQ:2026-07-28"
    rows += [
        make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=3.2,
                   event_key=spare, home_participant="MLB-SQ", away_participant="MLB-SP"),
        make_quote(source=spare_source, selection=Selection.AWAY, decimal_odds=1.46,
                   event_key=spare, home_participant="MLB-SQ", away_participant="MLB-SP"),
    ]
    return rows, spare


class TestGatingHappensBeforeTruncation:
    """Filter, then take the best N — the third time this pattern has bitten.

    Rounds 3 and 4 fixed it for the counterparty and freshness gates; round 5's
    own new limit gate landed the same way.  Three markets refused at ranks 1-3
    hid a viable +$739 plan at rank 4, and the offer printed "no hedgeable
    market on this book passed every gate".
    """

    def test_a_viable_market_below_the_refused_ones_still_prints(self):
        rows, spare = _many_markets(hedge_limit=250.0)
        plan = _the_plan(_plans([_offer(bonus_amount=1000.0)], rows))
        assert plan["plans"], plan["skipped"]
        assert plan["plans"][0]["event_key"] == spare, [
            p["event_key"] for p in plan["plans"]
        ]

    def test_the_refusals_are_still_counted(self):
        rows, _ = _many_markets(hedge_limit=250.0)
        plan = _the_plan(_plans([_offer(bonus_amount=1000.0)], rows))
        assert plan["skipped"].get("hedge_over_stated_limit", 0) >= 1, plan["skipped"]

    def test_no_false_claim_that_nothing_passed(self):
        rows, _ = _many_markets(hedge_limit=250.0)
        plan = _the_plan(_plans([_offer(bonus_amount=1000.0)], rows))
        assert not any("passed every gate" in c for c in plan["caveats"]), plan["caveats"]

    def test_the_boost_re_solve_gates_before_truncating_too(self):
        """The refused markets must rank strictly above the survivor.

        Three already-profitable markets (0% needed) sort ahead of one needing
        a real boost; their *boosted* stakes exceed the hedge's published size
        while their cash stakes did not, so the re-solve refuses exactly the
        top three.
        """
        # Cash hedges of 98.0-100.0 fit the $103 limit, so these three survive
        # the scan and become candidates; the *boosted* re-solve needs
        # 103.1-105.3 and refuses them.  They need ~10.8% and so sort above the
        # spare's 23.5%, which is what makes truncate-before-gating fatal.
        rows = []
        for index, (promo_odds, hedge_odds) in enumerate(
            [(1.95, 1.95), (1.94, 1.96), (1.93, 1.97)]
        ):
            key = f"MLB-P{index}@MLB-Q{index}:2026-07-28"
            rows += [
                make_quote(source="draftkings", selection=Selection.HOME,
                           decimal_odds=promo_odds, event_key=key,
                           home_participant=f"MLB-Q{index}",
                           away_participant=f"MLB-P{index}"),
                make_quote(source="bovada", selection=Selection.AWAY,
                           decimal_odds=hedge_odds, event_key=key, limit_amount=103.0,
                           home_participant=f"MLB-Q{index}",
                           away_participant=f"MLB-P{index}"),
            ]
        spare = "MLB-SP@MLB-SQ:2026-07-28"
        rows += [
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=1.90,
                       event_key=spare, home_participant="MLB-SQ",
                       away_participant="MLB-SP"),
            make_quote(source="onexbet", selection=Selection.AWAY, decimal_odds=1.90,
                       event_key=spare, home_participant="MLB-SQ",
                       away_participant="MLB-SP"),
        ]
        offer = _offer(kind="odds_boost", reward_type="boost", bonus_amount=None)
        plan = _the_plan(_plans([offer], rows))
        assert plan["strategy"] == "boost_breakeven"
        assert plan["skipped"].get("hedge_over_stated_limit", 0) >= 1, plan["skipped"]
        assert plan["plans"], plan["skipped"]
        assert any(p["event_key"] == spare for p in plan["plans"]), [
            p["event_key"] for p in plan["plans"]
        ]


class TestAlreadyProfitableBoostMarketsRankOnMoney:
    """Every market needing no boost clamps to 0.0, so the event key decided.

    A +$50 card lost to a +$5 one alphabetically.
    """

    def test_the_richest_zero_boost_market_leads(self):
        rows = []
        # Four markets that already lock, in ascending profit; the richest sorts
        # last by event key, so only a money-aware tiebreak surfaces it.
        for index, (promo_odds, hedge_odds) in enumerate(
            [(2.10, 2.10), (2.20, 2.05), (2.40, 1.95), (2.60, 1.90)]
        ):
            key = f"MLB-{chr(97 + index)}A@MLB-{chr(97 + index)}B:2026-07-28"
            rows += [
                make_quote(source="draftkings", selection=Selection.HOME,
                           decimal_odds=promo_odds, event_key=key,
                           home_participant=f"MLB-{chr(97 + index)}B",
                           away_participant=f"MLB-{chr(97 + index)}A"),
                make_quote(source="bovada", selection=Selection.AWAY,
                           decimal_odds=hedge_odds, event_key=key,
                           home_participant=f"MLB-{chr(97 + index)}B",
                           away_participant=f"MLB-{chr(97 + index)}A"),
            ]
        offer = _offer(kind="odds_boost", reward_type="boost", bonus_amount=None)
        plans = _the_plan(_plans([offer], rows))["plans"]
        assert plans, "no boost cards at all"
        floors = [p["guaranteed_cash"] for p in plans]
        assert floors == sorted(floors, reverse=True), floors


class TestSiteCreditIsNotConvertedAsABonusBet:
    """The same $250 must not be worth two different amounts by promo kind.

    Site credit is cash needing rollover — the dispatch says so two branches
    down — but a parlay boost paying site credit was routed to conversion and
    valued at $162.50 against $250 everywhere else.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    @pytest.mark.parametrize("kind", ["parlay_boost", "odds_boost", "profit_boost",
                                      "other", "deposit_match"])
    def test_site_credit_is_always_a_rollover(self, kind):
        plan = _the_plan(_plans(
            [_offer(kind=kind, reward_type="site_credit", bonus_amount=250.0,
                    wagering_requirement="1x")],
            self._slate(),
        ))
        assert plan["strategy"] == "rollover_grind", (kind, plan["strategy"])

    def test_a_parlay_paying_bonus_bets_still_converts(self):
        plan = _the_plan(_plans(
            [_offer(kind="parlay_boost", reward_type="bonus_bets", bonus_amount=300.0)],
            self._slate(),
        ))
        assert plan["strategy"] == "bonus_conversion"


class TestAConditionalRewardIsPricedAsInsurance:
    """Refusing to parse the qualifying stake was not enough.

    The offer still reached plain conversion, which prices the whole reward as
    credit in hand — the same dollar figure either way — with nothing saying
    the credit only lands when the qualifying bet loses.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    def test_a_second_chance_offer_is_valued_as_a_refund(self):
        offer = _offer(
            summary="Bet $50, get $50 back in bonus bets if your first bet loses",
            bonus_amount=50.0,
        )
        plan = _the_plan(_plans([offer], self._slate()))
        assert plan["strategy"] in {"no_sweat_hedge", "text_only"}, plan["strategy"]
        assert plan.get("refund_conversion_pct") is not None or plan["caveats"]

    def test_unconditional_copy_is_untouched(self):
        offer = _offer(summary="Bet $5, get $150 in bonus bets", bonus_amount=150.0)
        plan = _the_plan(_plans([offer], self._slate()))
        assert plan["strategy"] == "qualify_then_convert", plan["strategy"]

    @pytest.mark.parametrize(
        ("summary", "conditional"),
        [
            ("Bet $50, get $50 back in bonus bets if your first bet loses", True),
            ("Bet $50, get $50 as a bonus bet when your first bet loses", True),
            ("Bet $50, get up to $50 back should your first bet lose", True),
            ("Bet $50, get $50 in bonus bets on a losing first bet", True),
            ("Bet $100, get $100 in bonus bets unless your bet wins", True),
            ("Bet $25, get $25 money back", True),
            # Innocent uses of the same words: a rebate and a fee refund.
            ("Bet $5, get $150 in bonus bets plus 10% back in bonus bets on parlays",
             False),
            ("Bet $10, get $100 in bonus bets; we refund the transfer fee", False),
        ],
    )
    def test_only_real_conditions_are_detected(self, summary, conditional):
        parsed = parse_qualifying_stake(summary)
        assert (parsed is None) is conditional, (summary, parsed)


class TestThePrintedBoostMatchesThePillAboveIt:
    """The note fed an unrounded float straight onto the card.

    The pill read "needs a 0.0%+ boost" and the note under it read "priced with
    the stated 4.44089e-14% boost" — two numbers for one quantity, one in
    scientific notation.
    """

    def test_no_note_prints_scientific_notation(self):
        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.9),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9),
        ]
        offer = _offer(kind="odds_boost", reward_type="boost", bonus_amount=None)
        for concrete in _the_plan(_plans([offer], rows))["plans"]:
            for note in concrete["notes"]:
                assert "e-" not in note and "e+" not in note, note

    def test_the_note_and_the_pill_agree(self):
        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.9),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9),
        ]
        offer = _offer(kind="odds_boost", reward_type="boost", bonus_amount=None)
        card = _the_plan(_plans([offer], rows))["plans"][0]
        boost = card["breakeven_boost_pct"]
        assert any(f"{round(boost, 2):g}% boost" in note for note in card["notes"]), (
            card["breakeven_boost_pct"], card["notes"]
        )


class TestTheQualifyingCostNeverPrintsNegativeZero:
    """``max(-0.0, 0.0)`` returns ``-0.0`` — the clamp was a no-op for exactly
    the input it was added to normalise, and the live card said "$-0.00"."""

    def test_a_free_round_trip_costs_zero_not_minus_zero(self):
        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        offer = _offer(summary="Bet $5, get $150 in bonus bets", bonus_amount=150.0)
        plan = _the_plan(_plans([offer], rows))
        for caveat in plan["caveats"]:
            assert "$-0.00" not in caveat, caveat
        qualify = next((p for p in plan["plans"] if p.get("step") == "qualify"), None)
        if qualify is not None:
            import math
            assert not math.copysign(1.0, qualify["qualifying_cost"]) < 0 or \
                qualify["qualifying_cost"] != 0.0, qualify["qualifying_cost"]


class TestTwoHedgesMustAlsoBeFreshRelativeToEachOther:
    """Freshness is a property of the whole position, not of each pair.

    The pool pre-filter measures every hedge against the *promo* leg, so on a
    three-way market two hedges can each sit inside the window and 200s apart
    from one another.  Only the combination gate catches that, and nothing
    tested it — a plan whose legs no operator ever saw simultaneously.
    """

    EVENT = "SOCCER-ars@SOCCER-che:2026-07-28"

    def _slate(self):
        def q(source, selection, odds, offset):
            return make_quote(
                source=source, sport=Sport.SOCCER, league="EPL",
                event_key=self.EVENT, selection=selection, decimal_odds=odds,
                observed_at=AS_OF - timedelta(seconds=offset),
            )

        return [
            # Promo book, in the middle of the window.
            q("draftkings", Selection.AWAY, 3.70, 100),
            q("draftkings", Selection.HOME, 1.90, 100),
            q("draftkings", Selection.DRAW, 4.40, 100),
            # One book seen at the same instant as the promo leg — pairable.
            q("betmgm", Selection.HOME, 1.88, 100),
            q("betmgm", Selection.DRAW, 4.30, 100),
            q("betmgm", Selection.AWAY, 3.60, 100),
            # Two books each within 180s of the promo leg but 260s apart from
            # each other: 20s ago and 280s ago.
            q("caesars", Selection.HOME, 1.95, 280),
            q("caesars", Selection.DRAW, 4.45, 280),
            q("caesars", Selection.AWAY, 3.75, 280),
            q("bovada", Selection.HOME, 1.93, 20),
            q("bovada", Selection.DRAW, 4.42, 20),
            q("bovada", Selection.AWAY, 3.72, 20),
        ]

    def test_no_plan_mixes_two_hedges_seen_far_apart(self):
        plan = _the_plan(_plans([_offer()], self._slate()))
        assert plan["plans"], plan["skipped"]
        for concrete in plan["plans"]:
            stamps = [
                datetime.fromisoformat(leg["observed_at"]) for leg in concrete["legs"]
            ]
            spread = max(stamps) - min(stamps)
            assert spread <= timedelta(seconds=180), (spread, concrete["legs"])

    def test_the_combination_refusal_is_counted(self):
        plan = _the_plan(_plans([_offer()], self._slate()))
        assert plan["skipped"].get("observation_spread", 0) >= 1, plan["skipped"]


class TestAMislabelledSportDoesNotDecideTheMarket:
    """A group holding two sports has unknowable settlement rules.

    The detector keeps the majority sport and drops the outliers; a genuine tie
    is refused outright, because then there is no consensus to be an outlier
    from.  Neither branch had a test, so the hedge could be drawn from the book
    that mislabelled the sport — a different game's price on this game's card.
    """

    KEY = "MLB-PHI@MLB-MIA:2026-07-28"

    def _majority(self):
        """Two books say baseball, one says basketball."""
        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
            # The outlier: same event key, wrong sport, better price.
            make_quote(source="bovada", selection=Selection.HOME, decimal_odds=1.9,
                       sport=Sport.BASKETBALL, league="WNBA"),
        ]
        return rows

    def test_the_outlier_book_is_not_used(self):
        plan = _the_plan(_plans([_offer()], self._majority()))
        assert plan["plans"], plan["skipped"]
        for concrete in plan["plans"]:
            assert all(leg["source"] != "bovada" for leg in concrete["legs"]), concrete

    def test_the_majority_still_gets_its_plan(self):
        best = _the_plan(_plans([_offer()], self._majority()))["plans"][0]
        assert best["legs"][1]["source"] == "fanduel"
        assert best["guaranteed_cash"] == pytest.approx(66.66, abs=0.02)

    def test_a_tie_is_refused_and_counted(self):
        """One book each — no majority, so no consensus to judge against."""
        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       sport=Sport.BASKETBALL, league="WNBA"),
        ]
        out = _plans([_offer()], rows)
        plan = _the_plan(out)
        assert plan["plans"] == []
        assert out["meta"]["unusable_groups"].get("mixed_sport") == 1, out["meta"]
        assert plan["skipped"].get("mixed_sport") == 1, plan["skipped"]


class TestABookOfferingADifferentContractIsNotAHedge:
    """A two-way book cannot hedge a three-way market.

    In a window where the draw is priced, a book without a draw price settles a
    level result differently — refusing it is the whole point of the
    ambiguous-tie gate, and neither the promo-side nor the hedge-side site had
    a test.
    """

    EVENT = "SOCCER-ars@SOCCER-che:2026-07-28"

    def _slate(self):
        def q(source, selection, odds):
            return make_quote(source=source, sport=Sport.SOCCER, league="EPL",
                              event_key=self.EVENT, selection=selection,
                              decimal_odds=odds)

        return [
            q("draftkings", Selection.AWAY, 3.70),
            q("draftkings", Selection.HOME, 1.90),
            q("draftkings", Selection.DRAW, 4.40),
            # A complete three-way hedge, and a two-way book that is not one.
            q("betmgm", Selection.HOME, 1.88),
            q("betmgm", Selection.DRAW, 4.30),
            q("betmgm", Selection.AWAY, 3.60),
            # Priced as a genuine two-way market (draw-no-bet style), so its
            # own book sums above 1.0 and the self-crossing gate leaves it
            # alone — otherwise that gate fires first and this tests nothing.
            # A two-way book in a draw-priced window sums *under* 1.0 only
            # because the draw's share is missing.
            q("bovada", Selection.HOME, 1.60),
            q("bovada", Selection.AWAY, 2.50),
        ]

    def test_the_two_way_book_is_not_a_hedge_on_a_three_way_market(self):
        plan = _the_plan(_plans([_offer()], self._slate()))
        assert plan["plans"], plan["skipped"]
        for concrete in plan["plans"]:
            assert all(leg["source"] != "bovada" for leg in concrete["legs"]), concrete

    def test_the_refusal_is_counted(self):
        plan = _the_plan(_plans([_offer()], self._slate()))
        assert plan["skipped"].get("ambiguous_tie_settlement", 0) >= 1, plan["skipped"]

    def test_the_complete_book_still_hedges(self):
        best = _the_plan(_plans([_offer()], self._slate()))["plans"][0]
        assert {leg["source"] for leg in best["legs"] if leg["role"] == "hedge"} == {"betmgm"}


# ── round 7 adversarial findings ─────────────────────────────────────────────


class TestAWinConditionalRewardIsNotInsurance:
    """"…if your bet wins" is the opposite of a refund.

    A refund detector matching win-words routed a bet-and-get to the loss-refund
    model, which pays when the promo leg *loses*: it staked $200 of the
    operator's own money where the credit model staked credit, and printed
    +$88.88 where the printed legs give −$44.44.
    """

    WIN = "Bet $5, get $200 in bonus bets if your bet wins"
    LOSS = "Bet $50, get $50 back in bonus bets if your first bet loses"

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    def test_a_win_condition_does_not_reach_the_refund_model(self):
        plan = _the_plan(_plans(
            [_offer(summary=self.WIN, bonus_amount=200.0)], self._slate()
        ))
        assert plan["strategy"] != "no_sweat_hedge", plan["strategy"]

    def test_the_credit_is_staked_as_credit(self):
        """The qualifying leg is real money; the conversion leg is the credit.

        Under the refund model both were cash — $200 of the operator's own
        money on a leg the credit model funds with credit.
        """
        plan = _the_plan(_plans(
            [_offer(summary=self.WIN, bonus_amount=200.0)], self._slate()
        ))
        assert plan["plans"], plan
        convert = [p for p in plan["plans"] if p.get("step") != "qualify"]
        assert convert, plan["plans"]
        assert convert[0]["legs"][0]["stake_kind"] == "bonus", convert[0]["legs"]

    def test_the_card_says_the_credit_is_not_certain(self):
        """It is priced as credit in hand, which is only true once it lands."""
        plan = _the_plan(_plans(
            [_offer(summary=self.WIN, bonus_amount=200.0)], self._slate()
        ))
        assert any("only if the qualifying bet wins" in c for c in plan["caveats"]), (
            plan["caveats"]
        )

    def test_a_loss_condition_still_reaches_the_refund_model(self):
        plan = _the_plan(_plans(
            [_offer(summary=self.LOSS, bonus_amount=50.0)], self._slate()
        ))
        assert plan["strategy"] in {"no_sweat_hedge", "text_only"}, plan["strategy"]

    def test_copy_matching_both_detectors_is_not_treated_as_insurance(self):
        """"refunded … if your bet wins" trips the refund words and the win
        condition at once — which is exactly what the guard is for."""
        summary = "Bet $5, get $200 refunded in bonus bets if your bet wins"
        from src.promos.planner import _CONDITIONAL_REFUND, _WIN_CONDITIONAL

        assert _CONDITIONAL_REFUND.search(summary), "the fixture must trip both"
        assert _WIN_CONDITIONAL.search(summary), "the fixture must trip both"
        plan = _the_plan(_plans(
            [_offer(summary=summary, bonus_amount=200.0)], self._slate()
        ))
        assert plan["strategy"] != "no_sweat_hedge", plan["strategy"]

    @pytest.mark.parametrize(
        ("summary", "is_refund"),
        [
            ("Bet $5, get $200 in bonus bets if your bet wins", False),
            ("50% profit boost when your same-game parlay wins", False),
            ("Bet $5, get $150 in bonus bets", False),
            ("Bet $5, get $150 in bonus bets plus 10% back on parlays", False),
            ("Bet $50, get $50 back in bonus bets if your first bet loses", True),
            ("Bet $100, get $100 in bonus bets unless your bet wins", True),
            ("Bet $25, get $25 refunded in bonus bets on your first losing bet", True),
            ("$1500 Bonus Bets If Lose on Rangers vs Astros", True),
            ("$500 2nd Chance Free Bet", True),
        ],
    )
    def test_the_detector_separates_wins_from_losses(self, summary, is_refund):
        from src.promos.planner import _CONDITIONAL_REFUND, _WIN_CONDITIONAL

        assert bool(_CONDITIONAL_REFUND.search(summary)) is is_refund, summary
        if is_refund:
            assert not _WIN_CONDITIONAL.search(summary), summary


class TestTheHeadlineQuotesAConversionYouCanTake:
    """``expected_value`` read the ranked list, not the survivors.

    Six lines below the loop that had just been rewritten to gate before
    truncating, the headline still quoted the best conversion in the list —
    including one refused for exceeding its venue's stated size.
    """

    def test_no_headline_survives_when_every_conversion_was_refused(self):
        """Zero convert cards must mean zero expected value.

        The sharpest form of the defect: the planner refuses every conversion
        for exceeding the venue's size, prints no convert card, and still
        publishes "net value $666.60" from the list it just discarded.
        """
        rows = [
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=3.0),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=1.5,
                       limit_amount=200.0),
        ]
        offer = _offer(summary="Bet $5, get $1000 in bonus bets", bonus_amount=1000.0)
        plan = _the_plan(_plans([offer], rows))
        converts = [p for p in plan["plans"] if p.get("step") == "convert"]
        assert converts == [], plan["plans"]
        assert plan.get("expected_value") is None, plan["expected_value"]
        assert not any("best measured" in c for c in plan["caveats"]), plan["caveats"]

    def test_the_expected_value_uses_a_market_that_survived(self):
        """The richest conversion is at a book that will not take the stake.

        The refusal has to happen at *rescale*, not in the scan: a market
        refused during the scan never enters the ranked list, so the headline
        and the survivors agree by accident and the test proves nothing.  At
        $100 the hedge is $188 and fits a $500 maximum; at $1000 it is $1884
        and does not.
        """
        rows = [
            # 71.6% per $100 — and unplaceable once scaled.
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=3.6,
                       event_key="MLB-R0@MLB-S0:2026-07-28",
                       home_participant="MLB-S0", away_participant="MLB-R0"),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=1.38,
                       event_key="MLB-R0@MLB-S0:2026-07-28", limit_amount=500.0,
                       home_participant="MLB-S0", away_participant="MLB-R0"),
            # 40.0% per $100, no stated size.
            make_quote(source="draftkings", selection=Selection.HOME, decimal_odds=2.2,
                       event_key="MLB-R1@MLB-S1:2026-07-28",
                       home_participant="MLB-S1", away_participant="MLB-R1"),
            make_quote(source="fanduel", selection=Selection.AWAY, decimal_odds=2.0,
                       event_key="MLB-R1@MLB-S1:2026-07-28",
                       home_participant="MLB-S1", away_participant="MLB-R1"),
        ]
        offer = _offer(summary="Bet $5, get $1000 in bonus bets", bonus_amount=1000.0)
        plan = _the_plan(_plans([offer], rows))
        assert plan["strategy"] == "qualify_then_convert"
        printed = [p for p in plan["plans"] if p.get("step") == "convert"]
        assert printed, plan["skipped"]
        best_printed = max(p["conversion_pct"] for p in printed)
        assert best_printed < 71.0, "the refused market was printed after all"
        quoted = next(c for c in plan["caveats"] if "best measured" in c)
        rate = float(quoted.split("best measured ")[1].split("%")[0])
        assert rate <= best_printed + 0.05, (rate, best_printed, quoted)


class TestAParlayEarnedRewardAlwaysSaysSo:
    """Whichever model prices it, the correlated leg must be named.

    Routing site credit to rollover was right; doing it past both parlay
    branches left the offer asserting an unqualified $250 value for a reward
    contingent on hitting a parlay this planner refuses to model.
    """

    def _slate(self):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]

    @pytest.mark.parametrize("reward", ["site_credit", "bonus_bets"])
    def test_the_parlay_is_named_whatever_the_reward(self, reward):
        plan = _the_plan(_plans(
            [_offer(kind="parlay_boost", reward_type=reward, bonus_amount=250.0,
                    wagering_requirement="1x")],
            self._slate(),
        ))
        assert any("parlay" in c for c in plan["caveats"]), (reward, plan["caveats"])

    def test_site_credit_still_clears_by_rollover(self):
        plan = _the_plan(_plans(
            [_offer(kind="parlay_boost", reward_type="site_credit", bonus_amount=250.0,
                    wagering_requirement="1x")],
            self._slate(),
        ))
        assert plan["strategy"] == "rollover_grind"


class TestRefusalCountsAddAcrossPhasesAndMaxWithinOne:
    """Two scans of one slate see the same refusal twice; the scan and the
    rescale refuse disjoint sets.  One ``max`` over both was wrong both ways.
    """

    def _limited(self, count, limit):
        rows = []
        for index in range(count):
            key = f"MLB-C{index}@MLB-D{index}:2026-07-28"
            rows += [
                make_quote(source="draftkings", selection=Selection.HOME,
                           decimal_odds=3.0, event_key=key,
                           home_participant=f"MLB-D{index}",
                           away_participant=f"MLB-C{index}"),
                make_quote(source="bovada", selection=Selection.AWAY,
                           decimal_odds=1.5, event_key=key, limit_amount=limit,
                           home_participant=f"MLB-D{index}",
                           away_participant=f"MLB-C{index}"),
            ]
        return rows

    def test_every_refused_market_is_counted_once(self):
        """Five markets on the board, every one refused at scan time."""
        plan = _the_plan(_plans([_offer()], self._limited(5, limit=40.0)))
        assert plan["plans"] == []
        assert plan["skipped"].get("hedge_over_stated_limit") == 5, plan["skipped"]

    def test_refusals_at_rescale_are_counted_too(self):
        """A limit the $100 scan clears and the scaled stake does not.

        These are the post-scan refusals: at $100 of credit each hedge is
        $133.33 and fits, at $1000 it is $1333.30 and does not.  Counted apart
        from the scan's, because the two phases refuse disjoint markets.
        """
        plan = _the_plan(_plans([_offer(bonus_amount=1000.0)],
                                self._limited(4, limit=200.0)))
        assert plan["plans"] == []
        assert plan["skipped"].get("hedge_over_stated_limit") == 4, plan["skipped"]

    def test_a_two_scan_strategy_does_not_double_them(self):
        plan = _the_plan(_plans(
            [_offer(kind="no_sweat", reward_type="no_sweat")],
            self._limited(5, limit=40.0),
        ))
        counted = plan["skipped"].get("hedge_over_stated_limit", 0)
        assert counted <= 5, plan["skipped"]

    def test_the_bookkeeping_keys_never_reach_the_payload(self):
        plan = _the_plan(_plans([_offer()], self._limited(3, limit=40.0)))
        assert all(not r.startswith("__") for r in plan["skipped"]), plan["skipped"]


class TestNoPrintedFigureIsNegativeZero:
    """"-0.00" appeared in three more places after round 6 went looking for it."""

    def test_a_free_rollover_round_trip_is_positive_zero(self):
        """The rollover cost is ``-min(floor, 0.0)``, which is ``-0.0`` whenever
        the round trip does not lose money — and it reaches both the card and
        the clearing caveat."""
        import math

        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=2.05),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.05),
        ]
        offer = _offer(kind="deposit_match", reward_type="site_credit",
                       bonus_amount=500.0, wagering_requirement="5x")
        plan = _the_plan(_plans([offer], rows))
        for concrete in plan["plans"]:
            cost = concrete.get("cost_per_100_wagered")
            if cost is None:
                continue
            assert not (cost == 0.0 and math.copysign(1.0, cost) < 0), cost
            assert f"{cost:.2f}" != "-0.00", cost
        for caveat in plan["caveats"]:
            assert "$-0.00" not in caveat, caveat

    def test_no_payload_number_is_negative_zero(self):
        import math

        rows = [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=1.95),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.95),
        ]
        offers = [
            _offer(offer_id="boost", kind="odds_boost", reward_type="boost",
                   bonus_amount=None),
            _offer(offer_id="roll", kind="deposit_match", reward_type="site_credit",
                   bonus_amount=200.0, wagering_requirement="1x"),
            _offer(offer_id="qual", summary="Bet $5, get $150 in bonus bets",
                   bonus_amount=150.0),
        ]
        out = _plans(offers, rows)
        for key, entry in out["plans"].items():
            for caveat in entry["caveats"]:
                assert "$-0.00" not in caveat, (key, caveat)
            for concrete in entry["plans"]:
                for field in ("guaranteed_cash", "settled_cash",
                              "cost_per_100_wagered", "qualifying_cost"):
                    value = concrete.get(field)
                    if value is None:
                        continue
                    assert not (value == 0.0 and math.copysign(1.0, value) < 0), (
                        key, field, value
                    )


class TestAHalfLoseRefundsNothing:
    """A quarter line's surviving half is not a loss, so no refund is credited.

    ``_bonus_cash``'s mirror rule (half-win pays half) was pinned; this one was
    not.  Crediting the refund on a half-lose overstated the printed floor by
    the entire refund — $50 per $100 protected — on exactly the markets the
    docstring says it must not.
    """

    def _quarter(self):
        return [
            # 1.25, not 0.25: a quarter line rounding to zero cannot be landed
            # on in baseball (extra innings), so it enumerates no half-push and
            # the branch under test is never reached.
            make_quote(source="draftkings", market=Market.SPREAD,
                       selection=Selection.AWAY, line=1.25, decimal_odds=3.0),
            make_quote(source="fanduel", market=Market.SPREAD,
                       selection=Selection.HOME, line=-1.25, decimal_odds=2.0),
        ]

    def test_the_half_push_outcome_is_not_credited_with_the_refund(self):
        offer = _offer(kind="no_sweat", reward_type="no_sweat", bonus_amount=None)
        plan = _the_plan(_plans([offer], self._quarter()))
        cards = plan["plans"]
        if not cards:
            pytest.skip("this slate produced no protected-stake plan")
        outcomes = dict(cards[0]["outcome_profits"])
        half = [label for label in outcomes if label.startswith("half_push")]
        assert half, outcomes
        # The refund is credited only where the promo leg outright loses; the
        # half-push keeps half the stake and settles the rest, so it must sit
        # well below the fully-refunded branch.
        # The half-push keeps half the promo stake and settles the rest, so it
        # must not be credited the full refund: crediting it there lifted this
        # outcome to the level of the outright-loss branch.
        assert outcomes[half[0]] < max(outcomes.values()), outcomes


class TestTheStartedGameBoundary:
    """Earliest start wins, and a game starting exactly now has started.

    Both halves were free: reading the latest start instead of the earliest let
    a plan name a game one book already had underway, and a strict comparison
    let a game starting exactly at ``as_of`` through.
    """

    def _rows(self, first_offset, second_offset):
        key = "MLB-PHI@MLB-MIA:2026-07-28"
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0,
                       event_key=key, commence_time=AS_OF + first_offset),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5,
                       event_key=key, commence_time=AS_OF + second_offset),
        ]

    def test_the_earliest_start_decides(self):
        """One book says it kicked off an hour ago, the other says in an hour."""
        out = _plans([_offer()], self._rows(-timedelta(hours=1), timedelta(hours=1)))
        assert _the_plan(out)["plans"] == []
        assert out["meta"]["unusable_groups"].get("already_started") == 1, out["meta"]

    def test_a_game_starting_exactly_now_has_started(self):
        out = _plans([_offer()], self._rows(timedelta(0), timedelta(hours=1)))
        assert _the_plan(out)["plans"] == []
        assert out["meta"]["unusable_groups"].get("already_started") == 1, out["meta"]

    def test_a_game_starting_in_a_second_is_still_plannable(self):
        out = _plans([_offer()], self._rows(timedelta(seconds=1), timedelta(hours=1)))
        assert _the_plan(out)["plans"], out["meta"]


class TestTheStatedLimitBoundary:
    """A leg exactly at the published size is placeable; a cent over is not.

    Only "far over" was pinned, so the comparison could drift by a factor of
    three in one direction and refuse a legal stake in the other.
    """

    def _slate(self, limit):
        return [
            make_quote(source="draftkings", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="matchbook", selection=Selection.HOME, decimal_odds=1.5,
                       limit_amount=limit),
        ]

    def test_a_leg_exactly_at_the_limit_is_placed(self):
        plan = _the_plan(_plans([_offer()], self._slate(limit=133.33)))
        assert plan["plans"], plan["skipped"]
        assert plan["plans"][0]["legs"][1]["stake"] == pytest.approx(133.33)

    def test_a_cent_over_the_limit_is_refused(self):
        plan = _the_plan(_plans([_offer()], self._slate(limit=133.32)))
        assert plan["plans"] == [], plan["plans"]
        assert plan["skipped"].get("hedge_over_stated_limit", 0) >= 1


class TestTheFirstPartyFeedSuppliesThePromoLeg:
    """When both of a brand's feeds price a market, the book's own price wins.

    Nothing pinned the precedence, so the plan could quote Action Network's
    view while the "verify on the book" caveat — keyed on the first-party feed
    being present — stayed silent.
    """

    def test_the_books_own_price_is_the_promo_leg(self):
        rows = [
            make_quote(source="betmgm", selection=Selection.AWAY, decimal_odds=3.0),
            make_quote(source="an_betmgm", selection=Selection.AWAY, decimal_odds=2.5),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.5),
        ]
        out = build_promo_plans([_offer(source="betmgm")], rows, as_of=AS_OF,
                                commissions={}, one_counterparty={})
        plan = out["plans"]["betmgm|offer-1"]
        assert plan["plans"], plan["skipped"]
        promo = plan["plans"][0]["legs"][0]
        assert promo["source"] == "betmgm", promo
        assert promo["decimal_odds"] == pytest.approx(3.0)
        assert not any("Action Network" in c for c in plan["caveats"]), plan["caveats"]
