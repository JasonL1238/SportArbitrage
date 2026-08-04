"""SMS alert formatting, ROI gate, and fail-soft send path."""
from __future__ import annotations

import os
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

        # Pinned to twilio on purpose: readiness is transport-specific, and a
        # test that leaves the transport at its default asserts whatever the
        # machine happens to be rather than what it set up.
        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "twilio")
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

        # Transport pinned, and the sender asserts rather than returning: without
        # the pin this passed on a Mac for the wrong reason — the messages
        # transport reported ready, the sender raised, and ``notify``'s fail-soft
        # ``except`` swallowed it into the same empty list a skip produces.
        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "twilio")
        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "")
        tried: list[str] = []

        def _explode(body: str) -> str:
            tried.append(body)
            raise AssertionError("should not send")

        book = AlertBook(send=_explode)
        assert book.notify([_opportunity()]) == []
        assert tried == [], "the sender was called even though nothing was configured"


class TestTheThreePercentFloor:
    """``ALERT_MIN_ROI`` is read as ROI on staked bankroll, not market margin.

    Pinned at the boundary because "3%" is the number the user asked for and the
    two readings differ: a two-way pair at 2.06/2.06 is a 2.91% margin and a
    3.00% ROI, so a gate written against the wrong one is silently off by a
    third of a percent — in the direction that withholds the text.
    """

    def test_exactly_the_floor_qualifies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "ALERT_MIN_ROI", 0.03)
        opp = _opportunity(roi_prices=(2.06, 2.06))
        assert abs(opp.roi - 0.03) < 5e-4, opp.roi
        assert qualifies(opp)

    def test_just_under_the_floor_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "ALERT_MIN_ROI", 0.03)
        opp = _opportunity(roi_prices=(2.04, 2.04))
        assert opp.roi < 0.03
        assert not qualifies(opp)

    def test_the_shipped_default_is_three_percent(self) -> None:
        import src.settings as settings_mod

        assert settings_mod.ALERT_MIN_ROI == pytest.approx(0.03)


class TestLinksAreInTheText:
    """A 3% edge is only takeable if both slips are one tap away."""

    def test_a_confirmed_grammar_puts_the_event_url_in_the_body(self) -> None:
        from src.arb import ArbLeg, Opportunity
        from src.schema import Market, Period, Selection, Sport

        home = make_quote(
            source="draftkings", selection=Selection.HOME, decimal_odds=2.2,
            source_event_id="34475007", league="MLB",
        )
        away = make_quote(
            source="smarkets", selection=Selection.AWAY, decimal_odds=2.2,
            source_event_id="45241102", league="MLB", source_market_id="m2",
        )
        opp = Opportunity(
            event_key=home.event_key, sport=Sport.BASEBALL,
            home_team=home.home_team, away_team=home.away_team,
            commence_time=home.commence_time, market=Market.MONEYLINE,
            period=Period.FULL_GAME, side=None, line=None,
            legs=(ArbLeg(quote=home, stake=50.0), ArbLeg(quote=away, stake=50.0)),
            total_stake=100.0, outcome_profits=(("home", 10.0), ("away", 10.0)),
            max_total_stake=None,
        )
        text = format_alert(opp)
        assert "https://sportsbook.draftkings.com/event/34475007" in text
        assert "https://smarkets.com/event/45241102" in text

    def test_a_league_page_says_so_rather_than_posing_as_the_bet(self) -> None:
        text = format_alert(_opportunity())
        assert "league page" in text
        # The default fixture is fanduel/pinnacle, neither of which has a
        # confirmed grammar, so no bare event URL may appear.
        assert "/event/" not in text

    def test_a_mirrored_leg_names_the_book_and_the_feed(self) -> None:
        from src.arb import ArbLeg, Opportunity
        from src.schema import Market, Period, Selection, Sport

        home = make_quote(
            source="an_fanduel", selection=Selection.HOME, decimal_odds=2.2,
            source_event_id="292342", league="MLB",
        )
        away = make_quote(
            source="draftkings", selection=Selection.AWAY, decimal_odds=2.2,
            source_event_id="34475007", league="MLB", source_market_id="m2",
        )
        opp = Opportunity(
            event_key=home.event_key, sport=Sport.BASEBALL,
            home_team=home.home_team, away_team=home.away_team,
            commence_time=home.commence_time, market=Market.MONEYLINE,
            period=Period.FULL_GAME, side=None, line=None,
            legs=(ArbLeg(quote=home, stake=50.0), ArbLeg(quote=away, stake=50.0)),
            total_stake=100.0, outcome_profits=(("home", 10.0), ("away", 10.0)),
            max_total_stake=None,
        )
        text = format_alert(opp)
        assert "price via an_fanduel" in text
        assert "292342" not in text, "an aggregator's id must never reach a book URL"


class TestTheMessagesTransport:
    def test_the_body_travels_as_argv_never_as_script_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AppleScript injection: a quote in a team name must stay data.

        Promo copy and book-supplied names reach this string.  Interpolating them
        into the script would let a ``"`` close the literal and run whatever
        followed, with the user's Messages account as the payload.
        """
        import src.alerts as alerts_mod

        hostile = 'Cubs" \n tell application "Finder" to delete every file \n "'
        seen: dict = {}

        class _Done:
            returncode = 0
            stdout = "imessage"
            stderr = ""

        def _fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["input"] = kwargs.get("input")
            return _Done()

        monkeypatch.setattr(alerts_mod.subprocess, "run", _fake_run)
        monkeypatch.setattr(alerts_mod.sys, "platform", "darwin")
        alerts_mod.send_via_messages(hostile, to="+15550001111")

        assert seen["cmd"][:2] == ["osascript", "-"]
        assert seen["cmd"][2] == "+15550001111"
        assert seen["cmd"][3] == hostile, "body must be passed verbatim as argv"
        assert hostile not in (seen["input"] or ""), "body leaked into script text"
        assert "Finder" not in (seen["input"] or "")

    def test_a_denied_automation_prompt_says_what_to_do(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.alerts as alerts_mod

        class _Denied:
            returncode = 1
            stdout = ""
            stderr = "execution error: Not authorised to send Apple events (-1743)."

        monkeypatch.setattr(alerts_mod.subprocess, "run", lambda cmd, **kw: _Denied())
        monkeypatch.setattr(alerts_mod.sys, "platform", "darwin")
        with pytest.raises(RuntimeError, match="Privacy & Security"):
            alerts_mod.send_via_messages("hi", to="+15550001111")

    def test_off_mac_it_names_the_alternative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.alerts as alerts_mod

        monkeypatch.setattr(alerts_mod.sys, "platform", "linux")
        with pytest.raises(RuntimeError, match="ODDS_ALERT_TRANSPORT=twilio"):
            alerts_mod.send_via_messages("hi", to="+15550001111")
        assert not alerts_mod.messages_ready()
        assert "macOS" in alerts_mod.unready_reason()

    def test_send_alert_dispatches_on_the_configured_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.alerts as alerts_mod
        import src.settings as settings_mod

        routed: list[str] = []
        monkeypatch.setattr(
            alerts_mod, "send_via_messages",
            lambda body, to=None: routed.append("messages") or "imessage",
        )
        monkeypatch.setattr(
            alerts_mod, "send_sms",
            lambda body, to=None: routed.append("twilio") or "SMid",
        )
        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "messages")
        alerts_mod.send_alert("body")
        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "twilio")
        alerts_mod.send_alert("body")
        assert routed == ["messages", "twilio"]

    def test_an_unknown_transport_is_refused_by_name(self) -> None:
        """A typo must reach the entry point's refusal, not silence the alerts."""
        import importlib

        import src.settings as settings_mod

        module = importlib.reload(settings_mod)
        try:
            os.environ["ODDS_ALERT_TRANSPORT"] = "carrier-pigeon"
            module = importlib.reload(settings_mod)
            assert any("ALERT_TRANSPORT" in line for line in module.BAD_SETTINGS)
            assert module.ALERT_TRANSPORT in module.ALERT_TRANSPORTS
        finally:
            os.environ.pop("ODDS_ALERT_TRANSPORT", None)
            importlib.reload(settings_mod)


class TestOneArbIsOneText:
    """The duplicate-text bug, from both ends.

    ``collect_batch_once`` runs a GLOBAL pass and then one pass per state over
    the *same cached* global payloads, so an arb between two global books is
    handed to the alert book once per pass with an identical key. Recording the
    key only after a successful send left it unclaimed whenever a send raised —
    and a Messages send can raise *after* delivering — so the next pass sent the
    same text again.
    """

    def _book(self, *, fail_first: bool):
        calls: list[str] = []

        def _send(body: str) -> str:
            calls.append(body)
            if fail_first and len(calls) == 1:
                raise RuntimeError("delivered, then the bridge reported failure")
            return "sent"

        return AlertBook(send=_send), calls

    def test_a_failed_send_is_not_retried_into_a_second_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "messages")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+15550001111")
        monkeypatch.setattr("src.alerts.messages_ready", lambda: True)
        book, calls = self._book(fail_first=True)
        opp = _opportunity()
        # Pass one: the GLOBAL run. Pass two: the state run, same cached prices.
        assert book.notify([opp]) == []
        assert book.notify([opp]) == []
        assert len(calls) == 1, f"the same arb was sent {len(calls)} times"

    def test_two_batch_passes_over_the_same_arb_send_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "messages")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+15550001111")
        monkeypatch.setattr("src.alerts.messages_ready", lambda: True)
        book, calls = self._book(fail_first=False)
        opp = _opportunity()
        book.notify([opp])
        book.notify([opp])
        book.notify([opp])
        assert len(calls) == 1

    def test_the_applescript_sends_exactly_once(self) -> None:
        """Structural pin: the retry loop that double-delivered cannot return.

        Counted in the script text because the only other way to observe it is
        to actually send, and a test that sends two real texts to prove it sends
        one is not a test anyone can run.
        """
        import re

        from src.alerts import _MESSAGES_SCRIPT

        sends = re.findall(r"^\s*send\s+msg\s+to\b", _MESSAGES_SCRIPT, re.M)
        assert len(sends) == 1, f"{len(sends)} send statements in the AppleScript"
        assert "repeat" not in _MESSAGES_SCRIPT, (
            "a repeat over services re-sends through the service already tried"
        )
