from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.dedup import make_dedup_key, should_alert
from src.models import ArbOpportunity, BestOutcome


def _opp(
    *,
    event_id: str = "evt_1",
    market_key: str = "h2h",
    point: float | None = None,
    margin: float = 0.03,
    books: list[tuple[str, str]] | None = None,
) -> ArbOpportunity:
    books = books or [("book_a", "Book A"), ("book_b", "Book B")]
    outcomes = [
        BestOutcome(
            outcome_name=f"Outcome {i}",
            bookmaker_key=key,
            bookmaker_title=title,
            decimal_odds=2.10,
            point=point,
        )
        for i, (key, title) in enumerate(books)
    ]
    return ArbOpportunity(
        sport_key="basketball_nba",
        event_id=event_id,
        home_team="Team A",
        away_team="Team B",
        commence_time=datetime.now(UTC),
        market_key=market_key,
        point=point,
        outcome_count=len(outcomes),
        margin=margin,
        implied_prob_sum=1 - margin,
        best_outcomes=outcomes,
        stakes=[50.0] * len(outcomes),
        guaranteed_profit=margin * 100,
        total_stake=100.0,
        detected_at=datetime.now(UTC),
    )


class TestMakeDedupKey:
    def test_same_opp_gives_same_key(self):
        a = _opp()
        b = _opp()
        assert make_dedup_key(a) == make_dedup_key(b)

    def test_different_margin_same_key(self):
        a = _opp(margin=0.03)
        b = _opp(margin=0.05)
        assert make_dedup_key(a) == make_dedup_key(b)

    def test_different_event_different_key(self):
        a = _opp(event_id="evt_1")
        b = _opp(event_id="evt_2")
        assert make_dedup_key(a) != make_dedup_key(b)

    def test_different_market_different_key(self):
        a = _opp(market_key="h2h")
        b = _opp(market_key="spreads")
        assert make_dedup_key(a) != make_dedup_key(b)

    def test_different_books_different_key(self):
        a = _opp(books=[("book_a", "A"), ("book_b", "B")])
        b = _opp(books=[("book_a", "A"), ("book_c", "C")])
        assert make_dedup_key(a) != make_dedup_key(b)

    def test_order_independent(self):
        """Reversing the list order of best_outcomes gives the same key."""
        a = _opp()
        b = _opp()
        b.best_outcomes = list(reversed(b.best_outcomes))
        assert make_dedup_key(a) == make_dedup_key(b)

    def test_spread_line_normalized_to_abs(self):
        a = _opp(market_key="spreads", point=-3.5)
        b = _opp(market_key="spreads", point=3.5)
        assert make_dedup_key(a) == make_dedup_key(b)


class TestShouldAlert:
    def test_first_alert_always_sends(self):
        send, reason = should_alert(_opp(), None)
        assert send is True
        assert reason is None

    def test_cooldown_blocks_repeat(self):
        last = {
            "sent_at": datetime.now(UTC) - timedelta(minutes=5),
            "margin": 0.03,
        }
        send, reason = should_alert(_opp(margin=0.03), last)
        assert send is False
        assert reason == "cooldown"

    def test_cooldown_expired_allows_alert(self):
        last = {
            "sent_at": datetime.now(UTC) - timedelta(minutes=11),
            "margin": 0.03,
        }
        send, reason = should_alert(_opp(margin=0.03), last)
        assert send is True
        assert reason is None

    def test_margin_improvement_bypasses_cooldown(self):
        last = {
            "sent_at": datetime.now(UTC) - timedelta(minutes=2),
            "margin": 0.03,
        }
        send, reason = should_alert(_opp(margin=0.036), last)
        assert send is True
        assert reason is None

    def test_insufficient_margin_improvement_blocked(self):
        last = {
            "sent_at": datetime.now(UTC) - timedelta(minutes=2),
            "margin": 0.03,
        }
        send, reason = should_alert(_opp(margin=0.034), last)
        assert send is False
        assert reason == "cooldown"
