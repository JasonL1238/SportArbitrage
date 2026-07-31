"""SMS alert formatting, ROI gate, and fail-soft send path."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from src.alerts import (
    AlertBook,
    alert_ready,
    format_alert,
    opportunity_alert_key,
    qualifies,
    send_sms,
)
from src.arb import ArbLeg, Opportunity
from src.schema import Market, Period, Selection, Sport
from tests.conftest import make_quote


def _opportunity(*, roi_prices=(2.2, 2.2), stake=100.0) -> Opportunity:
    """Synthetic two-way MLB moneyline sized so arithmetic is obvious.

    Equal prices of 2.2 → sum implied 1/2.2 * 2 ≈ 0.909 → ROI ≈ 10%.
    """
    home = make_quote(
        source="fanduel",
        selection=Selection.HOME,
        decimal_odds=roi_prices[0],
        home_team="Miami Marlins",
        away_team="Philadelphia Phillies",
    )
    away = make_quote(
        source="pinnacle",
        selection=Selection.AWAY,
        decimal_odds=roi_prices[1],
        source_event_id="evt-2",
        source_market_id="mkt-2",
        home_team="Miami Marlins",
        away_team="Philadelphia Phillies",
    )
    stakes = [stake / 2, stake / 2]
    legs = (
        ArbLeg(quote=home, stake=stakes[0]),
        ArbLeg(quote=away, stake=stakes[1]),
    )
    payout = stakes[0] * roi_prices[0]
    profit = payout - stake
    return Opportunity(
        event_key=home.event_key,
        sport=Sport.BASEBALL,
        home_team="Miami Marlins",
        away_team="Philadelphia Phillies",
        commence_time=datetime(2026, 7, 28, 22, 41, tzinfo=UTC),
        market=Market.MONEYLINE,
        period=Period.FULL_GAME,
        side=None,
        line=None,
        legs=legs,
        total_stake=stake,
        outcome_profits=(("home", profit), ("away", profit)),
        max_total_stake=None,
    )


class TestQualifies:
    def test_ten_percent_clears_default_floor(self) -> None:
        assert qualifies(_opportunity())

    def test_one_percent_misses_default_floor(self) -> None:
        # 1/1.51 + 1/1.51 ≈ 1.3245 → negative; use 2.02/2.02 → ~1% ROI
        opp = _opportunity(roi_prices=(2.02, 2.02))
        assert opp.roi == pytest.approx(0.01, abs=0.001)
        assert not qualifies(opp)

    def test_exactly_two_and_a_half_percent_qualifies(self) -> None:
        # Solve 1/s - 1 = 0.025 → s = 1/1.025; each leg odds = 2/s
        odds = 2.0 / (1.0 / 1.025)
        opp = _opportunity(roi_prices=(odds, odds))
        assert opp.roi == pytest.approx(0.025, abs=1e-9)
        assert qualifies(opp, min_roi=0.025)

    def test_negative_guaranteed_profit_is_refused(self) -> None:
        opp = _opportunity()
        bad = Opportunity(
            event_key=opp.event_key,
            sport=opp.sport,
            home_team=opp.home_team,
            away_team=opp.away_team,
            commence_time=opp.commence_time,
            market=opp.market,
            period=opp.period,
            side=None,
            line=None,
            legs=opp.legs,
            total_stake=opp.total_stake,
            outcome_profits=(("home", -1.0), ("away", 5.0)),
            max_total_stake=None,
        )
        assert not qualifies(bad)


class TestFormat:
    def test_message_has_books_stakes_and_american_odds(self) -> None:
        text = format_alert(_opportunity())
        assert "ARB" in text
        assert "fanduel" in text
        assert "pinnacle" in text
        assert "stake $" in text
        assert "Philadelphia Phillies" in text
        assert "Miami Marlins" in text
        assert "PLACE BOTH NOW" in text
        assert "moneyline" in text


class TestDedupeAndSend:
    def test_same_opportunity_is_texted_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[str] = []
        book = AlertBook(send=lambda body: sent.append(body) or "SMid")
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "ACxxxx")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "token")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "+15551234567")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+18479070871")
        assert alert_ready()
        opp = _opportunity()
        assert book.notify([opp]) == [opp]
        assert book.notify([opp]) == []
        assert len(sent) == 1
        assert opportunity_alert_key(opp) in book.sent_keys

    def test_send_sms_posts_twilio_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "ACtest")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "secret")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "+15550001111")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+18479070871")

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode()
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(201, json={"sid": "SMabc"})

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            sid = send_sms("hello arb", client=client)
        assert sid == "SMabc"
        assert "ACtest" in captured["url"]
        assert "To=%2B18479070871" in captured["body"] or "To=+18479070871" in captured["body"]
        assert "Body=hello+arb" in captured["body"] or "Body=hello%20arb" in captured["body"]

    def test_missing_credentials_skips_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "")
        book = AlertBook(send=lambda body: (_ for _ in ()).throw(AssertionError("should not send")))
        assert book.notify([_opportunity()]) == []
