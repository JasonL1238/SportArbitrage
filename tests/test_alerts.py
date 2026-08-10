"""SMS alert formatting, ROI gate, and fail-soft send path."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

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


def _opportunity(
    *,
    roi_prices=(2.2, 2.2),
    stake=100.0,
    sources=("fanduel", "pinnacle"),
    teams=("Miami Marlins", "Philadelphia Phillies"),
    commence_time: datetime | None = None,
) -> Opportunity:
    """Synthetic two-way MLB moneyline sized so arithmetic is obvious.

    Equal prices of 2.2 → sum implied 1/2.2 * 2 ≈ 0.909 → ROI ≈ 10%.

    The default kickoff is a FIXED past instant, deliberately: the format
    tests pin the rendered "Kickoff 2026-07-28 …" line byte-for-byte.
    ``AlertBook.notify`` refuses a started fixture (its one clock), so
    every test that expects a text to actually go out must pass a future
    ``commence_time`` — a send expectation on the default is asserting the
    settled-game-texting bug the clock exists to prevent.
    """
    home = make_quote(
        source=sources[0],
        selection=Selection.HOME,
        decimal_odds=roi_prices[0],
        home_team=teams[0],
        away_team=teams[1],
    )
    away = make_quote(
        source=sources[1],
        selection=Selection.AWAY,
        decimal_odds=roi_prices[1],
        source_event_id="evt-2",
        source_market_id="mkt-2",
        home_team=teams[0],
        away_team=teams[1],
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
        home_team=teams[0],
        away_team=teams[1],
        commence_time=(
            datetime(2026, 7, 28, 22, 41, tzinfo=UTC)
            if commence_time is None
            else commence_time
        ),
        market=Market.MONEYLINE,
        period=Period.FULL_GAME,
        side=None,
        line=None,
        legs=legs,
        total_stake=stake,
        outcome_profits=(("home", profit), ("away", profit)),
        max_total_stake=None,
    )


def _pregame(**overrides) -> Opportunity:
    """An opportunity whose kickoff is still ahead — the only kind
    ``AlertBook.notify`` may text.  Every test expecting a send (or
    exercising the ledger through real sends) builds with this; using the
    helper's fixed past default there would test nothing, because the
    notify clock suppresses it before the ledger is ever consulted."""
    return _opportunity(
        commence_time=datetime.now(UTC) + timedelta(hours=4), **overrides
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


class TestTheClockOnTheAlertPath:
    """``notify`` owns the one wall-clock judgement on the alert path.

    ``arb --run N`` reads an old run as history (the MAX_PRICE_AGE gate is
    deliberately bypassed) and prints "a historical study, not positions
    anyone can take" — then handed the same opportunities to ``notify``,
    which texted "PLACE BOTH NOW — prices move" about games settled days
    earlier.  The clock lives in ``notify`` itself so no caller can forget
    it, and the CLI additionally refuses to alert at all under
    ``--include-started``.
    """

    def _ready_book(self, monkeypatch: pytest.MonkeyPatch):
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "twilio")
        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "ACxxxx")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "token")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "+15551234567")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+15550000000")
        sent: list[str] = []
        return AlertBook(send=lambda body: sent.append(body) or "SM"), sent

    def test_a_started_fixture_is_never_texted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book, sent = self._ready_book(monkeypatch)
        opp = _opportunity()  # the helper's fixed 2026-07-28 kickoff — long past
        assert book.notify([opp]) == []
        assert sent == []
        # Not even claimed: a suppressed study must not spend the arb's one
        # ledger slot, or the same position live later would be swallowed.
        assert opportunity_alert_key(opp) not in book.sent_keys

    def test_the_same_position_pregame_still_texts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        book, sent = self._ready_book(monkeypatch)
        assert len(book.notify([_pregame()])) == 1
        assert len(sent) == 1


def test_the_alert_key_ignores_stakes() -> None:
    """Same books, selections and prices → one key, whatever the bankroll.

    The key embedded ``{stake:.2f}`` per leg while its docstring promised
    price-identity, so ``arb --run N --stake 200`` re-minted every claimed
    position under a fresh key and the ledger waved the second text
    through — "at most one text per arb ever" quietly became "per arb per
    bankroll".
    """
    assert opportunity_alert_key(_opportunity(stake=100.0)) == opportunity_alert_key(
        _opportunity(stake=200.0)
    )


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


class TestLocalityDisclaimer:
    """A text naming a book the reader cannot reach must say so itself.

    The dashboard and the CLI carry per-leg labels; the SMS is the surface with
    no second look, so the disclaimer rides in the message — and in the *head*,
    because the body truncates from the end and a warning that can be cut off
    is not a warning.
    """

    def _pa_marking(self):
        from src.coverage import locality_marking

        return locality_marking("PA", route_scope="state")

    def test_a_mixed_position_names_its_foreign_leg(self) -> None:
        text = format_alert(_opportunity(), marking=self._pa_marking())
        assert "NON-LOCAL: pinnacle not reachable from PA — not PA prices" in text
        legs = [line for line in text.splitlines() if line.startswith(("1)", "2)"))]
        fanduel_line = next(line for line in legs if "fanduel" in line)
        pinnacle_line = next(line for line in legs if "pinnacle" in line)
        assert "[not reachable from PA]" in pinnacle_line
        assert "not reachable" not in fanduel_line

    def test_a_wholly_foreign_position_says_no_leg(self) -> None:
        text = format_alert(
            _opportunity(sources=("pinnacle", "bovada")), marking=self._pa_marking()
        )
        assert "NO LEG reachable from PA — informational, not PA prices" in text
        assert "NON-LOCAL:" not in text
        assert text.count("[not reachable from PA]") == 2
        # The call to action must not contradict the head: a message that says
        # "informational" cannot end by instructing the reader to place legs.
        assert "PLACE BOTH NOW" not in text
        assert "INFO ONLY — not takeable from PA" in text

    def test_a_mixed_position_keeps_the_call_to_action(self) -> None:
        """One reachable leg is a takeable position; the tag does the warning."""
        text = format_alert(_opportunity(), marking=self._pa_marking())
        assert "PLACE BOTH NOW" in text
        assert "INFO ONLY" not in text

    def test_a_pa_text_links_pennsylvanias_own_betrivers(self) -> None:
        """The link is the point of the text, so it must be this state's.

        Run 32's two real opportunities each texted ``il.betrivers.com`` under
        "PLACE BOTH NOW" while their prices came from ``rsiuspa``/``US-PA`` —
        another licence's site, which does not show this state's slip.  The
        governed marking now rides into ``bet_link``, and this pins the whole
        path: marking → link → the SMS body a reader actually taps.
        """
        text = format_alert(
            _opportunity(sources=("fanduel", "betrivers_kambi")),
            marking=self._pa_marking(),
        )
        assert "https://pa.betrivers.com" in text
        assert "il.betrivers.com" not in text

    def test_an_ungoverned_text_names_no_state_it_cannot_know(self) -> None:
        text = format_alert(_opportunity(sources=("fanduel", "betrivers_kambi")))
        assert "https://www.betrivers.com" in text
        assert "il.betrivers.com" not in text
        assert "pa.betrivers.com" not in text

    def test_a_fully_local_position_carries_no_disclaimer(self) -> None:
        """A governed marking must stay silent when there is nothing to mark.

        Every other test in this class has at least one foreign leg, so without
        this one the disclaimer branch could fire unconditionally — an empty
        "NON-LOCAL:" line on every clean PA text — and stay green.
        """
        text = format_alert(
            _opportunity(sources=("fanduel", "draftkings")),
            marking=self._pa_marking(),
        )
        assert "NON-LOCAL" not in text
        assert "NO LEG" not in text
        assert "not reachable" not in text
        assert "PLACE BOTH NOW" in text

    def test_the_marking_normalizes_its_state_itself(self) -> None:
        """A lowercase or padded jurisdiction string must not defeat the rule."""
        from src.coverage import locality_marking

        marking = locality_marking(" pa ", route_scope="state")
        assert marking.marking
        assert marking.label() == "not reachable from PA"
        assert not marking.leg_is_local("pinnacle")
        assert marking.leg_is_local("fanduel")

    def test_no_marking_and_an_ungoverned_marking_change_nothing(self) -> None:
        from src.coverage import locality_marking

        bare = format_alert(_opportunity())
        assert format_alert(_opportunity(), marking=None) == bare
        widened = locality_marking("PA", route_scope="global")
        assert format_alert(_opportunity(), marking=widened) == bare
        assert "not reachable" not in bare

    def test_truncation_cannot_cut_the_disclaimer(self) -> None:
        from src.alerts import _MAX_SMS_CHARS

        long = "Somerset Patriots of Greater Bridgewater Township " * 20
        text = format_alert(
            _opportunity(sources=("pinnacle", "bovada"), teams=(long, long)),
            marking=self._pa_marking(),
        )
        assert len(text) <= _MAX_SMS_CHARS
        assert text.endswith("…")
        assert "NO LEG reachable from PA — informational, not PA prices" in text


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
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+15550000000")
        assert alert_ready()
        opp = _pregame()
        assert book.notify([opp]) == [opp]
        assert book.notify([opp]) == []
        assert len(sent) == 1
        assert opportunity_alert_key(opp) in book.sent_keys

    def _ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "twilio")
        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "ACxxxx")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "token")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "+15551234567")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+15550000000")
        assert alert_ready()

    def test_pollute_the_default_book_for_the_pin_below(self) -> None:
        """Deliberate contamination, undone only by the fixture's reset.

        With the conftest ``sent_keys`` reset in place, this key lives in a
        per-test temporary set the fixture swaps out at teardown; with the
        reset deleted, it lands in the import-time set and *persists* into the
        next test — where the pin below reads a non-empty book and fails.
        Without this probe the pin's empty-set assertion passed vacuously
        inside this file, because every other test here builds its own book.
        """
        import src.alerts as alerts_mod

        alerts_mod.DEFAULT_BOOK.sent_keys.add("pollution-probe")
        assert "pollution-probe" in alerts_mod.DEFAULT_BOOK.sent_keys

    def test_the_suite_never_holds_a_live_transport(self) -> None:
        """The suite was texting the operator on every full run.

        Five CLI tests drive ``main(["arb"])`` over a genuine 4.76% synthetic
        arb; ``alert_ready()`` is True by default on a signed-in Mac, so
        ``DEFAULT_BOOK.send`` reached osascript with the operator's real
        number — unnoticed because ``notify`` is fail-soft and the tests pass
        either way.  The hermetic conftest fixture must therefore replace the
        send callable with a refusal, and this test is what makes removing
        that patch a test failure rather than a text message.

        The identity check comes first so that, if the patch is ever gone,
        this test fails *before* calling anything that could deliver.

        All three of the fixture's patch lines are held, not only the send
        callable: with the ``path`` repoint deleted the suite stayed green
        while claiming keys in ``data/alerts.sqlite3`` — the operator's *real*
        dedupe ledger, where a claimed key is a permanently suppressed future
        alert — because ``DEFAULT_BOOK`` binds the real path at import, before
        any fixture runs, and ``notify`` is fail-soft.
        """
        import src.alerts as alerts_mod
        import src.settings as settings_mod

        assert alerts_mod.DEFAULT_BOOK.path == settings_mod.ALERT_BOOK_PATH, (
            "conftest must repoint DEFAULT_BOOK.path at the hermetic ledger"
        )
        assert "alerts-hermetic" in str(alerts_mod.DEFAULT_BOOK.path), (
            "the patched path must be the fixture's tmp ledger, not the real one"
        )
        assert alerts_mod.DEFAULT_BOOK.sent_keys == set(), (
            "conftest must clear the in-memory half per test"
        )
        assert alerts_mod.DEFAULT_BOOK.send is not alerts_mod.send_alert, (
            "conftest must replace DEFAULT_BOOK.send for the whole suite"
        )
        with pytest.raises(AssertionError):
            alerts_mod.DEFAULT_BOOK.send("probe body — must never deliver")

    def test_once_means_once_across_processes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The ledger is the fix for a text that actually reached a phone.

        The in-memory book starts empty in every process, so ``arb --run N``
        re-texted a stored run's stale opportunities on each invocation — an
        offline review probe of run 32 delivered a real 3.3% tennis arb SMS
        that the collect pass had already had the chance to send.  Two books
        sharing one ledger path stand in for two processes here.
        """
        self._ready(monkeypatch)
        ledger = tmp_path / "alerts.sqlite3"
        first_sent: list[str] = []
        second_sent: list[str] = []
        first = AlertBook(send=lambda body: first_sent.append(body) or "S1", path=ledger)
        second = AlertBook(send=lambda body: second_sent.append(body) or "S2", path=ledger)

        opp = _pregame()
        assert first.notify([opp]) == [opp]
        assert second.notify([opp]) == [], (
            "a fresh process must see the ledger, not start from nothing"
        )
        assert first_sent and not second_sent

    def test_a_failed_send_stays_claimed_for_the_next_process_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Claim-before-send holds at the ledger, not only in memory.

        The trade the in-memory book already made — a genuinely failed send is
        not retried, because a send that fails after delivering would
        double-text — must survive the process boundary, or the failure mode
        returns through a restart.
        """
        self._ready(monkeypatch)
        ledger = tmp_path / "alerts.sqlite3"

        def _explode(body: str) -> str:
            raise RuntimeError("carrier down")

        crashed = AlertBook(send=_explode, path=ledger)
        opp = _pregame()
        assert crashed.notify([opp]) == []

        revived_sent: list[str] = []
        revived = AlertBook(send=lambda body: revived_sent.append(body) or "S", path=ledger)
        assert revived.notify([opp]) == []
        assert not revived_sent

    def test_an_unwritable_ledger_degrades_to_in_process_dedupe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """A broken disk must cost durability, never the alert or the loop."""
        self._ready(monkeypatch)
        blocked = tmp_path / "not-a-dir-parent"
        blocked.write_text("a file where the ledger's parent dir should be")
        sent: list[str] = []
        book = AlertBook(
            send=lambda body: sent.append(body) or "S",
            path=blocked / "alerts.sqlite3",
        )
        opp = _pregame()
        assert book.notify([opp]) == [opp], "the text still goes out"
        assert book.notify([opp]) == [], "and the in-process half still dedupes"
        assert len(sent) == 1

    def test_send_sms_posts_twilio_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.settings as settings_mod

        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "ACtest")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "secret")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "+15550001111")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+15550000000")

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
        assert "To=%2B15550000000" in captured["body"] or "To=+15550000000" in captured["body"]
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
        opp = _pregame()
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
        opp = _pregame()
        book.notify([opp])
        book.notify([opp])
        book.notify([opp])
        assert len(calls) == 1

    def test_the_batch_lets_the_state_pass_send_the_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The one text a batch sends must be the labelled one.

        The dedupe above and the locality disclaimer compose badly: the GLOBAL
        pass sees a cross-global arb minutes before the state passes do, its
        marking is ungoverned by design, and the alert key holds no
        jurisdiction — so letting it text first sends the bare body and the
        state pass's disclaimed text is then swallowed as a duplicate.  The
        batch therefore alerts from the state passes, and from the GLOBAL pass
        only when there is no state pass at all.
        """
        from src import collector
        from src.raw_store import RawStore

        alerts_seen: list[tuple[str, bool]] = []

        class _Result:
            ok = True
            quotes: list = []

        def collect(sources, **kwargs):
            alerts_seen.append((kwargs["jurisdiction"], kwargs["alert"]))
            return _Result()

        def build(keys=None, *, state=None, route_scope="all", **kwargs):
            class _Fake:
                def __init__(self, key): self.source_key = key; self.leagues = ("MLB",)
                def close(self): pass
            return [_Fake("pinnacle" if route_scope == "global" else f"retail-{state}")]

        monkeypatch.setattr(collector, "build_sources", build)
        monkeypatch.setattr(collector, "collect_once", collect)
        collector.collect_batch_once(
            ("IL", "PA"), detected_state="IL",
            raw_store=RawStore(tmp_path / "raw"), store=None,
        )
        assert alerts_seen == [("GLOBAL", False), ("IL", True), ("PA", True)]

        alerts_seen.clear()
        collector.collect_batch_once(
            (), detected_state="IL",
            raw_store=RawStore(tmp_path / "raw"), store=None,
        )
        assert alerts_seen == [("GLOBAL", True)], (
            "with no state pass the GLOBAL pass is the only alerting surface "
            "left, and silencing it too would drop the alert entirely"
        )

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
