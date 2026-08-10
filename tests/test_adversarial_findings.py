"""Regressions for defects an adversarial review found after the ten-source expansion.

Every case here is a bug that shipped, passed the whole suite, and would have
reported money that does not exist or hidden a fault that does.  They are grouped
in one file because they share a cause worth naming: **each was invisible to
every check that existed**, and each was found by asking what a specific wrong
answer would look like rather than by asserting that the right one still worked.

Three of them are the same defect in three places — a venue's own ``HOME``/
``AWAY``/outcome-one label read as this pipeline's orientation. In tennis there is
no home player, so :func:`src.events.orient` imposes an ordering by participant
key and the venue's own ordering is unrelated. The prices then land on the wrong
player, and nothing downstream can see it: the books agree on the participants,
each book's own overround is normal, and validation deliberately does not compare
an orientation that does not exist. What comes out is a position whose two legs
back the same competitor, reported as a guarantee.
"""
from __future__ import annotations

import dataclasses
import glob
import pathlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.arb import best_prices, find_opportunities
from src.commission import WinningsCommission, net_decimal_odds
from src.raw_store import RawResponse
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Side, Sport
from src.sources.kalshi import _ticker_start, parse_kalshi
from src.sources.smarkets import parse_smarkets
from src.sources.sxbet import parse_sxbet
from tests.conftest import make_quote

FETCHED = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
LATER = FETCHED + timedelta(hours=6)

#: Alcaraz sorts before Zverev by participant key, so ``orient`` makes Zverev the
#: "home" side — the opposite of the order both venues name them in.
FAVOURITE, UNDERDOG = "Carlos Alcaraz", "Alexander Zverev"


def _raw(source: str, endpoint: str, payload) -> RawResponse:
    return RawResponse(
        source=source,
        endpoint=endpoint,
        url="https://example.invalid/x",
        status_code=200,
        body=json.dumps(payload),
        fetched_at=FETCHED,
        content_type="application/json",
    )


def _priced(outcome) -> dict[str, float]:
    """``{competitor display name: decimal odds}`` for a parsed moneyline."""
    return {
        (q.home_team if q.selection is Selection.HOME else q.away_team): round(
            q.decimal_odds, 3
        )
        for q in outcome.quotes
        if q.market is Market.MONEYLINE
    }


class TestVenueOrientationIsTranslated:
    """A venue's own side label is *its* ordering, not ours."""

    def _sxbet(self, one: str, two: str, *, sport_id: int, league: str):
        usdc = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
        # SX's ``type`` is per sport: 52 is the tennis match winner, 226 the
        # moneyline in the US sports.  The same number means different contracts
        # in different sports, which is why the table is keyed by both.
        market = {
            "status": "ACTIVE", "marketHash": "0xabc",
            "type": 52 if sport_id == 6 else 226,
            "outcomeOneName": one, "outcomeTwoName": two,
            "teamOneName": one, "teamTwoName": two,
            "gameTime": int(LATER.timestamp()), "sportXeventId": "L1",
            "sportLabel": league, "sportId": sport_id, "leagueLabel": league,
            "chainVersion": "SXR", "isQuarterLineMarket": False,
        }

        def order(maker_implied: float, maker_on_one: bool) -> dict:
            return {
                "marketHash": "0xabc",
                "percentageOdds": str(int(maker_implied * 10**20)),
                "totalBetSize": "100000000", "fillAmount": "0",
                "pendingFillAmount": "0", "baseToken": usdc,
                "isMakerBettingOutcomeOne": maker_on_one, "orderStatus": "ACTIVE",
            }

        # Outcome one at 1.25 (taker implied 0.80); outcome two at 4.444.
        return parse_sxbet([
            _raw("sxbet", "markets:x:01", {"data": {"markets": [market]}}),
            _raw("sxbet", "orders:x:01", {"data": [order(0.20, False), order(0.775, True)]}),
        ])

    def test_sxbet_attaches_outcome_one_to_sxs_own_first_competitor(self) -> None:
        """SX names ``teamOneName`` first; for tennis that is not our home side.

        Hard-coding outcome one to ``HOME`` published the 4.44 underdog at 1.25
        on about half of all tennis matches, and paired against a correctly
        oriented book it produced a 2.5% "guaranteed" position whose two legs
        both backed the underdog.
        """
        priced = self._priced_tennis(self._sxbet(FAVOURITE, UNDERDOG, sport_id=6, league="ATP Toronto"))
        assert priced[FAVOURITE] == 1.25 and priced[UNDERDOG] == 4.444

    def test_sxbet_still_reads_a_team_sport_the_same_way(self) -> None:
        """``teamOneName`` *is* the home side where there is one — 16 of 16 NFL
        fixtures agreed with Bovada's explicit flag — so the translation must be
        a no-op there rather than an inversion."""
        outcome = self._sxbet(
            "Los Angeles Rams", "San Francisco 49ers", sport_id=8, league="NFL"
        )
        home = next(q for q in outcome.quotes if q.selection is Selection.HOME)
        assert home.home_team == "Los Angeles Rams"
        assert round(home.decimal_odds, 3) == 1.25

    def _smarkets(self, name: str, slug: str, home_contract: str, away_contract: str):
        event = {
            "id": "1", "name": name, "state": "upcoming", "bettable": True,
            "start_datetime": LATER.strftime("%Y-%m-%dT%H:%M:%SZ"), "full_slug": slug,
        }
        market = {
            "id": "10", "event_id": "1", "state": "open",
            "market_type": {"name": "WINNER_2_WAY"}, "name": "Match winner",
        }
        contracts = [
            {"id": "100", "market_id": "10", "contract_type": {"name": "HOME"},
             "name": home_contract},
            {"id": "101", "market_id": "10", "contract_type": {"name": "AWAY"},
             "name": away_contract},
        ]
        # The HOME contract is the 1.25 favourite: an offer at 8000 = 80%.
        return parse_smarkets([
            _raw("smarkets", "events:x", {"events": [event]}),
            _raw("smarkets", "markets:x:01", {"markets": [market]}),
            _raw("smarkets", "contracts:x:01", {"contracts": contracts}),
            _raw("smarkets", "quotes:x:01", {
                "100": {"offers": [{"price": 8000, "quantity": 1}]},
                "101": {"offers": [{"price": 2250, "quantity": 1}]},
            }),
        ])

    def test_smarkets_attaches_its_home_contract_to_its_own_first_competitor(self) -> None:
        priced = self._priced_tennis(self._smarkets(
            f"{FAVOURITE} vs {UNDERDOG}",
            "/sport/tennis/atp-toronto/2026/07/28/18-00/alcaraz-zverev",
            FAVOURITE, UNDERDOG,
        ))
        assert priced[FAVOURITE] == 1.25 and priced[UNDERDOG] == 4.444

    def test_smarkets_still_reads_a_team_sport_the_same_way(self) -> None:
        outcome = self._smarkets(
            "Philadelphia Phillies at Miami Marlins",
            "/sport/baseball/mlb/2026/07/28/22-41/phillies-marlins",
            "Miami Marlins", "Philadelphia Phillies",
        )
        home = next(q for q in outcome.quotes if q.selection is Selection.HOME)
        assert home.home_team == "Miami Marlins"
        assert round(home.decimal_odds, 3) == 1.25

    @staticmethod
    def _priced_tennis(outcome) -> dict[str, float]:
        assert outcome.rejections == [], outcome.rejections
        priced = _priced(outcome)
        assert set(priced) == {FAVOURITE, UNDERDOG}, priced
        return priced


class TestSmarketsWinnerTypesAreDifferentContracts:
    def test_a_two_way_soccer_winner_is_draw_no_bet_and_is_not_collected(self) -> None:
        """Mapping both winner types onto one ``(MONEYLINE, FULL_GAME)`` made them
        collide on ``dedup_key``, and which survived was decided by a string sort
        over market ids.  In hockey that files a *regulation* price as a full-game
        one, and an overtime win then loses a leg reported as covered."""
        event = {
            "id": "2", "name": "Arsenal at Chelsea", "state": "upcoming", "bettable": True,
            "start_datetime": LATER.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "full_slug": "/sport/football/premier-league/2026/07/28/18-00/arsenal-chelsea",
        }
        market = {
            "id": "20", "event_id": "2", "state": "open",
            "market_type": {"name": "WINNER_2_WAY"}, "name": "Draw no bet",
        }
        contracts = [
            {"id": "200", "market_id": "20", "contract_type": {"name": "HOME"}, "name": "Chelsea"},
            {"id": "201", "market_id": "20", "contract_type": {"name": "AWAY"}, "name": "Arsenal"},
        ]
        outcome = parse_smarkets([
            _raw("smarkets", "events:soccer", {"events": [event]}),
            _raw("smarkets", "markets:soccer:01", {"markets": [market]}),
            _raw("smarkets", "contracts:soccer:01", {"contracts": contracts}),
            _raw("smarkets", "quotes:soccer:01", {
                "200": {"offers": [{"price": 5000, "quantity": 1}]},
                "201": {"offers": [{"price": 5200, "quantity": 1}]},
            }),
        ])
        assert outcome.quotes == []
        assert outcome.skipped["draw_voids_market:soccer:WINNER_2_WAY"] == 1


class TestKalshiStartTime:
    def test_the_start_comes_from_the_ticker_not_the_expiry(self) -> None:
        """``occurrence_datetime`` is the expected *end*.

        On all 354 captured markets it equalled ``expected_expiration_time`` and
        sat exactly three hours — a baseball game — after the ticker's own time,
        which ``rules_primary`` states as the scheduled start.  Used as the start
        it put every Kalshi fixture three hours from every other book's, outside
        MLB's clustering tolerance, so Kalshi joined *nothing*; on a doubleheader
        it landed game one inside everyone else's game two.
        """
        # 22:10 US Eastern on 2026-07-30.
        assert _ticker_start("26JUL302210SEALAD") == datetime(
            2026, 7, 31, 2, 10, tzinfo=UTC
        )
        # 19:45 ET, which rules_primary states as "7:45 PM EDT".
        assert _ticker_start("26JUL281945CHCSTL") == datetime(
            2026, 7, 28, 23, 45, tzinfo=UTC
        )

    def test_a_ticker_without_a_time_places_no_fixture(self) -> None:
        """A date alone cannot be set against another book's clock, so it is
        refused rather than defaulted to midnight."""
        assert _ticker_start("26JUL30NYLV") is None
        assert _ticker_start("nonsense") is None

    def test_an_expiry_that_contradicts_the_ticker_is_refused(self) -> None:
        """The cross-check: the two should differ by about a game's length.  If
        they stop being consistent the convention has changed, and filing a
        fixture at a time nobody else agrees on is how it joins the wrong game."""
        market = {
            "ticker": "KXMLBGAME-26JUL281945CHCSTL-CHC",
            "event_ticker": "KXMLBGAME-26JUL281945CHCSTL",
            "status": "active",
            "occurrence_datetime": "2026-08-05T02:45:00Z",  # a week later
            "yes_ask_dollars": "0.5000",
        }
        outcome = parse_kalshi([_raw("kalshi", "markets:KXMLBGAME:01", {"markets": [market]})])
        assert outcome.quotes == []
        assert [r.reason for r in outcome.rejections] == ["start_time_disagrees_with_expiry"]


class TestTheStartedGameGateIsNotOrderDependent:
    def test_the_same_rows_in_either_order_give_the_same_verdict(self) -> None:
        """It consulted ``rows[0]``, so which book happened to be first decided
        whether a fixture counted as under way — and books disagree about a start
        by minutes routinely and by hours in tennis."""
        as_of = datetime(2026, 7, 28, 23, 0, tzinfo=UTC)
        started = make_quote(
            source="book_a", selection=Selection.HOME, decimal_odds=2.10,
            commence_time=as_of - timedelta(hours=1), source_market_id="a",
        )
        late = make_quote(
            source="book_b", selection=Selection.AWAY, decimal_odds=2.10,
            commence_time=as_of + timedelta(hours=1), source_market_id="b",
        )
        forward = find_opportunities([started, late], as_of=as_of)
        backward = find_opportunities([late, started], as_of=as_of)
        assert forward.comparable_group_count == backward.comparable_group_count == 0
        assert forward.opportunities == backward.opportunities == []


class TestLineShoppingUsesThePriceActuallyPaid:
    def test_the_better_quoted_price_is_not_always_the_better_price(self) -> None:
        """The one surface the commission model had not been applied to, and it
        contradicted the detector on exactly the legs the model exists for."""
        book = make_quote(source="sportsbook", selection=Selection.HOME,
                          decimal_odds=2.08, source_market_id="a")
        exchange = make_quote(source="exchange", selection=Selection.HOME,
                              decimal_odds=2.10, source_market_id="b")
        commissions = {"exchange": WinningsCommission(label="exchange", rate=0.05)}

        gross = best_prices([book, exchange], {})
        assert next(iter(gross.values()))[Selection.HOME].source == "exchange"

        net = best_prices([book, exchange], commissions)
        assert next(iter(net.values()))[Selection.HOME].source == "sportsbook", (
            "the exchange quotes 2.10 and pays 2.045, which is worse than 2.08"
        )


class TestTheMirrorGateIsConsulted:
    def test_two_sources_that_are_one_counterparty_fail_the_run(self) -> None:
        """The gate was measured and then never asked.  ``src.distinctness`` had
        no caller outside the tests, so the registry could gain a mirror and
        nothing would say so — and measuring without acting is the same as not
        measuring."""
        from src.collector import _check_distinctness
        from src.validation import ValidationReport

        quotes: list[Quote] = []
        for index in range(15):
            event = f"MLB-AA{index}@MLB-BB{index}:2026-07-28"
            for source in ("tenant_a", "tenant_b"):
                for selection, odds in ((Selection.HOME, 1.91), (Selection.AWAY, 1.95)):
                    quotes.append(make_quote(
                        source=source, event_key=event, selection=selection,
                        decimal_odds=odds, source_event_id=f"e{index}",
                        source_market_id=f"{source}-{index}",
                    ))
        report = ValidationReport()
        _check_distinctness(quotes, report)
        codes = {finding.code for finding in report.errors}
        assert "sources_are_one_counterparty" in codes

    def test_two_genuinely_different_books_do_not(self) -> None:
        quotes: list[Quote] = []
        for index in range(15):
            event = f"MLB-AA{index}@MLB-BB{index}:2026-07-28"
            for source, home, away in (
                ("book_a", 1.91, 1.95),
                ("book_b", 1.87, 2.00),
            ):
                for selection, odds in ((Selection.HOME, home), (Selection.AWAY, away)):
                    quotes.append(make_quote(
                        source=source, event_key=event, selection=selection,
                        decimal_odds=odds, source_event_id=f"e{index}",
                        source_market_id=f"{source}-{index}",
                    ))
        from src.collector import _check_distinctness
        from src.validation import ValidationReport

        report = ValidationReport()
        _check_distinctness(quotes, report)
        assert report.errors == []


class TestTheRegressionAlarmDoesNotExpire:
    def _health(self, source: str, quote_count: int):
        from src.sources.base import SourceHealth

        return SourceHealth(
            source_key=source, ok=quote_count > 0,
            checked_at=datetime(2026, 7, 28, 7, 0, tzinfo=UTC),
            quote_count=quote_count,
        )

    def test_a_feed_that_stopped_long_ago_is_still_reported(self, tmp_path) -> None:
        """A ten-run lookback made the alarm expire while the fault persisted:
        the dead source dropped out of the window, the error disappeared, and an
        unattended loop went green with a permanently broken book."""
        from src.collector import _check_source_health
        from src.store import Store
        from src.validation import ValidationReport, validate

        with Store(tmp_path / "db.sqlite3") as store:
            first = store.start_run(datetime(2026, 7, 1, tzinfo=UTC))
            store.save_health(first, self._health("gone", 500))
            store.save_health(first, self._health("alive", 500))
            store.finish_run(
                first, finished_at=datetime(2026, 7, 1, tzinfo=UTC),
                report=validate([make_quote()]),
            )
            for _ in range(30):
                run = store.start_run(datetime(2026, 7, 2, tzinfo=UTC))
                store.save_health(run, self._health("gone", 0))
                store.save_health(run, self._health("alive", 500))

            report = ValidationReport()
            _check_source_health(
                [self._health("gone", 0), self._health("alive", 500)],
                report,
                configured=["gone", "alive"],
                store=store,
                run_id=run + 1,
            )
        codes = {finding.code for finding in report.errors}
        assert "source_stopped_producing" in codes

    def test_a_narrowed_run_is_not_compared_against_unnarrowed_history(
        self, tmp_path
    ) -> None:
        """Several adapters accept a league they have no path for, so a
        ``--league`` run manufactures the alarm rather than raising it."""
        from src.collector import _check_source_health
        from src.store import Store
        from src.validation import ValidationReport, validate

        with Store(tmp_path / "db.sqlite3") as store:
            first = store.start_run(datetime(2026, 7, 1, tzinfo=UTC))
            store.save_health(first, self._health("gone", 500))
            store.save_health(first, self._health("alive", 500))
            store.finish_run(
                first, finished_at=datetime(2026, 7, 1, tzinfo=UTC),
                report=validate([make_quote()]),
            )
            report = ValidationReport()
            _check_source_health(
                [self._health("gone", 0), self._health("alive", 500)],
                report,
                configured=["gone", "alive"],
                store=store,
                run_id=first + 1,
                narrowed=True,
            )
        assert "source_stopped_producing" not in {f.code for f in report.errors}
        assert "regression_check_skipped_for_a_narrowed_run" in {
            f.code for f in report.warnings
        }


class TestTheDashboardCapKeepsEverySource:
    def test_truncation_drops_distant_fixtures_not_whole_sportsbooks(
        self, tmp_path
    ) -> None:
        """Within one run every row shares an instant, so ordering by insert
        order orders by *source* — rows go in one source at a time,
        alphabetically.  A cap on that embedded three sportsbooks out of ten and
        silently dropped the rest, while the coverage grid still showed all ten.
        """
        from src.report import build_report
        from src.store import Store
        from src.validation import validate

        quotes = [
            make_quote(
                source=f"book{book:02d}",
                event_key=f"MLB-AA{game}@MLB-BB{game}:2026-07-28",
                source_event_id=f"e{game}",
                source_market_id=f"book{book:02d}-{game}",
                commence_time=datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
                + timedelta(hours=game),
            )
            for book in range(10)
            for game in range(10)
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime(2026, 7, 28, 7, 0, tzinfo=UTC))
            store.save_quotes_by_source(run, quotes)
            store.finish_run(
                run, finished_at=datetime(2026, 7, 28, 7, 1, tzinfo=UTC),
                report=validate(quotes),
            )
            data = build_report(store, max_quote_rows=30)

        strings = data["strings"]
        column = data["quotes"]["columns"].index("source")
        embedded = {strings[row[column]] for row in data["quotes"]["rows"]}
        assert len(embedded) == 10, f"only {sorted(embedded)} survived the cap"
        assert data["meta"]["quote_rows_capped"] is True


class TestReplayToleratesACapturedRefusal:
    def test_a_source_that_failed_to_fetch_does_not_fail_the_replay(
        self, tmp_path, monkeypatch
    ) -> None:
        """Introduced by the fix for a different finding, and caught live.

        Capturing the bytes that explain a failure is right — a block page whose
        markers changed is unreadable from a log line and obvious from the
        payload.  But those bytes are not a slate, and handing them back to the
        parser raises: Pinnacle's 503 maintenance page has no matchups/markets
        pair to join, so ``replay`` failed for the whole run every time a venue
        had a bad hour, on a source that had contributed nothing either way.

        The distinction that keeps both properties is *whether the source stored
        rows*.  A parse that raises where quotes were stored is a real
        regression and still fails the replay; a parse that raises on a captured
        refusal had nothing to reproduce, so it is a note.
        """
        from src import collector
        from src.raw_store import RawStore
        from src.sources.guards import ServerError
        from src.store import Store
        from tests.test_pipeline import FakeSource

        raw_store = RawStore(tmp_path / "raw")

        class Refusing(FakeSource):
            """Fetches nothing, but hands back the response that explains why."""

            def fetch_raw(self, *, tier=None):
                error = ServerError("bovada:MLB: HTTP 503: MAINTENANCE")
                error.raw = _raw("bovada", "events:MLB", {"detail": "down"})
                raise error

        class Unreadable(FakeSource):
            """What replay meets: a parser handed a maintenance page."""

            def parse(self, raws):
                raise AssertionError("no matchups/markets pair in this response")

        rows = [make_quote(source="fanduel")]
        for key, quotes in (("fanduel", rows), ("pinnacle", [make_quote(source="pinnacle")])):
            monkeypatch.setitem(
                collector.SOURCE_FACTORIES, key,
                lambda _quotes=quotes, _key=key, **kwargs: FakeSource(
                    _key, _quotes, leagues=("MLB",)
                ),
            )
        monkeypatch.setitem(
            collector.SOURCE_FACTORIES, "bovada",
            lambda **kwargs: Unreadable("bovada", [], leagues=("MLB",)),
        )
        # ``fanduel`` is a state-sensitive key, so replay resolves it from the run's
        # jurisdiction rather than from ``SOURCE_FACTORIES``. What is under test here
        # is replay's tolerance of a captured refusal, not state resolution, so the
        # seam is pointed back at the patched factories for the duration.
        monkeypatch.setattr(
            collector, "replay_factory",
            lambda state, key: collector.SOURCE_FACTORIES[key],
        )

        sources = [
            FakeSource("fanduel", rows, leagues=("MLB",),
                       raws=[_raw("fanduel", "slate", {})]),
            FakeSource("pinnacle", [make_quote(source="pinnacle")], leagues=("MLB",),
                       raws=[_raw("pinnacle", "slate", {})]),
            Refusing("bovada", [], leagues=("MLB",)),
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            result = collector.collect_once(sources, raw_store=raw_store, store=store)
            # The refusal was kept, which is the point of capturing it.
            assert any(
                source == "bovada" for source, _ in store.raw_paths(result.run_id)
            )
            ok, problems = collector.replay_run(
                result.run_id, store=store, raw_store=raw_store
            )
            assert ok, problems

            # ...but the same raise on a source that *did* store rows is a
            # regression, and must still be reported.
            monkeypatch.setitem(
                collector.SOURCE_FACTORIES, "fanduel",
                lambda **kwargs: Unreadable("fanduel", [], leagues=("MLB",)),
            )
            ok, problems = collector.replay_run(
                result.run_id, store=store, raw_store=raw_store
            )
        assert not ok
        assert any("fanduel: replay raised" in problem for problem in problems)


# ── round two ────────────────────────────────────────────────────────────────


class TestLineShoppingAgreesWithTheDetector:
    def test_the_printed_sum_is_the_one_arb_acts_on(self, capsys) -> None:
        """``best_prices`` was made net; the command that prints it was not.

        So ``lines`` selected the right pair and then described it with the wrong
        arithmetic: on the committed slate four markets printed a sum below 1.0
        that is really a loss — ``MLB-TEX@MLB-TB total/full_game @8.5`` showed
        0.9976 against a net 1.0200 — while ``arb`` correctly reported nothing.
        An operator comparing the two would have concluded the detector was
        broken, which is the one conclusion that makes a false positive
        actionable.
        """
        # A book at 2.05 against an exchange at 1.96 whose net is 1.9408: the
        # quoted pair sums to 0.9980 and looks like a 0.2% edge; the net pair
        # sums to 1.0031 and loses.  This is the whole failure in two prices.
        quotes = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.05),
            make_quote(source="matchbook", selection=Selection.AWAY, decimal_odds=1.96),
        ]
        printed = _print_lines(quotes, capsys)
        sums = [float(part.split()[-1].rstrip(")")) for part in printed if "(sum " in part]
        assert sums, printed

        net = sum(
            1.0 / net_decimal_odds(q.source, q.decimal_odds) for q in quotes
        )
        gross = sum(1.0 / q.decimal_odds for q in quotes)
        assert gross < 1.0 < net, (gross, net)
        assert sums[0] == pytest.approx(net, abs=5e-5)

        # And the price shown is the price paid, with the quoted one beside it
        # rather than instead of it.
        body = "\n".join(printed)
        assert "1.941" in body and "quoted 1.960" in body

    def test_a_book_is_printed_exactly_as_quoted(self, capsys) -> None:
        """Nothing is added where nothing is charged."""
        quotes = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.08),
            make_quote(source="pinnacle", selection=Selection.AWAY, decimal_odds=1.95),
        ]
        body = "\n".join(_print_lines(quotes, capsys))
        assert "quoted" not in body and "commission" not in body
        assert "2.080" in body and "1.950" in body


def _print_lines(quotes, capsys) -> list[str]:
    """Run ``lines`` over *quotes* and return what it printed."""
    import argparse
    from unittest.mock import patch

    from src import collector

    now = datetime.now(UTC)

    class _FakeStore:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def latest_run_id(self, *, only_ok=False, sports=None, leagues=None):
            return 1

        def run_scope(self, run_id):
            return [], [], 0

        def recorded_counterparty_groups(self, run_id):
            return {}

        def run_row(self, run_id):
            # ``jurisdiction`` and ``route_scope`` are columns the real row always
            # carries, and ``lines`` now reads both to say which prices are out of
            # state. A double that omits a column the production row has does not
            # simplify the test, it just fails somewhere unrelated later.
            return {
                "id": run_id,
                "started_at": now.isoformat(),
                "finished_at": now.isoformat(),
                "ok": 1,
                "quote_count": len(quotes),
                "jurisdiction": "IL",
                "route_scope": "state",
            }

        def load_quotes(self, run_id, sports=None, leagues=None):
            return list(quotes)

    args = argparse.Namespace(
        run=None, limit=50, cross_book_only=False, sport=None, league=None
    )
    with patch.object(collector, "_open_store", lambda: _FakeStore()):
        collector._cmd_lines(args)
    return capsys.readouterr().out.splitlines()


class TestOneCollectionPassIsOneCapture:
    def test_two_passes_five_minutes_apart_are_not_merged(self) -> None:
        """``latest_capture`` guessed at run boundaries from timestamps, and no
        threshold can do that job.

        Its window was 20 minutes; the default watch interval is 5
        (:data:`src.settings.DEFAULT_INTERVAL_SECONDS`).  Narrowing the window
        does not help either — the slowest source takes about 2½ minutes for one
        pass, so anything tight enough to separate two passes splits one in half.
        The consequence on an order-book source is not a stale row but a phantom
        price: ``page 02`` covers different markets each pass, and the best-offer
        pass takes the maximum across whatever it is given, so a five-minute-old
        resting order that has since been filled outbids the live book.

        The collector knows where a pass ends, so it records it.
        """
        from src.sources._common import latest_capture, latest_per_endpoint

        first = [
            _stamped("markets:baseball:01", FETCHED, "run-a"),
            _stamped("orders:baseball:01", FETCHED, "run-a"),
            _stamped("orders:baseball:02", FETCHED, "run-a"),
        ]
        later = FETCHED + timedelta(minutes=5)
        second = [
            _stamped("markets:baseball:01", later, "run-b"),
            _stamped("orders:baseball:01", later, "run-b"),
        ]

        kept = latest_capture([*first, *second])
        assert {raw.endpoint for raw in kept} == {
            "markets:baseball:01",
            "orders:baseball:01",
        }
        assert all(raw.capture_id == "run-b" for raw in kept)
        # The trailing batch of the older pass is gone, so it cannot stand as
        # "the latest" for a label the newer pass did not reach.
        assert "orders:baseball:02" not in {
            raw.endpoint for raw in latest_per_endpoint(kept)
        }

    def test_captures_written_before_the_stamp_existed_still_work(self) -> None:
        """Old envelopes carry no stamp, and must keep the behaviour they had."""
        from src.sources._common import latest_capture

        old = [
            _stamped("orders:baseball:01", FETCHED, ""),
            _stamped("orders:baseball:02", FETCHED, ""),
        ]
        assert len(latest_capture(old)) == 2
        # Yesterday's pass is still separated, which is what the window did do.
        stale = _stamped("orders:baseball:03", FETCHED - timedelta(days=1), "")
        assert len(latest_capture([*old, stale])) == 2

    def test_a_stamped_pass_never_inherits_an_unstamped_one(self) -> None:
        """A mixed batch must not let an old capture smuggle pages into a new
        one just because it predates the stamp."""
        from src.sources._common import latest_capture

        mixed = [
            _stamped("orders:baseball:02", FETCHED, ""),
            _stamped("orders:baseball:01", FETCHED + timedelta(minutes=1), "run-b"),
        ]
        kept = latest_capture(mixed)
        assert [raw.endpoint for raw in kept] == ["orders:baseball:01"]

    def test_the_collector_stamps_one_id_per_pass(self, tmp_path) -> None:
        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.store import Store
        from tests.test_pipeline import FakeSource

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            ids = []
            for _ in range(2):
                sources = [
                    FakeSource("fanduel", [make_quote(source="fanduel")], leagues=("MLB",),
                               raws=[_raw("fanduel", "slate", {})]),
                    FakeSource("pinnacle", [make_quote(source="pinnacle")], leagues=("MLB",),
                               raws=[_raw("pinnacle", "slate", {})]),
                ]
                result = collect_once(sources, raw_store=raw_store, store=store)
                stamps = {
                    raw_store.read(path).capture_id
                    for _, path in store.raw_paths(result.run_id)
                }
                assert len(stamps) == 1, stamps
                ids.append(stamps.pop())
        assert ids[0] and ids[1] and ids[0] != ids[1]


def _stamped(endpoint: str, when: datetime, capture_id: str) -> RawResponse:
    return RawResponse(
        source="sxbet",
        endpoint=endpoint,
        url="https://example.invalid/x",
        status_code=200,
        body="{}",
        fetched_at=when,
        content_type="application/json",
        capture_id=capture_id,
    )


class TestPredictionMarketLegsDoNotVoidWithABooksLeg:
    def test_a_moneyline_across_two_settlement_regimes_is_refused(self) -> None:
        """The void-risk note was suppressed for exactly the market that needed it.

        ``_build_opportunity`` attached a settlement caveat only when the market
        was not a moneyline, on the reasoning that every US book gives a
        moneyline action after five innings.  True, and about a *shortened* game;
        the failure that matters is the game that is never completed as
        scheduled, and there the venues do different things.  From the captured
        payloads, verbatim: Kalshi's ``rules_secondary`` says the market "will
        remain open and close after the rescheduled game has finished" and a
        cancellation "will resolve to a fair price"; Polymarket's description
        says a cancelled game "will resolve 50-50".  A sportsbook refunds.

        So the game is rained out and replayed the next day: the book refunds,
        Kalshi settles the make-up game, and what was reported as risk-free is a
        naked one-sided bet for the whole Kalshi stake.

        Refused, not noted.  A note left ``is_risk_free`` returning ``True`` on a
        position the same object's prose said was not risk-free, and ranked it
        above a same-margin position that genuinely was — while this module's
        stated rule is that settlement outcomes are enumerated and the minimum
        reported, precisely so that nothing depends on a caller reading a
        caveat.
        """
        quotes = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10),
            make_quote(source="kalshi", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        report = find_opportunities(quotes, as_of=FETCHED)
        assert not report.opportunities
        codes = [d.code for d in report.diagnostics]
        assert codes == ["legs_do_not_void_together"], codes
        detail = report.diagnostics[0].detail
        assert "kalshi" in detail and "fanduel" in detail

    def test_two_books_are_not_given_a_caveat_that_does_not_apply(self) -> None:
        """Both refund, so there is nothing to warn about — a note printed on
        every position is a note nobody reads."""
        quotes = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10),
            make_quote(source="pinnacle", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        report = find_opportunities(quotes, as_of=FETCHED)
        assert report.opportunities, [d.code for d in report.diagnostics]
        assert not any(
            "void together" in note for note in report.opportunities[0].notes
        )

    def test_an_exchange_settles_like_the_book_it_is_paired_with(self) -> None:
        """Matchbook voids a cancelled fixture and returns the stake, so an
        exchange leg is in the sportsbook's regime and must not be flagged."""
        quotes = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10),
            make_quote(source="matchbook", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        report = find_opportunities(quotes, as_of=FETCHED)
        assert report.opportunities, [d.code for d in report.diagnostics]
        assert not any(
            "void together" in note for note in report.opportunities[0].notes
        )

    def test_every_registered_source_declares_how_it_settles(self) -> None:
        """The silent default is the dangerous one, so the registry refuses it —
        the same guarantee it already gives for commission."""
        from src.settlement import SETTLEMENT
        from src.sources import registry

        assert {entry.key for entry in registry.SOURCES} <= set(SETTLEMENT)


class TestOrderDrivenVenuesAreNotAccusedOfMispricing:
    def test_a_narrowly_crossed_exchange_is_reported_but_does_not_fail_the_run(
        self,
    ) -> None:
        """``negative_overround`` is an ERROR whose message — "a book does not
        price itself to lose, so the prices or lines are mispaired" — is simply
        false for an order book, where the two sides are separate books nobody
        quoted against each other.

        The captured slate is already on the boundary: Kalshi's two tightest
        moneylines sum to exactly 1.0000 and Matchbook's to 1.0023, so one cent
        of movement on any of them turned a healthy ten-source run into a failed
        one over a real observation.
        """
        from src.validation import Severity, validate

        # One cent of probability crossed: 0.51 + 0.4854 = 0.9954.
        crossed = [
            make_quote(source="kalshi", selection=Selection.HOME, decimal_odds=1.9608),
            make_quote(source="kalshi", selection=Selection.AWAY, decimal_odds=2.0600),
        ]
        report = validate(crossed, order_book_sources={"kalshi"})
        found = [f for f in report.findings if f.code == "negative_overround"]
        assert found and found[0].severity is Severity.WARNING
        assert "separate order books" in found[0].message
        # The old check could not tell a crossed market from a mispairing at
        # this size and had to state both readings.  Judged net of the venue's
        # own commission it can: nobody can take both sides at a profit here,
        # so the message asserts the benign reading and shows the number that
        # justifies it.
        assert "not a mispairing" in found[0].message
        assert "1.03" in found[0].message  # the net sum, on display
        assert not report.errors

    def test_a_sportsbook_pricing_itself_to_lose_is_still_an_error(self) -> None:
        """The check is widened for order books, not removed."""
        from src.validation import Severity, validate

        crossed = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.9608),
            make_quote(source="fanduel", selection=Selection.AWAY, decimal_odds=2.0600),
        ]
        report = validate(crossed)
        found = [f for f in report.findings if f.code == "negative_overround"]
        assert found and found[0].severity is Severity.ERROR

    def test_a_badly_crossed_order_book_is_still_an_error(self) -> None:
        """A market crossed by more than a couple of percent does not survive the
        seconds it takes to collect it, so that is still evidence of a fault."""
        from src.validation import Severity, validate

        broken = [
            make_quote(source="kalshi", selection=Selection.HOME, decimal_odds=2.50),
            make_quote(source="kalshi", selection=Selection.AWAY, decimal_odds=2.50),
        ]
        report = validate(broken, order_book_sources={"kalshi"})
        found = [f for f in report.findings if f.code == "negative_overround"]
        assert found and found[0].severity is Severity.ERROR
        assert "mispairing" in found[0].message


class TestKalshiExpiryCrossCheckIsTightEnoughToCatchAShift:
    def test_a_ticker_that_decodes_three_hours_early_is_refused(self) -> None:
        """The bound was four times looser than the data it checks.

        Across all 354 captured markets ``occurrence_datetime`` minus the decoded
        ticker start is *exactly* three hours, with no variance.  Twelve hours
        caught a ticker that decoded late but waved through one that decoded
        early — which is the direction that matters, because a three-hour-early
        start is the same offset that put Kalshi's game one inside every other
        book's game two cluster and produced a 20% "guaranteed" position.
        """
        outcome = parse_kalshi([_kalshi_page(expiry_offset_hours=6)])
        assert not outcome.quotes
        assert any(
            r.reason == "start_time_disagrees_with_expiry" for r in outcome.rejections
        ), outcome.rejections

    def test_the_observed_three_hour_gap_is_still_accepted(self) -> None:
        outcome = parse_kalshi([_kalshi_page(expiry_offset_hours=3)])
        assert outcome.quotes, outcome.rejections


def _kalshi_page(*, expiry_offset_hours: int):
    """One Kalshi moneyline whose expiry sits *offset* hours after the start."""
    start = _ticker_start("26JUL282210AZPIT")
    assert start is not None
    expiry = start + timedelta(hours=expiry_offset_hours)
    def market(code: str, ask: float) -> dict:
        return {
            "ticker": f"KXMLBGAME-26JUL282210AZPIT-{code}",
            "event_ticker": "KXMLBGAME-26JUL282210AZPIT",
            "status": "active",
            "yes_ask_dollars": f"{ask:.4f}",
            "yes_bid_dollars": f"{ask - 0.04:.4f}",
            "no_ask_dollars": f"{1.0 - ask + 0.04:.4f}",
            "no_bid_dollars": f"{1.0 - ask:.4f}",
            "occurrence_datetime": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    return _raw("kalshi", "markets:KXMLBGAME:01", {
        "markets": [market("PIT", 0.55), market("AZ", 0.47)]
    })


class TestOneClubIsOneKeyAcrossEveryVenuesSpelling:
    def test_brighton_joins_across_all_three_spellings(self) -> None:
        """Three sources priced Brighton v Aston Villa and the pipeline produced
        three non-comparable outcomes and zero cross-book markets.

        Two separate defects with one visible effect.  ``_PAIRING`` matched the
        ampersand, so Bovada's "Brighton & Hove Albion" was skipped as a doubles
        pairing — the module docstring says this exact fixture had already been
        rescued once, from the *word* "and", and the symbol went on doing the
        same thing.  And the surviving two still disagreed: the alias
        ``brighton -> brightonhovealbion`` pointed at a slug that no full
        spelling produced, so FanDuel's "Brighton" and Pinnacle's "Brighton and
        Hove Albion" landed on different keys.
        """
        from src.leagues import league
        from src.participants import canonical_participant

        epl = league("EPL")
        keys = {
            canonical_participant(name, epl).key
            for name in (
                "Brighton & Hove Albion",   # Bovada
                "Brighton and Hove Albion", # Pinnacle
                "Brighton",                 # FanDuel
            )
        }
        assert keys == {"SOCCER-brightonhovealbion"}

    def test_a_doubles_pair_written_with_an_ampersand_is_still_unresolvable(
        self,
    ) -> None:
        """The symbol still separates two people, because pairs are a tennis
        regime — clubs do not enter two at a time."""
        from src.leagues import league
        from src.participants import canonical_participant

        assert canonical_participant("Bolelli & Vavassori", league("ATP")) is None


class TestBetHistoryDoesNotSpliceTwoGamesOfADoubleheader:
    def test_the_ordinal_is_reassigned_when_the_earlier_game_starts(self) -> None:
        """Not a bug on its own — the documented consequence the dashboard must
        allow for, pinned here so a change to it is noticed.

        The doubleheader ordinal ranks the fixtures a *single collection* can
        see, and adapters drop started games.  Once game one is under way, game
        two is the only cluster left, is numbered one, and inherits the bare key
        game one carried an hour earlier.
        """
        from src.events import reconcile_event_keys

        early = FETCHED + timedelta(hours=5)
        late = FETCHED + timedelta(hours=9)
        both = [
            make_quote(source="fanduel", commence_time=early),
            make_quote(source="fanduel", commence_time=late, source_event_id="g2"),
        ]
        keyed, _ = reconcile_event_keys(both)
        by_time = {q.commence_time: q.event_key for q in keyed}
        assert by_time[late].endswith("#2")

        # Next collection: game one has started and is gone.
        after, _ = reconcile_event_keys(
            [q for q in both if q.commence_time == late]
        )
        assert after[0].event_key == by_time[early]
        assert after[0].event_key != by_time[late]


class TestSmarketsAsksOnlyForWhatItWillUse:
    def test_a_market_the_parser_rejects_is_not_fetched(self) -> None:
        """``_market_ids`` selected by the sport-agnostic ``COLLECTED_MARKET_TYPES``
        while ``_parse_market`` rejected by ``(sport, name)``, so every
        out-of-scope winner market was paid for in a contracts request *and* a
        quotes request before being thrown away.

        Soccer publishes a two-way (draw-no-bet) beside its three-way, so on a
        hundred-fixture slate that is two hundred ids instead of one hundred —
        about ten wasted requests against a budget of twenty a minute that this
        adapter has already lost whole sports to.
        """
        from src.schema import Sport
        from src.sources.smarkets import COLLECTED_MARKET_TYPES, _market_ids

        page = _raw("smarkets", "markets:soccer:01", {"markets": [
            {"id": "1", "event_id": "e", "state": "open",
             "market_type": {"name": "WINNER_3_WAY"}},
            {"id": "2", "event_id": "e", "state": "open",
             "market_type": {"name": "WINNER_2_WAY"}},
        ]})
        assert _market_ids([page], COLLECTED_MARKET_TYPES) == {"1", "2"}
        assert _market_ids([page], COLLECTED_MARKET_TYPES, Sport.SOCCER) == {"1"}
        # Hockey is the mirror image: its two-way is the one that is collected.
        assert _market_ids([page], COLLECTED_MARKET_TYPES, Sport.HOCKEY) == {"2"}


class TestSxBetAsksOnlyForOrderBooksItWillRead:
    def test_a_market_out_of_scope_for_its_sport_gets_no_order_book(self) -> None:
        """``_collectable_hashes`` says its purpose is not to spend "somebody
        else's bandwidth for data that is discarded on arrival", and then applied
        a narrower scope than the parser: it checked the market type, status,
        quarter-line flag and start, but not whether the league resolves or
        whether the sport collects that market at all.  ``MARKETS_BY_SPORT``
        restricts tennis to the moneyline, so every tennis spread and total was
        fetched and thrown away.
        """
        from src.sources.sxbet import _collectable_hashes

        def market(hash_: str, type_: int, league: str, sport_id: int) -> dict:
            return {
                "marketHash": hash_, "type": type_, "status": "ACTIVE",
                "isQuarterLineMarket": False, "gameTime": int(LATER.timestamp()),
                "sportId": sport_id, "leagueLabel": league,
            }

        page = _raw("sxbet", "markets:tennis:01", {"data": {"markets": [
            market("0xml", 52, "ATP Toronto", 6),    # tennis match winner
            market("0xsp", 201, "ATP Toronto", 6),   # games handicap — out of scope
            market("0xus", 226, "ATP Toronto", 6),   # a US-sport code in tennis
            market("0xkbo", 226, "KBO League", 3),   # league does not resolve
        ]}})
        assert _collectable_hashes([page]) == {"0xml"}


# ── round three ──────────────────────────────────────────────────────────────


class TestACursorUrlKeepsItsOwnQuery:
    def test_following_a_next_page_link_does_not_strip_its_parameters(self) -> None:
        """The pagination fix did not page, and spent the budget it was written
        to respect.

        ``_fetch_events`` passed ``params=None`` for the cursor follow-up, and
        ``SourceClient.get`` collapsed that to ``{}`` — which httpx treats as a
        *replacement* of the URL's query rather than as "leave it alone".  So the
        cursor arrived complete with ``type``, ``state``, ``limit`` and
        ``offset``, and every one of them was deleted before the request went
        out.  Three failures at once: the cursor never advanced (the truncation
        it was fixing stayed open), the unfiltered reply returned other sports'
        events which were then paid for in contracts and quotes batches, and a
        full page of those could drive the loop to its 10-page cap re-requesting
        one URL.
        """
        import httpx

        from src.sources._common import SourceClient

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"events": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        http = SourceClient("smarkets", client=client)
        cursor = "https://api.smarkets.invalid/v3/events/?type=baseball&limit=100&offset=100"
        http.get(cursor, endpoint="events:baseball:02", params=None)
        http.get("https://api.smarkets.invalid/v3/events/", endpoint="events:baseball:01",
                 params={"type": "baseball", "limit": "100"})
        client.close()

        assert seen[0] == cursor, "the cursor's own query must survive"
        assert "type=baseball" in seen[1], "an explicit params dict must still apply"


class TestTheStampReachesTheParserToo:
    def test_the_response_handed_to_parse_carries_the_capture_id(self, tmp_path) -> None:
        """``_persist_raw`` stamped a **local** rebinding of a frozen dataclass,
        so the bytes on disk were stamped and the list handed to ``parse`` was
        not.

        That split is not cosmetic: ``latest_capture`` reads the stamp when there
        is one and falls back to a timestamp window when there is not, so
        collection and replay took different branches over the same bytes.  For a
        pass longer than that window they disagree — and for Smarkets they
        disagree by dropping the ``events`` response, so the parser raises during
        collection and succeeds on replay.
        """
        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.store import Store
        from tests.test_pipeline import FakeSource

        seen: list[str] = []

        class Watching(FakeSource):
            def parse(self, raws):
                seen.extend(raw.capture_id for raw in raws)
                return super().parse(raws)

        raw_store = RawStore(tmp_path / "raw")
        sources = [
            Watching("fanduel", [make_quote(source="fanduel")], leagues=("MLB",),
                     raws=[_raw("fanduel", "slate:01", {}), _raw("fanduel", "slate:02", {})]),
            FakeSource("pinnacle", [make_quote(source="pinnacle")], leagues=("MLB",),
                       raws=[_raw("pinnacle", "slate", {})]),
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(sources, raw_store=raw_store, store=store)
            on_disk = {
                raw_store.read(path).capture_id
                for _, path in store.raw_paths(result.run_id)
            }
        assert seen and all(seen), "parse was handed unstamped responses"
        assert len(set(seen)) == 1
        assert on_disk == set(seen), "disk and parser disagree about the pass"


class TestTheHistoryWindowIsTheLeaguesOwnTolerance:
    def test_a_tennis_fixture_priced_hours_apart_keeps_both_venues(self) -> None:
        """A flat two-hour window was narrower than the tolerance that put the
        two rows under one key in the first place.

        Tennis clusters at 14 hours because a match starts when the previous one
        on that court finishes, and three cross-source fixtures on the captured
        slate are 3½ to 6½ hours apart.  For each, the panel dropped one of the
        two venues and then reported "1 venue offering it — nothing to compare"
        about a bet the detector does compare.
        """
        from src.leagues import league
        from src.report import _fixture_tolerances

        class _Store:
            def query(self, sql, params=()):
                return [{"league": "ATP"}, {"league": "MLB"}]

        tolerances = _fixture_tolerances(_Store())
        assert tolerances["ATP"] == int(
            league("ATP").same_event_tolerance.total_seconds()
        )
        # Wide where two competitors never meet twice in a day...
        assert tolerances["ATP"] > 6 * 3600
        # ...and tight where they do, so a doubleheader stays two fixtures.
        assert tolerances["MLB"] < 3 * 3600


class TestMirrorsAreJudgedPerCompetition:
    def _slate(self, agreeing: str, differing: str, *, shared: int = 25):
        """Two sources that are one feed in one league and two in another."""
        rows = []
        for index in range(shared):
            key_a = f"TENNIS-a{index}@TENNIS-b{index}:2026-07-29"
            key_b = f"MLB-CIN@MLB-PHI:2026-07-{10 + index % 20}#{index}"
            for source, offset in (("book_one", 0.0), ("book_two", 0.0)):
                rows.append(make_quote(
                    source=source, sport=Sport.TENNIS, league=agreeing,
                    event_key=key_a, source_event_id=f"t{index}",
                    home_participant=f"TENNIS-b{index}", away_participant=f"TENNIS-a{index}",
                    home_team=f"B {index}", away_team=f"A {index}",
                    selection=Selection.HOME, decimal_odds=1.80 + offset,
                    commence_time=LATER,
                ))
            for source, offset in (("book_one", 0.0), ("book_two", 0.07)):
                rows.append(make_quote(
                    source=source, league=differing, event_key=key_b,
                    source_event_id=f"m{index}",
                    selection=Selection.HOME, decimal_odds=1.90 + offset,
                    commence_time=LATER,
                ))
        return rows

    def test_one_feed_in_one_league_is_a_mirror_however_it_averages(self) -> None:
        """The live slate is the case: BetRivers and LeoVegas score 64% overall
        and pass as DISTINCT, while agreeing on 132/132 ITF prices, 46/46 ATP
        Challenger, 40/40 ATP, 34/34 WTA and 27/27 Bundesliga.

        For all of tennis they are provably one price feed, and an "arbitrage"
        between them there is a position nobody can hold.  One rate over
        everything is exactly the statistic that hides it.
        """
        from src.distinctness import Verdict, compare_sources, find_mirrors

        quotes = self._slate("ATP", "MLB")
        pair = compare_sources(quotes, "book_one", "book_two")
        assert 0.4 < pair.rate < 0.6, pair.rate
        assert pair.verdict is Verdict.MIRROR
        assert [entry.league for entry in pair.mirrored_leagues] == ["ATP"]
        assert "one feed in ATP" in pair.summary()
        assert find_mirrors(quotes)

    def test_two_venues_that_merely_agree_often_are_still_distinct(self) -> None:
        """The rule must not fire on ordinary agreement: -110 is the most common
        number in the industry and two real books land on it constantly."""
        from src.distinctness import Verdict, compare_sources

        # Make the tennis leg disagree too, so no league is a full feed.
        quotes = self._slate("ATP", "MLB")
        for index, quote in enumerate(quotes):
            if quote.league == "ATP" and quote.source == "book_two" and index % 3 == 0:
                quotes[index] = quote.model_copy(update={"decimal_odds": 1.95})
        pair = compare_sources(quotes, "book_one", "book_two")
        assert pair.verdict is Verdict.DISTINCT
        assert not pair.mirrored_leagues


class TestAMirrorCannotBeArbitragedAgainstItself:
    def test_a_position_between_one_feeds_two_keys_is_not_reported(self) -> None:
        """The mirror gate measured and reported, and the detector went on
        comparing source *keys* — which is exactly what a mirror has two of."""
        one_feed = {"ATP": [frozenset({"book_one", "book_two"})]}
        quotes = [
            make_quote(source="book_one", sport=Sport.TENNIS, league="ATP",
                       event_key="TENNIS-a@TENNIS-b:2026-07-29",
                       home_participant="TENNIS-b", away_participant="TENNIS-a",
                       home_team="B", away_team="A", source_event_id="t1",
                       selection=Selection.HOME, decimal_odds=2.10, commence_time=LATER),
            make_quote(source="book_two", sport=Sport.TENNIS, league="ATP",
                       event_key="TENNIS-a@TENNIS-b:2026-07-29",
                       home_participant="TENNIS-b", away_participant="TENNIS-a",
                       home_team="B", away_team="A", source_event_id="t1",
                       selection=Selection.AWAY, decimal_odds=2.10, commence_time=LATER),
        ]
        assert find_opportunities(quotes, as_of=FETCHED, one_counterparty={}).opportunities
        assert not find_opportunities(
            quotes, as_of=FETCHED, one_counterparty=one_feed
        ).opportunities
        # ...and in a league where they are two books, the same pair is fine.
        elsewhere = {"WTA": [frozenset({"book_one", "book_two"})]}
        assert find_opportunities(
            quotes, as_of=FETCHED, one_counterparty=elsewhere
        ).opportunities

    def test_the_gate_applies_without_the_caller_asking_for_it(self) -> None:
        """The first version took the map as an argument, the live path passed it
        and the re-analysis path did not, and the same rows gave two answers
        depending on which command asked.  Measured from the quotes instead."""
        import inspect

        from src.arb import find_opportunities as detect

        assert inspect.signature(detect).parameters["one_counterparty"].default is None


class TestNonFiniteNumbersAreRefusedAtTheEdge:
    def test_a_bare_infinity_in_a_payload_is_not_decoded(self) -> None:
        """``json.loads`` accepts the bare words ``NaN`` and ``Infinity`` even
        though no JSON specification permits them.

        ``inf`` then passes an adapter's ``available > 0`` liquidity test, becomes
        a ``limit_amount``, and overflows the stake arithmetic in ``src.arb`` —
        the run dies inside the detector rather than at the row that caused it.
        """
        raw = _raw("matchbook", "events:baseball:01", {})
        broken = raw.__class__(**{
            **{f: getattr(raw, f) for f in raw.__dataclass_fields__},
            "body": '{"available-amount": Infinity}',
        })
        with pytest.raises(ValueError, match="Infinity"):
            broken.json()

    @pytest.mark.parametrize("field,value", [
        ("limit_amount", float("inf")),
        ("limit_amount", float("nan")),
        ("limit_amount", -500.0),
        ("limit_amount", 0.0),
        ("line", float("nan")),
        ("line", float("inf")),
    ])
    def test_the_schema_refuses_it_too(self, field: str, value: float) -> None:
        """Belt and braces, at the one gate every row passes through.

        ``nan`` is the nastier of the two: SQLite stores it as **NULL**, so a
        total's line round-trips to ``None`` and a row that loaded cleanly raises
        "market total requires a line" on the way back out, from inside
        ``load_quotes`` where there is no row left to name.
        """
        kwargs = {"market": Market.TOTAL, "selection": Selection.OVER, "line": 8.5}
        kwargs[field] = value
        with pytest.raises(Exception):
            make_quote(**kwargs)


class TestReadCommandsRefuseAStaleOrMissingRun:
    def test_a_run_collected_weeks_ago_is_not_offered_as_takeable(self, tmp_path) -> None:
        """``latest_run_id`` returns the newest *finished* run however old it is,
        and every freshness check in the detector is relative — two legs captured
        six weeks ago are seconds apart and clear it.  The started-game gate does
        not help either, since a fixture weeks out is still in the future.

        So the morning after collection silently stops, ``arb`` printed a full
        page of "guaranteed" positions priced before the outage, with nothing
        anywhere naming their age, and exited 0.
        """
        from datetime import timedelta

        from src.collector import _resolve_run
        from src.store import Store

        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            stale = store.start_run(datetime.now(UTC) - timedelta(days=45))
            # Rows stored: a run holding none is refused for that first, which
            # is a different gate from the age one under test here.
            store.save_quotes(stale, [make_quote()])
            store.finish_run(
                stale,
                finished_at=datetime.now(UTC) - timedelta(days=45),
                report=ValidationReport(quote_count=1),
            )

            run_id, note = _resolve_run(store, None, what="to analyse")
            # The newest run is the stale one, and it is refused rather than read.
            assert run_id is None
            assert "45 days ago" in note and "--run" in note

            # Asked for by id, it is readable as history, and says how old it is.
            run_id, note = _resolve_run(store, stale, what="to analyse")
            assert run_id == stale and "45 days ago" in note

            # A run that does not exist is not "nothing found".
            run_id, note = _resolve_run(store, 999_999, what="to analyse")
            assert run_id is None and "does not exist" in note

            # Nor is one that never finished.
            interrupted = store.start_run(datetime.now(UTC))
            run_id, note = _resolve_run(store, interrupted, what="to analyse")
            assert run_id is None and "never finished" in note


# ── round four ───────────────────────────────────────────────────────────────


class TestAReversedBookIsNamedRatherThanLosingItsJoins:
    def _reverse(self, quotes, source: str):
        from src.leagues import league as get_league

        out = []
        for quote in quotes:
            try:
                has_home_away = get_league(quote.league).has_home_away
            except KeyError:
                has_home_away = False
            if quote.source == source and has_home_away:
                out.append(quote.model_copy(update={
                    "home_participant": quote.away_participant,
                    "away_participant": quote.home_participant,
                    "home_team": quote.away_team,
                    "away_team": quote.home_team,
                    "event_key": f"{quote.home_participant}@{quote.away_participant}"
                                 f":{quote.event_key.partition(':')[2]}",
                }))
            else:
                out.append(quote)
        return out

    def _slate(self, count: int = 8):
        rows = []
        for index in range(count):
            key = f"MLB-CIN@MLB-PHI:2026-07-{10 + index}"
            for source in ("book_one", "book_two", "book_three"):
                for selection, odds in ((Selection.HOME, 1.95), (Selection.AWAY, 1.95)):
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"e{index}",
                        selection=selection, decimal_odds=odds, commence_time=LATER,
                    ))
        return rows

    def test_a_systematically_reversed_book_is_an_error(self) -> None:
        """The check ran on every run and could never fire.

        ``home_away_disagreement`` was grouped on ``event_key`` — but the key
        *is* ``away@home:date`` and reconciliation rebuilds it from the
        participants each source reported, so a reversed book does not join a
        group and disagree, it forms a group of its own.  Measured on the live
        slate with one book's team-sport rows reversed: 103 errors before
        reconciliation, **zero** after it, the run passing, and that book
        dropping from 133 shared fixtures to 30 — the only trace being 110
        warnings describing a completely reversed matchup as "a per-source
        doubleheader ordinal" correction.
        """
        from src.validation import Severity, validate

        report = validate(self._reverse(self._slate(), "book_two"))
        found = [f for f in report.findings if f.code == "home_away_disagreement"]
        assert found, [f.code for f in report.findings]
        assert found[0].severity is Severity.ERROR
        assert found[0].source == "book_two"
        assert "systematic" in found[0].message

    def test_one_fixture_oriented_two_ways_is_only_a_warning(self) -> None:
        """A match on neutral ground genuinely has no home side, and the live
        slate has exactly one: a pre-season friendly where FanDuel writes
        "Fulham @ Al Ahli", Pinnacle writes the reverse, and both price Fulham
        the favourite at 1.67/1.70.  Failing a run over that would teach an
        operator to ignore the check that catches the reversed book."""
        from src.validation import Severity, validate

        rows = self._slate()
        odd = [q for q in rows if q.event_key.endswith("-10")]
        rest = [q for q in rows if not q.event_key.endswith("-10")]
        report = validate([*rest, *self._reverse(odd, "book_two")])
        found = [f for f in report.findings if f.code == "home_away_disagreement"]
        assert found and found[0].severity is Severity.WARNING

    def test_an_untouched_slate_says_nothing(self) -> None:
        from src.validation import validate

        report = validate(self._slate())
        assert not [f for f in report.findings if f.code == "home_away_disagreement"]


class TestTheHandicapHasToFavourTheSameCompetitor:
    def _slate(self, count: int = 30):
        rows = []
        for index in range(count):
            key = f"MLB-CIN@MLB-PHI:2026-07-{10 + index % 20}#{index}"
            for source in ("book_one", "book_two", "book_three"):
                for selection, line, odds in (
                    (Selection.HOME, -1.5, 2.10), (Selection.AWAY, 1.5, 1.80)
                ):
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"e{index}",
                        market=Market.SPREAD, selection=selection, line=line,
                        decimal_odds=odds, commence_time=LATER,
                        source_market_id=f"{source}-m{index}",
                    ))
        return rows

    def test_a_source_with_the_handicap_on_the_wrong_side_is_an_error(self) -> None:
        """Nothing checked this at all.

        ``_check_group`` verifies that a spread mirrors within one source's own
        market — which a sign flip satisfies perfectly, since both of its sides
        flip together — and the cross-source checks compare sport, participants,
        orientation, league and start time, never the line.  On the live slate,
        flipping one source's spread signs adds up to 185 positions that exist
        only because of the flip, the largest printed as ``margin 24.86%,
        guaranteed +33.02 on 100``, with **no finding of any kind**.
        """
        from src.validation import Severity, validate

        flipped = [
            q.model_copy(update={"line": -q.line})
            if q.source == "book_two" and q.line is not None else q
            for q in self._slate()
        ]
        report = validate(flipped)
        found = [f for f in report.findings if f.code == "line_favours_the_other_competitor"]
        assert found, [f.code for f in report.findings]
        assert found[0].severity is Severity.ERROR and found[0].source == "book_two"

    def test_books_agreeing_on_the_favourite_say_nothing(self) -> None:
        from src.validation import validate

        report = validate(self._slate())
        assert not [
            f for f in report.findings if f.code == "line_favours_the_other_competitor"
        ]

    def test_a_venue_publishing_a_symmetric_ladder_is_not_judged(self) -> None:
        """Kalshi lists "PIT wins by over 1.5/2.5/3.5" *and* the same for Arizona,
        which is home ±1.5, ±2.5, ±3.5 with every row marked primary.  Flipping
        every sign maps that set onto itself, so there is no side to be wrong
        about and including it would be noise."""
        from src.validation import validate

        ladder = list(self._slate())
        for index in range(30):
            key = f"MLB-CIN@MLB-PHI:2026-07-{10 + index % 20}#{index}"
            for line in (-1.5, 1.5):
                ladder.append(make_quote(
                    source="ladder", event_key=key, source_event_id=f"e{index}",
                    market=Market.SPREAD, selection=Selection.HOME, line=line,
                    decimal_odds=2.0, commence_time=LATER,
                    source_market_id=f"ladder-m{index}-{line}",
                ))
        report = validate(ladder)
        assert not [
            f for f in report.findings
            if f.code == "line_favours_the_other_competitor" and f.source == "ladder"
        ]

    def test_a_multi_window_source_is_still_judged_per_window(self) -> None:
        """The ladder exemption is one line per (event, PERIOD), not per event.

        Grouped by event alone, a source posting a full-game spread beside a
        first-5-innings spread — betrivers_kambi's ordinary baseball slate —
        held two lines per group, tripped the one-line exemption meant for
        symmetric ladders, and was never judged: measured on run 32, 0 of 15
        baseball and 0 of 7 hockey cells graded, so a wholesale sign flip on
        exactly the multi-window book produced no finding of any kind.
        """
        from src.validation import Severity, validate

        rows = []
        for q in self._slate():
            if q.source == "book_two":
                # The flip under test, on every one of book_two's rows…
                q = q.model_copy(update={"line": -q.line})
                rows.append(q)
                # …and a second scoring window beside each full-game market,
                # which used to disqualify the source from judgement entirely.
                rows.append(q.model_copy(update={
                    "period": Period.FIRST_5_INNINGS,
                    "source_market_id": f"{q.source_market_id}-f5",
                }))
            else:
                rows.append(q)
        report = validate(rows)
        found = [
            f for f in report.findings
            if f.code == "line_favours_the_other_competitor" and f.source == "book_two"
        ]
        assert found, [f.code for f in report.findings]
        assert found[0].severity is Severity.ERROR

    def test_a_two_source_window_convicts_nobody(self) -> None:
        """Two sources have no majority, only a midpoint — refuse to judge.

        The per-period regrouping created secondary windows holding exactly
        two judgeable sources in production, and a median of two fails both
        ways: a symmetric flip cancels to 0 and reads as a pick'em (blind),
        and an asymmetric flip where the flipped book quotes the larger
        magnitude hands the midpoint the flipped sign — the HONEST source
        was convicted with a systematic ERROR while the flipped one walked.
        """
        from src.validation import validate

        rows = []
        for index in range(30):
            key = f"MLB-CIN@MLB-PHI:2026-07-{10 + index % 20}#{index}"
            for source, home_line in (("honest_book", -0.5), ("flipped_book", 0.75)):
                for selection, line in (
                    (Selection.HOME, home_line), (Selection.AWAY, -home_line)
                ):
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"e{index}",
                        market=Market.SPREAD, selection=selection, line=line,
                        period=Period.FIRST_5_INNINGS, decimal_odds=1.95,
                        commence_time=LATER, source_market_id=f"{source}-f5-{index}",
                    ))
        report = validate(rows)
        assert not [
            f for f in report.findings
            if f.code == "line_favours_the_other_competitor"
        ], [
            (f.source, f.severity, f.message[:80]) for f in report.findings
            if f.code == "line_favours_the_other_competitor"
        ]

    def test_a_one_window_wholesale_flip_is_systematic(self) -> None:
        """Rates are judged per window class, not pooled across windows.

        Pooled per source, a wholesale flip of one window on a two-window
        book capped at 50% shared cells — structurally below the systematic
        threshold — so a wrong-side mapping on one criterion label (only
        "Handicap - First 5 Innings", say) could never earn more than a
        WARNING, with the message counting windows as "fixtures".
        """
        from src.validation import Severity, validate

        rows = []
        for q in self._slate():
            rows.append(q)  # honest full-game window for all three books
            flip = -1.0 if q.source == "book_two" else 1.0
            rows.append(q.model_copy(update={
                "period": Period.FIRST_5_INNINGS,
                "line": q.line * flip,
                "source_market_id": f"{q.source_market_id}-f5",
            }))
        report = validate(rows)
        found = [
            f for f in report.findings
            if f.code == "line_favours_the_other_competitor" and f.source == "book_two"
        ]
        assert found, [f.code for f in report.findings]
        assert found[0].severity is Severity.ERROR, found[0].message
        assert "first_5_innings" in found[0].message

    def test_a_regulation_flip_is_judged_against_the_full_game_consensus(self) -> None:
        """FULL_GAME and REGULATION share a favourite (FULL_CONTEST_WINDOWS),
        so a regulation-only source is judged by the full-game majority.

        Split by raw period, hockey's regulation window held too few sources
        to judge at all, and the one book posting only regulation spreads —
        betrivers_kambi's ordinary hockey slate — could flip unnoticed.
        """
        from src.validation import Severity, validate

        rows = []
        for index in range(30):
            key = f"NHL-CIN@NHL-PHI:2026-07-{10 + index % 20}#{index}"
            for source in ("book_one", "book_three"):
                for selection, line in ((Selection.HOME, -1.5), (Selection.AWAY, 1.5)):
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"e{index}",
                        sport=Sport.HOCKEY, league="NHL",
                        market=Market.SPREAD, selection=selection, line=line,
                        decimal_odds=1.95, commence_time=LATER,
                        source_market_id=f"{source}-m{index}",
                    ))
            for selection, line in ((Selection.HOME, 1.5), (Selection.AWAY, -1.5)):
                rows.append(make_quote(
                    source="reg_only", event_key=key, source_event_id=f"e{index}",
                    sport=Sport.HOCKEY, league="NHL",
                    market=Market.SPREAD, selection=selection, line=line,
                    period=Period.REGULATION, decimal_odds=1.95,
                    commence_time=LATER, source_market_id=f"reg-m{index}",
                ))
        report = validate(rows)
        found = [
            f for f in report.findings
            if f.code == "line_favours_the_other_competitor" and f.source == "reg_only"
        ]
        assert found, [f.code for f in report.findings]
        assert found[0].severity is Severity.ERROR

    def test_windows_are_not_compared_across_periods(self) -> None:
        """An honest source pricing only one window must not be charged with
        the other window's median: grouped by event alone, a regulation-only
        line was compared against full-game numbers it never disagreed with.
        """
        from src.validation import validate

        rows = list(self._slate())
        # An honest fourth source that prices ONLY first-5-innings, with the
        # same orientation as everyone else's full-game lines.
        for index in range(30):
            key = f"MLB-CIN@MLB-PHI:2026-07-{10 + index % 20}#{index}"
            for selection, line in ((Selection.HOME, -0.5), (Selection.AWAY, 0.5)):
                rows.append(make_quote(
                    source="f5_only", event_key=key, source_event_id=f"e{index}",
                    market=Market.SPREAD, selection=selection, line=line,
                    period=Period.FIRST_5_INNINGS, decimal_odds=2.0,
                    commence_time=LATER, source_market_id=f"f5-m{index}",
                ))
        report = validate(rows)
        assert not [
            f for f in report.findings
            if f.code == "line_favours_the_other_competitor" and f.source == "f5_only"
        ]


class TestTheMirrorGateIsFiledWhereItIsRead:
    def test_a_dissenting_league_label_does_not_hide_a_mirror(self) -> None:
        """Two rules resolved "which league is this?" and they disagreed.

        ``src.distinctness._leagues`` filed a mirrored selection under the
        lexicographically first spelling; ``src.arb._counterparties`` looked the
        gate up under the modal one.  Matchbook labels 22 tennis fixtures ``ATP``
        where the rest of the slate says ``ATP_CHALLENGER``, ``ATP`` sorts first,
        and 44 Challenger selections on which the two Kambi tenants agree 44 of
        44 were filed under ATP — so ``ATP_CHALLENGER`` never appeared in the
        gate and a position between one book and itself was reportable there.
        """
        from src.arb import counterparty_groups

        rows = []
        for index in range(25):
            key = f"TENNIS-a{index}@TENNIS-b{index}:2026-07-29"
            common = dict(
                sport=Sport.TENNIS, event_key=key, source_event_id=f"t{index}",
                home_participant=f"TENNIS-b{index}", away_participant=f"TENNIS-a{index}",
                home_team=f"B {index}", away_team=f"A {index}",
                selection=Selection.HOME, decimal_odds=1.80, commence_time=LATER,
            )
            # The two tenants agree, and both call it ATP_CHALLENGER.
            for source in ("book_one", "book_two"):
                rows.append(make_quote(source=source, league="ATP_CHALLENGER", **common))
            # One unrelated source calls the same fixture ATP.
            rows.append(make_quote(source="dissenter", league="ATP", **common))

        from src.arb import EVERY_LEAGUE, _counterparties

        groups = counterparty_groups(rows)
        # The gate is no longer filed per league, so it cannot be filed under a
        # spelling other than the one it is read back under — this whole class
        # of disagreement can no longer reopen it.  See
        # :class:`TestOneBookIsOneCounterpartyInEveryLeague` for why the
        # per-league form was wrong on its own terms as well.
        assert list(groups) == [EVERY_LEAGUE], sorted(groups)
        assert frozenset({"book_one", "book_two"}) in groups[EVERY_LEAGUE]
        # And it reaches the detector under either spelling of the league.
        for spelling in ("ATP", "ATP_CHALLENGER"):
            market = [
                make_quote(source="book_one", league=spelling),
                make_quote(source="book_two", league=spelling),
            ]
            mapping = _counterparties(market, groups)
            assert mapping["book_one"] == mapping["book_two"], spelling


class TestAFixtureCannotBeChainedAcrossDays:
    def test_three_kickoffs_a_day_apart_are_not_one_fixture(self) -> None:
        """Single-linkage bounds each *gap* and not the total span, so N sources
        stepping along one at a time fused into one fixture spanning up to
        ``(N-1) x tolerance``.

        Three books listing kickoffs 29 hours apart in sequence became one event
        under soccer's 30-hour width, and the resulting "position" had its three
        legs on three different days — reported as a 16.67% guarantee, with
        nothing downstream to catch it, because ``_fixture_outliers`` deliberately
        does not compare ``commence_time``.
        """
        from datetime import timedelta

        from src.events import cluster_start_times

        base = FETCHED
        window = timedelta(hours=30)
        chain = [base + timedelta(hours=29 * step) for step in range(6)]
        clusters = cluster_start_times(chain, window)
        # The property, not the count: no fixture may span more than the width
        # that defines it, however many sources step along the chain.
        assert all(max(c) - min(c) <= window for c in clusters), clusters
        assert len(clusters) > 1
        # Two books genuinely inside the window still join — this is the case the
        # 30-hour width exists for, a Bundesliga fixture listed a day apart.
        pair = [base, base + timedelta(hours=26)]
        assert len(cluster_start_times(pair, window)) == 1


class TestABetterCrossRegimePriceDoesNotDestroyARealPosition:
    def test_the_position_falls_back_to_one_settlement_regime(self) -> None:
        """The refusal is right and its placement was not.

        ``_best_assignment`` knows nothing about settlement, so the cheapest legs
        could span two regimes even where a perfectly good same-regime position
        existed underneath — and the whole market was then discarded.  Adding a
        source offering a *better* price destroyed a real opportunity; 211 live
        market groups have both shapes available.
        """
        two_books = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.02),
            make_quote(source="pinnacle", selection=Selection.AWAY, decimal_odds=2.02),
        ]
        assert find_opportunities(two_books, as_of=FETCHED).opportunities

        with_better = [
            *two_books,
            make_quote(source="kalshi", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        report = find_opportunities(with_better, as_of=FETCHED)
        assert report.opportunities, [d.code for d in report.diagnostics]
        assert {leg.source for leg in report.opportunities[0].legs} == {
            "fanduel", "pinnacle"
        }

    def test_a_market_with_only_a_cross_regime_pair_is_still_refused(self) -> None:
        quotes = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10),
            make_quote(source="kalshi", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        report = find_opportunities(quotes, as_of=FETCHED)
        assert not report.opportunities
        assert [d.code for d in report.diagnostics] == ["legs_do_not_void_together"]


class TestACollapsedRunIsNotASuccess:
    def test_most_of_the_venues_failing_is_an_error(self) -> None:
        """Eight of ten geo-blocked on a fresh database reported zero errors and
        exited 0: ``insufficient_sources`` did not fire because two is still two,
        and ``source_stopped_producing`` needs a history a fresh database does not
        have.  ``_check_source_health``'s own docstring calls that "a catastrophic
        run"."""
        from src.collector import _check_source_health
        from src.sources.base import SourceHealth
        from src.validation import Severity, ValidationReport

        configured = [f"book_{i}" for i in range(10)]
        health = [
            SourceHealth(
                source_key=key,
                ok=index < 2,
                checked_at=FETCHED,
                quote_count=100 if index < 2 else 0,
                error_kind=None if index < 2 else "blocked",
                error_message=None if index < 2 else "HTTP 403",
            )
            for index, key in enumerate(configured)
        ]
        report = ValidationReport()
        _check_source_health(health, report, configured=configured)
        collapse = [
            f for f in report.findings
            if f.code == "configured_sources_produced_nothing"
        ]
        assert collapse and collapse[0].severity is Severity.ERROR

    def test_a_source_that_rejected_its_rows_is_reported(self) -> None:
        """``SourceHealth.ok=False`` was never converted into a finding, and
        ``RunResult.ok`` reads only the validation report — so a book that refused
        5,000 in-scope markets produced a report byte-identical to a clean run."""
        from src.collector import _check_source_health
        from src.sources.base import SourceHealth
        from src.validation import ValidationReport

        configured = ["book_a", "book_b"]
        health = [
            SourceHealth(source_key="book_a", ok=True, checked_at=FETCHED, quote_count=10),
            SourceHealth(
                source_key="book_b", ok=False, checked_at=FETCHED, quote_count=30_049,
                rejection_count=5_000, error_kind="rejections",
                error_message="5000 in-scope markets could not be represented",
            ),
        ]
        report = ValidationReport()
        _check_source_health(health, report, configured=configured)
        assert any(f.code.startswith("source_unhealthy:") for f in report.findings)


class TestAFutureStampedRunIsRefused:
    def test_a_clock_ahead_of_this_machine_does_not_pass_the_age_gate(
        self, tmp_path
    ) -> None:
        """Assuming UTC for a naive stamp fails *open* east of UTC: a run written
        at UTC+10 and read as UTC looks newer than it is, so a stale run sailed
        past the age gate on a negative number."""
        from datetime import timedelta

        from src.collector import _resolve_run
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            future = datetime.now(UTC) + timedelta(hours=3)
            run_id = store.start_run(future)
            # Rows stored, because a run holding none is refused for *that*
            # first — a different gate, and not the one under test here.
            store.save_quotes(run_id, [make_quote()])
            store.finish_run(
                run_id, finished_at=future,
                report=ValidationReport(quote_count=1),
            )
            resolved, note = _resolve_run(store, None, what="to analyse")
        assert resolved is None and "future" in note


# ── round five ───────────────────────────────────────────────────────────────


class TestAnAsianSplitLineIsStatedAsItsMidpoint:
    """The most expensive defect in this pipeline, and the quietest.

    Bovada writes a quarter (Asian) line as **two** fields on the runner's price:
    ``handicap`` and ``handicap2``.  The parser read only the first, so Everton
    at ``0.0``/``-0.5`` — a -0.25 line — was emitted as a draw-no-bet, and
    "Over 2.0 / handicap2 2.5" was emitted as a 2.0 total instead of 2.25.

    A quarter line read as zero is not a near-miss.  It is a different contract:
    a push where there is none, and half the stake on a handicap nobody offered.

    The scale, measured by re-parsing one live capture's own stored bytes:
    **41 of 43 reported positions had a Bovada leg, and every one of them was
    built on a mis-stated line.**  Reading the pair correctly takes the run from
    43 opportunities to 2.  Nothing else in the pipeline could see it — the two
    other sources that meet split lines refuse them outright, and ``src.arb``'s
    quarter-line machinery never fired because the rows arrived labelled as whole
    or half lines.
    """

    @staticmethod
    def _capture():
        """A real captured Bovada coupon, so the parser sees what it really sees."""
        import glob
        import json as _json

        from src.raw_store import RawResponse

        path = sorted(glob.glob("tests/fixtures/raw/bovada__*premier-league*.json"))[0]
        return RawResponse.from_envelope(_json.load(open(path)))

    def _relabelled(self, handicap, handicap2):
        """That coupon with every Goal Spread handicap replaced by one pair."""
        import json as _json

        from dataclasses import replace

        raw = self._capture()
        payload = _json.loads(raw.body)
        touched = 0
        for group in payload:
            for event in group.get("events") or []:
                for display in event.get("displayGroups") or []:
                    for market in display.get("markets") or []:
                        if market.get("description") != "Goal Spread":
                            continue
                        for index, entry in enumerate(market.get("outcomes") or []):
                            price = entry.get("price") or {}
                            sign = 1 if index % 2 else -1
                            price["handicap"] = f"{sign * handicap:g}"
                            if handicap2 is None:
                                price.pop("handicap2", None)
                            else:
                                price["handicap2"] = f"{sign * handicap2:g}"
                            touched += 1
        assert touched, "no Goal Spread outcomes in the capture"
        return replace(raw, body=_json.dumps(payload))

    def _spread_lines(self, handicap, handicap2):
        from src.sources.bovada import parse_bovada

        outcome = parse_bovada([self._relabelled(handicap, handicap2)])
        return outcome, sorted(
            {q.line for q in outcome.quotes if q.market is Market.SPREAD}
        )

    def test_the_two_halves_are_read_as_the_line_between_them(self) -> None:
        outcome, lines = self._spread_lines(0.0, 0.5)
        assert not [r for r in outcome.rejections if "handicap" in r.reason]
        assert lines == [-0.25, 0.25], lines

    def test_a_line_with_no_second_half_is_untouched(self) -> None:
        outcome, lines = self._spread_lines(1.5, None)
        assert not [r for r in outcome.rejections if "handicap" in r.reason]
        assert lines == [-1.5, 1.5], lines

    def test_a_pair_that_is_not_half_a_point_apart_is_refused(self) -> None:
        """54 of 54 occurrences in the fixtures and 114 of 114 live are exactly
        half a point apart.  Anything else is a shape this code has never seen,
        and guessing at it is what produced the defect."""
        outcome, lines = self._spread_lines(0.0, 2.0)
        assert lines == []
        assert {r.reason for r in outcome.rejections} == {"unrecognised_second_handicap"}


class TestEveryKalshiRowStatesItsDepth:
    def test_the_no_side_reads_the_field_the_venue_actually_sends(self) -> None:
        """``no_ask_size_fp`` appears in **none** of the 354 captured markets, so
        every UNDER on a total and one side of every spread — 255 of 590 rows —
        silently carried no bankroll cap.  ``src.arb`` reads a missing cap as
        "this leg states none", so those positions were sized from the other leg
        alone, under a note saying the real cap can only be lower.  It could be a
        great deal lower: one of them is $0.12.

        Buying NO at its ask is selling YES at its bid — ``no_ask == 1 -
        yes_bid`` holds exactly on all 354 markets — so the depth is there under
        another name.
        """
        import glob
        import json as _json

        from src.raw_store import RawResponse
        from src.sources.kalshi import parse_kalshi

        raws = [
            RawResponse.from_envelope(_json.load(open(path)))
            for path in sorted(glob.glob("tests/fixtures/raw/kalshi__*.json"))
        ]
        quotes = parse_kalshi(raws).quotes
        assert quotes
        assert all(q.limit_amount is not None for q in quotes), (
            "every Kalshi row states a depth"
        )
        # And the small ones are real, not a placeholder.
        assert min(q.limit_amount for q in quotes) < 1.0


class TestOneSourceCannotDisagreeWithEverybodyAboutAPrice:
    def _slate(self, count: int = 120):
        rows = []
        for index in range(count):
            key = f"MLB-CIN@MLB-PHI:2026-07-{10 + index % 20}#{index}"
            for source in ("book_one", "book_two", "book_three"):
                for selection, line, odds in (
                    (Selection.HOME, -1.5, 2.10), (Selection.AWAY, 1.5, 1.80)
                ):
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"e{index}",
                        market=Market.SPREAD, selection=selection, line=line,
                        decimal_odds=odds, commence_time=LATER,
                        source_market_id=f"{source}-m{index}",
                    ))
        return rows

    def test_a_source_pricing_a_different_bet_is_caught(self) -> None:
        """The general net beneath the specific checks.

        ``_check_line_orientation`` can only judge a source that publishes one
        primary line per fixture, and four of the ten publish a symmetric ladder
        — for those, negating every line maps the set of lines onto itself while
        moving each *price* onto the opposite handicap.  Flipping Matchbook that
        way added 58 reported positions, the largest at ``margin 24.90%``, and
        produced a byte-identical validation report.

        Prices catch it, and not only it: a selection on the wrong participant, a
        market filed as something it is not, a stale or unit-wrong price all look
        the same from here — one source disagreeing with everybody about what a
        bet is worth.
        """
        from src.validation import Severity, validate

        rows = self._slate()
        swapped = [
            q.model_copy(update={
                "decimal_odds": 6.0 if q.selection is Selection.HOME else 1.2,
                "implied_probability": 1 / 6.0 if q.selection is Selection.HOME else 1 / 1.2,
            }) if q.source == "book_two" else q
            for q in rows
        ]
        report = validate(swapped)
        found = [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
        ]
        assert found and found[0].severity is Severity.ERROR
        assert found[0].source == "book_two"

    def test_ordinary_price_differences_say_nothing(self) -> None:
        """Books differ by a point or two as a matter of course; the largest
        deviation any live source shows from a consensus of three is 0.083."""
        from src.validation import validate

        rows = [
            q.model_copy(update={
                "decimal_odds": q.decimal_odds * 1.03,
                "implied_probability": q.implied_probability / 1.03,
            }) if q.source == "book_three" else q
            for q in self._slate()
        ]
        report = validate(rows)
        assert not [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
        ]

    def _first_inning_slate(self, count: int = 40):
        """Three books pricing a three-way first-inning moneyline, one two-way.

        Modelled on the 2026-08-08 Pennsylvania board, where BetRivers, betPARX
        and theScore posted a three-way first-inning moneyline and Caesars posted
        a two-way one.  Both are honest books pricing different bets.

        The prices are drawn further apart than that board's, deliberately: the
        real gap between the two shapes was 0.35, but a *swap* within the
        three-way shape has to clear ``MAX_PRICE_DEVIATION`` too, and exchanging
        +210 with +400 moves the price only 0.12.  A test built on the live
        numbers would have reported the swap as undetectable when it is merely
        small.
        """
        rows = []
        for index in range(count):
            key = f"MLB-ATH@MLB-BOS:2026-08-{1 + index % 20}#{index}"
            for source in ("three_way_one", "three_way_two", "three_way_three"):
                for selection, odds in (
                    (Selection.HOME, 1.50),
                    (Selection.AWAY, 8.00),
                    (Selection.DRAW, 3.50),
                ):
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"e{index}",
                        market=Market.MONEYLINE, period=Period.FIRST_1_INNING,
                        selection=selection, line=None, decimal_odds=odds,
                        commence_time=LATER, sport=Sport.BASEBALL,
                        source_market_id=f"{source}-m{index}",
                    ))
            for selection, odds in ((Selection.HOME, 3.00), (Selection.AWAY, 1.55)):
                rows.append(make_quote(
                    source="two_way", event_key=key, source_event_id=f"e{index}",
                    market=Market.MONEYLINE, period=Period.FIRST_1_INNING,
                    selection=selection, line=None, decimal_odds=odds,
                    commence_time=LATER, sport=Sport.BASEBALL,
                    source_market_id=f"two_way-m{index}",
                ))
        return rows

    def test_a_two_way_moneyline_is_not_judged_against_a_three_way_one(self) -> None:
        """Different contracts, so their prices are not commensurable.

        The three-way home price loses when the inning is scoreless and the
        two-way one does not, so 0.323 and 0.677 are both right.  Compared as one
        market that is a 0.35 deviation, and this check indicted all thirty of
        Pennsylvania's first-inning moneylines the day period markets were first
        collected.

        ``src.arb`` already keeps the two shapes apart — ``contract_shape``
        groups them separately and ``ambiguous_tie_settlement`` refuses the
        two-way one outright in a draw-pricing window — so no position was ever
        at risk.  The cost was an ERROR firing every run for a benign reason,
        which is how a report stops being read.
        """
        from src.validation import validate

        report = validate(self._first_inning_slate())
        assert not [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
        ], "a two-way book must not be indicted for not being a three-way one"

    def test_a_suspended_draw_does_not_turn_a_three_way_book_two_way(self) -> None:
        """The shape is what the market settles on, not what is buyable now.

        ``arb.contract_shape`` already judges from all rows including suspended
        ones — suspending a price does not change which outcomes the market
        settles on.  The first cut of this check's ``prices_draw`` required the
        draw row to be ACTIVE, so a three-way book whose draw was momentarily
        suspended pooled with the genuine two-way book and the check indicted
        the two-way book — the same false ERROR, resurrected through a status
        filter.  Found by adversarial review before it shipped a full day.
        """
        from src.validation import validate

        slate = [
            q.model_copy(update={"status": QuoteStatus.SUSPENDED})
            if q.selection is Selection.DRAW
            else q
            for q in self._first_inning_slate()
        ]
        assert not [
            f for f in validate(slate).findings
            if f.code == "prices_disagree_with_every_other_source"
        ], "a suspended draw must not change which contract the book offers"

    def test_a_swapped_three_way_book_is_still_caught_beside_a_two_way_one(self) -> None:
        """The fix above must not become a way to hide a real fault.

        Same slate, but one of the three-way books has home and away exchanged —
        the permutation the whole check exists for.  Grouping by contract shape
        must separate it from the two-way book *without* also separating it from
        the two honest three-way books it has to be judged against.
        """
        from src.validation import Severity, validate

        swapped = [
            q.model_copy(update={
                "decimal_odds": 8.00 if q.selection is Selection.HOME else 1.50,
                "implied_probability": 1 / (
                    8.00 if q.selection is Selection.HOME else 1.50
                ),
            })
            if q.source == "three_way_one" and q.selection is not Selection.DRAW
            else q
            for q in self._first_inning_slate()
        ]
        found = [
            f for f in validate(swapped).findings
            if f.code == "prices_disagree_with_every_other_source"
        ]
        assert found, "a genuine swap must survive the shape split"
        assert found[0].severity is Severity.ERROR
        assert found[0].source == "three_way_one"

    def test_a_book_mirrored_in_one_sport_is_not_diluted_by_the_ones_it_gets_right(
        self,
    ) -> None:
        """The per-sport rate, which nothing else exercises.

        A source swapped in **one** sport is diluted below the overall threshold
        by the sports it prices correctly — measured before this check graded per
        sport, BetRivers mirrored in basketball alone was 17% of its basketball
        markets and 1.99% overall, just under the 2% bar, for 58 phantom
        positions and no finding.

        Here: 200 baseball fixtures priced correctly and 3 basketball fixtures
        with home and away exchanged.  Six outliers against 406 comparisons is
        1.5% overall and 100% of basketball, so only the per-sport rate can
        report it.

        Pinned because that rate is reached through a ``sport_of`` lookup keyed
        on the *same* tuple as ``groups``.  The two keys have to be built
        identically, and when they are not every lookup misses, every outlier is
        filed under sport ``""``, and the per-sport rate silently collapses back
        onto the overall one — the exact dilution above, restored by a typo.
        """
        from src.validation import Severity, validate

        rows = []
        for index in range(200):
            key = f"MLB-CIN@MLB-PHI:2026-08-{1 + index % 20}#{index}"
            for source in ("book_one", "book_two", "book_three"):
                for selection, odds in ((Selection.HOME, 1.50), (Selection.AWAY, 8.00)):
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"mlb{index}",
                        market=Market.MONEYLINE, selection=selection,
                        decimal_odds=odds, commence_time=LATER,
                        source_market_id=f"{source}-mlb{index}",
                    ))
        for index in range(3):
            key = f"NBA-LAL@NBA-BOS:2026-08-{1 + index}#{index}"
            for source in ("book_one", "book_two", "book_three"):
                for selection, odds in ((Selection.HOME, 1.50), (Selection.AWAY, 8.00)):
                    if source == "book_two":
                        odds = 8.00 if selection is Selection.HOME else 1.50
                    rows.append(make_quote(
                        source=source, event_key=key, source_event_id=f"nba{index}",
                        sport=Sport.BASKETBALL, league="NBA",
                        home_participant="NBA-BOS", away_participant="NBA-LAL",
                        home_team="Boston Celtics", away_team="Los Angeles Lakers",
                        market=Market.MONEYLINE, selection=selection,
                        decimal_odds=odds, commence_time=LATER,
                        source_market_id=f"{source}-nba{index}",
                    ))

        found = [
            f for f in validate(rows).findings
            if f.code == "prices_disagree_with_every_other_source"
        ]
        assert found, "a swap confined to one sport must still be reported"
        assert found[0].severity is Severity.ERROR
        assert found[0].source == "book_two"

    def test_two_sources_alone_are_not_judged(self) -> None:
        """Their median is their midpoint, so each is equally far from it and a
        flipped book would indict the honest one just as hard."""
        from src.validation import validate

        rows = [q for q in self._slate() if q.source != "book_three"]
        swapped = [
            q.model_copy(update={
                "decimal_odds": 1.80 if q.selection is Selection.HOME else 2.10,
                "implied_probability": 1 / (1.80 if q.selection is Selection.HOME else 2.10),
            }) if q.source == "book_two" else q
            for q in rows
        ]
        report = validate(swapped)
        assert not [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
        ]


class TestAStatisticIsNotAFixture:
    def test_a_stat_container_is_skipped_rather_than_rejected(self) -> None:
        """Two adapters, one shape, found one round apart.

        Books hang aggregate markets off a container built exactly like a
        fixture, with the statistics in the participant fields: Bovada sends
        ``"Away Total Runs @ Home Total Runs"``, Pinnacle ``"Home Runs (16
        Games)"`` against ``"Away Runs (16 Games)"`` — a figure aggregated across
        the whole day's slate.

        The parser was right to refuse both and wrong about what kind of refusal
        it was.  An unresolved *competitor* is a rejection, which marks the whole
        source unhealthy — one of these put Pinnacle's 29,711 good rows behind
        ``ok=0``.  A market this collector does not cover is a counted skip.

        The rule lives in :mod:`src.participants` so it is one rule rather than
        one per adapter.
        """
        from src.participants import is_statistic

        for label in (
            "Away Total Runs", "Home Total Runs",
            "Home Runs (16 Games)", "Away Runs (3 games)",
        ):
            assert is_statistic(label), label

        # And it must not catch a competitor.  "Homenetmen" is the trap the
        # word-boundary is there for.
        for real in (
            "Cincinnati Reds",
            "Philadelphia Phillies (A Nola)",
            "White Sox - Game 2",
            "Savannah Dada-Mascoll (Games)",
            "Homenetmen Beirut",
            "Away",
        ):
            assert not is_statistic(real), real

    def test_both_adapters_use_it(self) -> None:
        """The two that have met it in the wild."""
        import inspect

        from src.sources import bovada, pinnacle

        for module in (bovada, pinnacle):
            assert "is_statistic" in inspect.getsource(module), module.__name__


# ── round six ────────────────────────────────────────────────────────────────


class TestPinnacleRefusesAGameThatHasStarted:
    def test_a_matchup_whose_start_has_passed_is_skipped(self) -> None:
        """Pinnacle was the only source in this pipeline with no started-game
        guard, and the only one producing such rows.

        ``isLive`` is the venue's *product* flag — whether the matchup has moved
        to its in-play book — and that is not the same question as whether the
        game has begun.  On one live capture, **248 rows across 18 fixtures**
        carried a ``startTime`` in the past, every one of them ``isLive: false``
        and emitted as ``ACTIVE``: a tennis match two hours old published at
        1.01/32.47 and joined to another book's genuine pregame market.  No other
        source produced a single such row.

        ``src.arb`` has a gate of its own, but it runs on the earliest
        ``commence_time`` in a group and only at detection, so it cannot stop
        these rows being stored, compared, counted as coverage, or shown as
        prices anyone could take.
        """
        from src.sources.pinnacle import parse_pinnacle

        def slate(start: datetime):
            matchups = [{
                "id": 1, "type": "matchup", "isLive": False,
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "league": {"id": 246, "name": "MLB", "sport": {"id": 3}},
                "participants": [
                    {"alignment": "home", "name": "Cincinnati Reds"},
                    {"alignment": "away", "name": "Philadelphia Phillies"},
                ],
            }]
            markets = [{
                "matchupId": 1, "key": "s;0;m", "type": "moneyline", "period": 0,
                "status": "open", "cutoffAt": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "prices": [
                    {"designation": "home", "price": -110},
                    {"designation": "away", "price": -110},
                ],
            }]
            return [
                _raw("pinnacle", "matchups:league:MLB:246", matchups),
                _raw("pinnacle", "markets:league:MLB:246", markets),
            ]

        # FETCHED is the capture time every ``_raw`` here carries.
        future = parse_pinnacle(slate(FETCHED + timedelta(hours=6)))
        assert future.quotes, future.rejections
        assert not future.skipped.get("event_already_started")

        started = parse_pinnacle(slate(FETCHED - timedelta(hours=2)))
        assert not started.quotes
        assert started.skipped["event_already_started"] == 1
        assert not started.rejections


class TestSxBetTypesAreReadPerSport:
    def test_the_same_code_is_two_different_contracts_in_two_sports(self) -> None:
        """SX Bet's ``type`` is not a global vocabulary, and the collision is
        live.  From a census of 1,704 market records: ``52`` in tennis is the
        match winner — ``("Adrian Mannarino", "Learner Tien")``, no line — and
        tennis has no draw, so it is a moneyline.  ``52`` in **soccer** is also
        two team names with no line — ``("Lech Poznan", "Aarhus AGF")`` — but
        soccer does have a draw, so it is a draw-loses two-way and emphatically
        not a moneyline.

        One flat table could hold only one of them.  It held the US-sport codes,
        so the whole soccer and tennis book was fetched and discarded while
        ``capabilities()`` promised eighteen leagues and the parse delivered
        three.
        """
        from src.schema import Sport as _Sport
        from src.sources.sxbet import MARKET_TYPES, READABLE_SPORTS

        assert (_Sport.TENNIS, 52) in MARKET_TYPES
        assert MARKET_TYPES[(_Sport.TENNIS, 52)].market is Market.MONEYLINE
        # Soccer is not read at all, so its 52 cannot be mistaken for one.
        assert _Sport.SOCCER not in READABLE_SPORTS
        assert not [key for key in MARKET_TYPES if key[0] is _Sport.SOCCER]
        # ...and a US-sport code does not leak into tennis.
        assert (_Sport.TENNIS, 226) not in MARKET_TYPES

    def test_it_declares_only_what_it_can_read(self) -> None:
        """A declared-but-empty league is worse than an absent one: it is a
        promise the coverage check cannot audit, because a ``(source, sport)``
        pair with no rows never enters the comparison."""
        from src.leagues import league as get_league
        from src.sources.registry import descriptor
        from src.sources.sxbet import READABLE_SPORTS

        source = descriptor("sxbet").replay_instance()
        assert all(
            get_league(key).sport in READABLE_SPORTS for key in source.leagues
        ), source.leagues


class TestANonFiniteBodyKeepsItsStatusClassification:
    def test_a_rate_limit_is_still_a_rate_limit(self) -> None:
        """Refusing bare ``NaN``/``Infinity`` is right, and the raise was placed
        above every status-derived refusal — so a 429 whose body happened to
        carry one stopped being retryable and lost the ``Retry-After`` the
        server sent."""
        from src.sources.guards import (
            FormatChangeError,
            GeoRestrictedError,
            RateLimitedError,
            ServerError,
            check_http_response,
        )

        def refuse(status: int, body: str):
            with pytest.raises(Exception) as caught:
                check_http_response(
                    source="s", endpoint="e", status_code=status, body=body,
                    content_type="application/json", retry_after="30",
                )
            return caught.value

        limited = refuse(429, '{"backoff": NaN}')
        assert isinstance(limited, RateLimitedError)
        assert limited.retryable and limited.retry_after == 30.0
        assert isinstance(refuse(503, '{"load": Infinity}'), ServerError)
        assert isinstance(refuse(451, '{"x": NaN}'), GeoRestrictedError)
        # A 200 that is unusable is a format change, and not a block page: the
        # loose marker scan below must never see a real payload.
        assert isinstance(refuse(200, '{"a": NaN}'), FormatChangeError)
        assert isinstance(
            refuse(200, '{"a": NaN, "u": "https://cdn.cloudflare.com/x"}'),
            FormatChangeError,
        )


class TestACollapseIsGradedAgainstWhatCoversTheScope:
    def test_a_narrowed_run_still_notices_losing_most_of_its_books(self) -> None:
        """The narrowing exemption was blanket, so ``collect --sport baseball``
        could lose eight of the ten books that serve baseball and exit 0.

        Graded against the sources that *declared* they cover the scope instead:
        a hockey-only run expects the three that serve hockey, and a
        baseball-only run expects all ten.
        """
        from src.collector import LeagueCoverage, _check_source_health, _sources_covering
        from src.sources.base import SourceHealth
        from src.validation import Severity, ValidationReport

        coverage = [
            LeagueCoverage(source_key=f"book_{i}", league="MLB", sport="baseball",
                           quote_count=0, event_count=0)
            for i in range(10)
        ] + [
            LeagueCoverage(source_key=f"book_{i}", league="NHL", sport="hockey",
                           quote_count=0, event_count=0)
            for i in range(3)
        ]
        configured = [f"book_{i}" for i in range(10)]
        health = [
            SourceHealth(source_key=key, ok=True, checked_at=FETCHED,
                         quote_count=100 if index < 2 else 0)
            for index, key in enumerate(configured)
        ]

        def grade(sports):
            report = ValidationReport()
            _check_source_health(
                health, report, configured=configured,
                expected=_sources_covering(coverage, sports, None),
                narrowed=bool(sports),
            )
            return [
                f for f in report.findings
                if f.code == "configured_sources_produced_nothing"
                and f.severity is Severity.ERROR
            ]

        # Two of the ten that serve baseball answered: a collapse.
        assert grade(["baseball"])
        # Two of the three that serve hockey answered: a thin slate, not a fault.
        assert not grade(["hockey"])

        # And a narrowed run whose relevant sources all answered is not a
        # collapse, however many of the others sat it out.  An earlier version
        # added every source missing from the *scope-filtered* coverage back into
        # the denominator, which failed `--league TENNIS_OTHER` on a run where
        # all three sources that claim tennis had answered.
        report = ValidationReport()
        _check_source_health(
            [
                SourceHealth(source_key=key, ok=True, checked_at=FETCHED,
                             quote_count=100 if index < 3 else 0)
                for index, key in enumerate(configured)
            ],
            report,
            configured=configured,
            expected={"book_0", "book_1", "book_2"},
            narrowed=True,
        )
        assert not [
            f for f in report.findings
            if f.code == "configured_sources_produced_nothing"
            and f.severity is Severity.ERROR
        ]


# ── round seven ──────────────────────────────────────────────────────────────


class TestMatchbookReadsTheUnconditionalPriceField:
    def test_the_decimal_field_is_preferred_over_the_typed_one(self) -> None:
        """``price["odds"]`` means whatever the sibling ``price["odds-type"]``
        says, and that field was never read.

        Both are on all 17,057 captured prices and agree exactly today, because
        ``odds-type`` is ``DECIMAL`` on every one — but a response in another
        format would be read as decimal in silence, and the plausibility band
        cannot catch it.  AMERICAN is the dangerous direction: a true decimal
        4.00 arrives as ``+300`` and is recorded as decimal **300.0**, an implied
        probability of 0.33% — instant phantom arbitrage on every underdog, and
        3,552 of the capture's 5,480 positive-American values land inside the
        band.  ``decimal-odds`` is unconditional, so reading it removes the
        dependency rather than checking it.
        """
        from src.sources.matchbook import _best_back

        runner = {"prices": [{
            "side": "back", "odds": 300.0, "decimal-odds": 4.0,
            "odds-type": "AMERICAN", "currency": "USD", "available-amount": 500.0,
        }]}
        assert _best_back(runner) == (4.0, 500.0)

    def test_the_fallback_refuses_a_format_it_cannot_read(self) -> None:
        """Falling back to ``odds`` when ``decimal-odds`` is absent reinstated
        the whole hazard: an AMERICAN ``+300`` reads as decimal 300.0, an implied
        0.33%, and passes the plausibility band."""
        from collections import Counter

        from src.sources.matchbook import _best_back

        counts: Counter[str] = Counter()
        american = {"prices": [{
            "side": "back", "odds": 300.0, "odds-type": "AMERICAN",
            "currency": "USD", "available-amount": 500.0,
        }]}
        assert _best_back(american, outcome_counts=counts) is None
        assert counts["price_in_an_unreadable_odds_type"] == 1
        # ...and the same shape in decimal is still read.
        decimal = {"prices": [{
            "side": "back", "odds": 4.0, "odds-type": "DECIMAL",
            "currency": "USD", "available-amount": 500.0,
        }]}
        assert _best_back(decimal) == (4.0, 500.0)

    def test_a_price_whose_size_cannot_be_compared_is_not_emitted(self) -> None:
        """``available-amount`` becomes ``limit_amount``, and ``src.arb`` takes a
        ``min()`` of those across venues as one bankroll cap."""
        from src.sources.matchbook import _best_back

        from collections import Counter

        counts: Counter[str] = Counter()
        runner = {"prices": [{
            "side": "back", "odds": 4.0, "decimal-odds": 4.0, "odds-type": "DECIMAL",
            "currency": "EUR", "available-amount": 500.0,
        }]}
        # Refused, not silently unsized.  ``None`` means *unknown*, and
        # ``src.arb`` reports a cap only when some leg states one — so on a
        # two-leg position with one exchange leg, dropping the size removes the
        # cap *and* the note explaining it, and a position backed by a $40 order
        # book is presented as unbounded.
        assert _best_back(runner, outcome_counts=counts) is None
        assert counts["price_in_another_currency"] == 1

        # Compared against the currency this instance asked for, not a module
        # constant — an adapter built for GBP would otherwise discard every
        # price it collected.
        assert _best_back(runner, currency="EUR") == (4.0, 500.0)

        # A comparable price alongside an incomparable one is still used.
        mixed = {"prices": [
            {"side": "back", "decimal-odds": 2.0, "odds-type": "DECIMAL",
             "currency": "USD", "available-amount": 500.0},
            runner["prices"][0],
        ]}
        assert _best_back(mixed) == (2.0, 500.0)


class TestACompetitionCanCarryTheIdentityMarker:
    def test_a_womens_competition_does_not_key_as_the_mens_fixture(self) -> None:
        """Pinnacle routes every unrecognised soccer competition into one
        catch-all league, and the competition name was then read by nothing — so
        "Club Friendlies Women" Utrecht v De Graafschap produced an ``event_key``
        byte-identical to the men's fixture.  24 matchups across 14 competitions
        on one live slate.

        Nothing downstream recovers from that: ``dedup_key`` includes ``source``
        so the storage constraint is blind across sources, the participant pair
        is identical so ``participant_pair_disagreement`` cannot fire, and
        soccer's 30-hour tolerance clusters an afternoon women's game with an
        evening men's one.  Injecting one men's fixture beside the real women's
        rows produced an **11.2% "guaranteed" position ranked first in the whole
        run**.
        """
        from src.participants import competition_marker

        assert competition_marker("Iceland - Urvalsdeild Women") == "w"
        assert competition_marker("CONCACAF - U20 Championship") == "u20"
        assert competition_marker("Uruguay - Reserve League") == "reserves"
        assert competition_marker("Colombia - U19 Championship") == "u19"
        # ...and a senior men's competition is untouched.
        for senior in ("England - Premier League", "Spain - La Liga", "Brazil - Serie A"):
            assert competition_marker(senior) is None, senior

    def test_an_individual_sport_is_never_marked(self) -> None:
        """The marker exists because one *club* fields a men's, a women's and a
        reserve side under one name.  A player is not like that — Polona Hercog
        is Polona Hercog whether the draw is called "ITF Women" or not — and
        marking her split 50 tennis fixtures away from every other book.  That
        happened, and this is the regression for it.
        """
        from src.participants import competition_marker

        assert competition_marker("ITF Women Monastir", Sport.TENNIS) is None
        assert competition_marker("W15 Naiman", Sport.TENNIS) is None
        # The same name in a club sport still marks.
        assert competition_marker("Iceland - Urvalsdeild Women", Sport.SOCCER) == "w"
        # With no sport stated the marker still applies, since the caller that
        # omits it is not an adapter.
        assert competition_marker("Iceland - Urvalsdeild Women") == "w"

    def test_the_marker_reaches_the_participant_key(self) -> None:
        from src.leagues import league as get_league
        from src.participants import canonical_participant, competition_marker

        soccer = get_league("SOCCER_OTHER")
        marker = competition_marker("Club Friendlies Women")
        mens = canonical_participant("Utrecht", soccer)
        womens = canonical_participant(f"Utrecht {marker}", soccer)
        assert mens is not None and womens is not None
        assert mens.key != womens.key


class TestThePriceCheckSeesEveryShapeOfMirror:
    #: A lopsided pair, so mirroring it moves probability far enough to be an
    #: outlier — which is the point: books differ by a point or two, and a
    #: mirror moves the whole mass.
    FAVOURITE, LONGSHOT = 1.25, 5.00

    def _slate(self, sports=("baseball", "basketball"), per_sport: int = 60,
               sources=("thin", "book_two", "book_three")):
        rows = []
        for sport in sports:
            league = "MLB" if sport == "baseball" else "NBA"
            for index in range(per_sport):
                for source in sources:
                    for selection, odds in (
                        (Selection.HOME, self.FAVOURITE),
                        (Selection.AWAY, self.LONGSHOT),
                    ):
                        rows.append(make_quote(
                            source=source, sport=Sport(sport), league=league,
                            event_key=f"{league}-CIN@{league}-PHI:2026-07-29#{index}",
                            source_event_id=f"{sport}{index}",
                            home_participant=f"{league}-PHI", away_participant=f"{league}-CIN",
                            selection=selection, decimal_odds=odds,
                            commence_time=LATER,
                            source_market_id=f"{source}-{sport}{index}",
                        ))
        return rows

    def _mirror(self, rows, source: str, sport: str | None = None):
        out = []
        for quote in rows:
            if quote.source != source or (sport and quote.sport.value != sport):
                out.append(quote)
                continue
            odds = (
                self.LONGSHOT if quote.selection is Selection.HOME else self.FAVOURITE
            )
            out.append(quote.model_copy(update={
                "decimal_odds": odds, "implied_probability": 1 / odds,
            }))
        return out

    def test_a_thin_source_is_not_exempt(self) -> None:
        """A *market-count* floor was the first attempt and was worse than the
        problem it solved.  At 100 it exempted Smarkets permanently — 46 compared
        markets — from the only check that can see a source whose prices are
        attached to the wrong side while its participants are correct.  Mirroring
        Smarkets produced 12 positions up to a 24.97% "guaranteed" margin and a
        byte-identical validation report.
        """
        from src.validation import Severity, validate

        # "thin" prices only 20 fixtures where the others price 120 — far below
        # any market-count floor, and far above the outlier floor.
        full = self._slate(sports=("baseball",), per_sport=120)
        thin = [
            q for q in full
            if q.source != "thin" or int(q.source_event_id.removeprefix("baseball")) < 20
        ]
        report = validate(self._mirror(thin, "thin"))
        found = [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source" and f.source == "thin"
        ]
        assert found and found[0].severity is Severity.ERROR

    def test_a_source_mirrored_in_one_sport_is_not_diluted(self) -> None:
        """Orientation was given a per-sport grade for this reason and the price
        check was not — in the check that is the only defence against a price
        mirror.  One source mirrored in basketball alone is 17% of its basketball
        markets and 1.99% overall, just under a 2% bar, for 58 phantom positions
        and no finding."""
        from src.validation import Severity, validate

        rows = self._slate(per_sport=120)
        report = validate(self._mirror(rows, "book_two", sport="basketball"))
        found = [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
            and f.source == "book_two"
        ]
        assert found and found[0].severity is Severity.ERROR

    def test_an_honest_slate_stays_clean(self) -> None:
        from src.validation import validate

        assert not [
            f for f in validate(self._slate()).findings
            if f.code == "prices_disagree_with_every_other_source"
        ]


class TestABodilessServerErrorIsRetried:
    def test_a_5xx_with_no_body_is_an_outage_not_an_off_day(self) -> None:
        """``EmptyResponseError`` is documented as "well formed but carried no
        usable data" and is **not retryable**, so a bodiless 502/503 from a CDN —
        which is ordinary — got exactly one attempt and read downstream as a
        quiet slate."""
        from src.sources.guards import (
            EmptyResponseError,
            HttpStatusError,
            ServerError,
            check_http_response,
        )

        def refuse(status: int, body: str = ""):
            with pytest.raises(Exception) as caught:
                check_http_response(
                    source="s", endpoint="e", status_code=status, body=body,
                    content_type="application/json",
                )
            return caught.value

        for status in (500, 502, 503):
            failure = refuse(status)
            assert isinstance(failure, ServerError) and failure.retryable, status
        assert refuse(502, "   ").retryable
        # A 4xx with no body is still not retryable, and a 200 is still empty.
        assert isinstance(refuse(404), HttpStatusError)
        assert not refuse(404).retryable
        assert isinstance(refuse(200), EmptyResponseError)


# ── round eight ──────────────────────────────────────────────────────────────


class TestTheSettlementRetryRefusesWhatThePrimaryPathWould:
    """The two rejection branches of the cross-regime rebuild, neither of which
    had a test.

    When the cheapest legs span two settlement regimes the position is rebuilt
    from the largest regime present — a fix for adding a venue *destroying* a
    real position.  The rebuild then has to re-apply the gates the primary path
    already passed, and both re-applications were regressions with no regression
    test: delete either ``continue`` and the suite stays green while a
    ten-hour-stale leg is reported as a 16.67% risk-free position.
    """

    def _legs(self, *, stale: timedelta = timedelta(0)):
        """Kalshi holds the best HOME, so the *primary* assignment is
        kalshi+pinnacle — cross-regime, and both fresh, so it clears the primary
        freshness gate.  The rebuild then has only fanduel's HOME to fall back
        on, and that is the leg made stale."""
        seen = FETCHED - timedelta(minutes=1)
        return [
            make_quote(source="kalshi", selection=Selection.HOME, decimal_odds=2.40,
                       observed_at=seen),
            make_quote(source="pinnacle", selection=Selection.AWAY, decimal_odds=2.40,
                       observed_at=seen),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.20,
                       observed_at=seen - stale),
        ]

    def test_the_rebuilt_legs_must_also_be_fresh(self) -> None:
        """Kalshi's only role is making the *primary* assignment cross-regime;
        the fallback then picked two book legs ten hours apart."""
        fresh = find_opportunities(self._legs(), as_of=FETCHED)
        assert fresh.opportunities, [d.code for d in fresh.diagnostics]

        stale = find_opportunities(self._legs(stale=timedelta(hours=10)), as_of=FETCHED)
        assert not stale.opportunities
        assert [d.code for d in stale.diagnostics] == ["stale_leg"]
        assert "one settlement regime" in stale.diagnostics[0].detail

    def test_the_rebuilt_margin_must_clear_the_same_bar(self) -> None:
        """The retry used ``<`` where the primary path uses ``<=``, so it
        admitted a zero-edge position the primary path refuses."""
        quotes = [
            make_quote(source="kalshi", selection=Selection.HOME, decimal_odds=2.40),
            make_quote(source="matchbook", selection=Selection.AWAY, decimal_odds=2.40),
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=1.90),
        ]
        refused = find_opportunities(quotes, as_of=FETCHED, min_margin=0.10)
        assert not refused.opportunities
        assert [d.code for d in refused.diagnostics] == ["legs_do_not_void_together"]
        assert "does not clear" in refused.diagnostics[0].detail

        # The control: the same rebuild under a bar it does clear.
        allowed = find_opportunities(quotes, as_of=FETCHED, min_margin=0.01)
        assert allowed.opportunities
        assert {leg.source for leg in allowed.opportunities[0].legs} == {
            "matchbook", "fanduel"
        }


class TestAPartialMirrorIsReportedAndActedOn:
    def test_the_per_league_branch_fires_and_reaches_the_detector(self) -> None:
        """``_check_distinctness`` has two branches and only the full-mirror one
        was tested — while the *partial* one is what fires in production.

        It is what populates ``counterparty_groups``, which is what stops
        ``find_opportunities`` reporting an arbitrage between one book and
        itself.  Had it regressed to an empty ``mirrored_leagues`` the suite
        would have stayed green while the pipeline reported risk-free positions
        between the two Kambi tenants across every tennis tour they share.
        """
        from src.arb import counterparty_groups
        from src.collector import _check_distinctness
        from src.validation import Severity, ValidationReport

        rows = []
        for index in range(25):
            shared = dict(
                sport=Sport.TENNIS, league="ITF",
                event_key=f"TENNIS-a{index}@TENNIS-b{index}:2026-07-29",
                source_event_id=f"t{index}",
                home_participant=f"TENNIS-b{index}", away_participant=f"TENNIS-a{index}",
                home_team=f"B {index}", away_team=f"A {index}",
                selection=Selection.HOME, commence_time=LATER,
            )
            # One feed in ITF...
            for source in ("tenant_a", "tenant_b"):
                rows.append(make_quote(source=source, decimal_odds=1.80, **shared))
            # ...and two books in baseball.
            for source, odds in (("tenant_a", 1.90), ("tenant_b", 2.05)):
                rows.append(make_quote(
                    source=source, league="MLB", decimal_odds=odds,
                    event_key=f"MLB-CIN@MLB-PHI:2026-07-{10 + index % 20}#{index}",
                    source_event_id=f"m{index}", selection=Selection.HOME,
                    commence_time=LATER,
                ))

        report = ValidationReport()
        _check_distinctness(rows, report)
        codes = [(f.severity, f.code) for f in report.findings]
        assert (Severity.WARNING, "sources_are_one_counterparty_in_some_leagues") in codes
        # ...and it is not the run-failing full-mirror grade.
        assert not any(code == "sources_are_one_counterparty" for _, code in codes)

        from src.arb import EVERY_LEAGUE

        groups = counterparty_groups(rows)
        # These two lines used to read ``groups.get("ITF") == [...]`` and
        # ``"MLB" not in groups`` — they pinned the gate to the leagues the
        # mirror was *measured* in, which is the defect
        # :class:`TestOneBookIsOneCounterpartyInEveryLeague` covers.  Two
        # licences of one operator are one counterparty in MLB too; what MLB
        # lacks is evidence, not independence.
        assert groups == {EVERY_LEAGUE: [frozenset({"tenant_a", "tenant_b"})]}


class TestTheErrorsNoTestHadEverEmitted:
    def test_a_zero_american_price_is_refused(self) -> None:
        from src.validation import Severity, validate

        row = make_quote(decimal_odds=2.0).model_copy(update={"american_odds": 0})
        found = [f for f in validate([row]).findings if f.code == "zero_american_odds"]
        assert found and found[0].severity is Severity.ERROR

    def test_two_sources_disagreeing_about_the_sport_is_refused(self) -> None:
        from src.validation import Severity, validate

        rows = [
            make_quote(source="book_a", selection=Selection.HOME),
            make_quote(source="book_b", selection=Selection.AWAY).model_copy(
                update={"sport": Sport.BASKETBALL}
            ),
        ]
        found = [f for f in validate(rows).findings if f.code == "sport_disagreement"]
        assert found and found[0].severity is Severity.ERROR


class TestALetterIsFoldedNotDeleted:
    def test_a_name_nfkd_cannot_decompose_keeps_its_letters(self) -> None:
        """``_fold`` NFKD-normalises and drops combining marks, which folds
        ``é`` onto ``e``.  A letter that is *not* a base plus a mark survives
        NFKD unchanged and was then removed by the alphanumeric filter — which
        does not fold it, it **deletes** it.

        ``Ægir`` became ``SOCCER-gir``: a four-letter name reduced to three, and
        a short mangled stem is the most collision-prone thing this module can
        produce.  Every mapping below is the spelling another source in this
        pipeline already uses, so this recovers real joins rather than inventing
        them.
        """
        from src.participants import resolve_open

        for exotic, plain in (
            ("Tromsø", "Tromso"),
            ("FC Nordsjælland", "FC Nordsjaelland"),
            ("Ægir", "Aegir"),
            ("UMFN Njarðvik", "UMFN Njardvik"),
            ("Þór", "Thor"),
            ("Łódź", "Lodz"),
        ):
            left = resolve_open(exotic, Sport.SOCCER)
            right = resolve_open(plain, Sport.SOCCER)
            assert left is not None and right is not None, exotic
            assert left.key == right.key, (exotic, left.key, right.key)
            # ...and no letter was dropped on the way.
            assert len(left.abbr) >= len(exotic.replace(" ", "")) - 3, exotic

    def test_the_accent_folding_it_was_written_for_still_works(self) -> None:
        from src.participants import resolve_open

        assert (
            resolve_open("Andrés Martín", Sport.TENNIS).key
            == resolve_open("Andres Martin", Sport.TENNIS).key
        )


class TestAStateCodeIsNotAClubType:
    def test_two_clubs_in_two_states_stay_apart(self) -> None:
        """One live capture carries ``Botafogo RJ`` at one book and ``Botafogo
        SP`` at four others — two different clubs.  Any rule that strips a
        two-letter Brazilian state suffix merges them, and the merge would carry
        prices from two unrelated fixtures under one event key.
        """
        from src.participants import (
            _CLUB_TYPE_TOKENS,
            _STATE_CODES_NOT_CLUB_TYPES,
            resolve_open,
        )

        assert not (_CLUB_TYPE_TOKENS & _STATE_CODES_NOT_CLUB_TYPES)
        key = lambda name: resolve_open(name, Sport.SOCCER).key  # noqa: E731
        assert key("Botafogo RJ") != key("Botafogo SP")
        assert key("Botafogo SP") == key("Botafogo-SP")
        assert key("Juventude RS") != key("Juventude")

    def test_the_club_type_prefixes_still_merge(self) -> None:
        from src.participants import resolve_open

        key = lambda name: resolve_open(name, Sport.SOCCER).key  # noqa: E731
        for decorated, plain in (
            ("KF Drita", "Drita"),
            ("CS Universitatea Craiova", "Universitatea Craiova"),
            ("AS Roma", "Roma"),
            ("KAA Gent", "Gent"),
        ):
            assert key(decorated) == key(plain), decorated


class TestTheTourMarkerIsFoundWhereverTheVenuePutsIt:
    def test_a_challenger_written_as_a_suffix_is_a_challenger(self) -> None:
        """Prefix matching filed every Challenger event as a main-tour one,
        because that is not where these venues put the word: Matchbook writes
        ``"ATP Vancouver Challenger"`` and SX Bet ``"Vancouver Challenger ATP"``
        — 31 of Matchbook's 83 tennis events, and every SX Bet Challenger market.
        Matchbook's parsed tennis came out ``{WTA, ATP}`` where Pinnacle, on the
        same slate, separates four tours.
        """
        from src.sources.matchbook import _league_for as matchbook_league
        from src.sources.sxbet import _league_for as sxbet_league

        assert matchbook_league(Sport.TENNIS, "ATP Vancouver Challenger") == "ATP_CHALLENGER"
        assert matchbook_league(Sport.TENNIS, "ATP San Marino Challenger") == "ATP_CHALLENGER"
        assert sxbet_league(Sport.TENNIS, "Vancouver Challenger ATP") == "ATP_CHALLENGER"

        # ...and a *women's* Challenger is not the men's tour.  One ordered list
        # of markers cannot say that: testing "challenger" before "wta" filed
        # "Vancouver Challenger WTA" — 252 markets on one capture — as
        # ATP_CHALLENGER, and produced a fresh disagreement with Matchbook,
        # which calls the same event WTA.  The tour is decided first and the tier
        # only narrows it.
        for women in ("Vancouver Challenger WTA", "Targu Mures Challenger WTA"):
            assert sxbet_league(Sport.TENNIS, women) == "WTA", women
            assert matchbook_league(Sport.TENNIS, women) == "WTA", women

        # The main tours are still themselves.
        assert matchbook_league(Sport.TENNIS, "ATP Los Cabos") == "ATP"
        assert matchbook_league(Sport.TENNIS, "WTA Vancouver 125K Series") == "WTA"
        assert sxbet_league(Sport.TENNIS, "Memphis WTA") == "WTA"


class TestADeclaredSportThatReturnedNothingIsReported:
    def test_a_whole_sport_disappearing_is_no_longer_silent(self) -> None:
        """``_check_coverage`` indexed only the sports that *arrived*, so a
        ``(source, sport)`` with no rows was not a key and the loop never ran for
        it.  Ten such pairs sat in one live capture — Smarkets returning nothing
        for four of its six sports among them — and validation reported PASS with
        no finding naming any of them.

        The check's own docstring says it exists to catch "an upstream rename
        that turns a real market into a silent skip"; a rename that takes a whole
        sport is exactly that, and was the one shape it could not see.
        """
        from src.validation import Severity, validate

        rows = [
            make_quote(source="book_a", selection=Selection.HOME),
            make_quote(source="book_a", selection=Selection.AWAY),
        ]
        capabilities = {
            ("book_a", "MLB"): frozenset({Market.MONEYLINE}),
            ("book_a", "EPL"): frozenset({Market.MONEYLINE}),
        }
        report = validate(rows, capabilities=capabilities)
        found = [
            f for f in report.findings if f.code == "declared_sport_produced_nothing"
        ]
        assert found, [f.code for f in report.findings]
        assert found[0].severity is Severity.WARNING
        assert "soccer" in found[0].message and found[0].source == "book_a"

        # A sport that did produce rows says nothing.
        assert not [
            f for f in validate(rows, capabilities={("book_a", "MLB"): frozenset()}).findings
            if f.code == "declared_sport_produced_nothing"
        ]


# ── round ten ────────────────────────────────────────────────────────────────


class TestARefusedScopeIsVisibleInTheRun:
    def test_one_league_answering_429_is_not_a_healthy_book(self, tmp_path) -> None:
        """``ScopeTally`` counted every refusal and nothing could read the count.

        Its only reader is ``require_something``, which consults it **solely
        when every scope came back empty** — so one surviving league discarded
        the lot.  All eight adapters do the same thing on a per-scope failure:
        log it, tally it, continue.  Downstream, ``request_count`` has nothing to
        be compared against and ``RunResult.ok`` reads the validation report
        alone.

        Measured on real captured bytes with one league answering 429: the book
        lost 14% of its rows and summarised as ``OK (701 quotes, 12 requests)``
        with no finding, no error and exit 0.
        """
        import httpx

        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.sources.bovada import BovadaAdapter
        from src.store import Store
        from src.validation import Severity

        captured = {
            json.load(open(path))["endpoint"]: json.load(open(path))["body"]
            for path in glob.glob("tests/fixtures/raw/bovada__*.json")
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            for endpoint, body in captured.items():
                if endpoint.split(":", 1)[1].strip("/") in path:
                    if "premier-league" in path:
                        return httpx.Response(429, json={"error": "slow down"})
                    return httpx.Response(200, content=body)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        source = BovadaAdapter(["MLB", "EPL", "NFL"], client=client)
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [source], raw_store=RawStore(tmp_path / "raw"), store=store
            )

        health = next(h for h in result.health if h.source_key == "bovada")
        # It kept the leagues it could read...
        assert health.quote_count and health.ok
        # ...and the one it could not is named, counted, and said out loud.
        assert len(health.failed_scopes) == 1
        assert "premier-league" in health.failed_scopes[0]
        assert "SCOPE(S) REFUSED" in health.summary()
        refused = [f for f in result.report.findings if f.code == "scopes_refused"]
        assert refused and refused[0].severity is Severity.WARNING
        assert refused[0].source == "bovada"

    def test_a_book_that_lost_nothing_says_nothing(self, tmp_path) -> None:
        import httpx

        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.sources.bovada import BovadaAdapter
        from src.store import Store

        captured = {
            json.load(open(path))["endpoint"]: json.load(open(path))["body"]
            for path in glob.glob("tests/fixtures/raw/bovada__*.json")
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            for endpoint, body in captured.items():
                if endpoint.split(":", 1)[1].strip("/") in path:
                    return httpx.Response(200, content=body)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        source = BovadaAdapter(["MLB"], client=client)
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [source], raw_store=RawStore(tmp_path / "raw"), store=store
            )
        health = next(h for h in result.health if h.source_key == "bovada")
        assert health.failed_scopes == ()
        assert not [f for f in result.report.findings if f.code == "scopes_refused"]


class TestAWomensChallengerIsNotTheMensTour:
    def test_the_tour_is_decided_before_the_tier(self) -> None:
        """One ordered list of markers cannot express a tour *and* a tier.

        Matching "challenger" before "wta" filed every women's Challenger —
        ``"Vancouver Challenger WTA"``, 252 markets on one capture — as
        ``ATP_CHALLENGER``, the men's tour, and created a fresh cross-source
        disagreement with Matchbook, which calls the same event ``WTA``.

        There is no ``WTA_CHALLENGER`` key, so a women's Challenger stays
        ``WTA``: the same governing body at a lower tier is a far smaller error
        than the wrong tour, and it agrees with what the other sources say.
        """
        from src.sources.matchbook import _league_for as matchbook_league
        from src.sources.sxbet import _league_for as sxbet_league

        for resolve in (matchbook_league, sxbet_league):
            assert resolve(Sport.TENNIS, "Vancouver Challenger WTA") == "WTA"
            assert resolve(Sport.TENNIS, "WTA Vancouver 125K Series") == "WTA"
            assert resolve(Sport.TENNIS, "Vancouver Challenger ATP") == "ATP_CHALLENGER"
            assert resolve(Sport.TENNIS, "ATP Vancouver Challenger") == "ATP_CHALLENGER"
            assert resolve(Sport.TENNIS, "ITF Monastir") == "ITF"
            # A bare tier marker with no tour named is a men's Challenger by
            # convention; the women's tour always names itself.
            assert resolve(Sport.TENNIS, "Bonn Challenger") == "ATP_CHALLENGER"


class TestAScopeThatFailsPartWayKeepsWhatItFetched:
    def test_page_one_survives_page_two_failing(self) -> None:
        """Every multi-request scope built its pages in a local list and the
        caller caught ``SourceError`` around the whole helper, so requests 1..N-1
        were paid for out of somebody else's bandwidth and thrown away.

        Measured on real adapters: Kalshi losing one series' second page
        discarded 4 of 6 responses and the entire MLB totals market, on a run
        that reported ``ok=True`` with no error at all.  Polymarket losing its
        MLB page 2 discarded every Polymarket MLB row the same way.
        """
        import httpx

        from src.sources.polymarket import PolymarketAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params.get("offset", "0"))
            tag = request.url.params.get("tag_slug", "")
            if tag == "mlb" and offset > 0:
                return httpx.Response(500, json={"error": "boom"})
            # A full first page, so the loop asks for a second.
            return httpx.Response(200, json=[
                {"id": str(index), "slug": f"{tag}-{index}", "title": "x",
                 "startTime": "2026-08-01T18:00:00Z", "markets": []}
                for index in range(40)
            ])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        source = PolymarketAdapter(["MLB", "NFL"], client=client)
        kept = {raw.endpoint for raw in source.fetch_raw()}

        # The page that arrived before the failure is still here...
        assert "events:mlb:01" in kept
        # ...the scope that worked is untouched...
        assert {"events:nfl:01", "events:nfl:02"} <= kept
        # ...and the refusal is named rather than swallowed.
        assert any("mlb" in scope for scope in source.last_fetch.failed_scopes)


class TestSxBetDoesNotFetchASportItCannotRead:
    def test_the_fetch_loop_agrees_with_the_capability_claim(self) -> None:
        """``capabilities()`` stopped claiming soccer when the market table was
        keyed by sport; the fetch loop kept asking for it.  The adapter spent 9
        metadata pages and 466 KB — 24% of its whole pass — on a sport whose
        every record is discarded on arrival, and requested no order books for
        it at all."""
        from src.sources.registry import descriptor
        from src.sources.sxbet import READABLE_SPORTS

        source = descriptor("sxbet").replay_instance()
        assert set(source.sports) <= READABLE_SPORTS
        assert Sport.SOCCER not in source.sports
        # ...and it still fetches everything it does read.
        assert Sport.TENNIS in source.sports and Sport.BASEBALL in source.sports


class TestSmarketsFollowsTheCursorItActuallySends:
    def test_a_query_only_next_page_is_a_cursor_not_a_failure(self) -> None:
        """The guard written to stop silent truncation was causing total loss.

        Smarkets' ``pagination.next_page`` is a bare **query string** — a
        query-only relative reference, "same path, this query":

            ?state=upcoming&type=football_match&limit=100&pagination_last_id=45164938

        The round-5 guard accepted only an absolute URL or a leading ``/`` and
        raised on anything else, so soccer, tennis and football each returned a
        full first page, a cursor, and then nothing: **three whole sports lost on
        every run**, invisibly, because a refused scope reached a log line and
        stopped there.  It surfaced the moment the health row learned to carry
        refusals.
        """
        import httpx

        from src.sources.smarkets import SmarketsAdapter

        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            last = request.url.params.get("pagination_last_id")
            events = [
                {"id": str(1000 + index), "name": f"A{index} vs B{index}",
                 "state": "upcoming", "bettable": True,
                 "start_datetime": "2026-08-01T18:00:00Z",
                 "full_slug": f"/sport/football/epl/2026/08/01/18-00/a{index}-b{index}"}
                for index in range(100)
            ]
            return httpx.Response(200, json={
                "events": [] if last else events,
                "pagination": {
                    "next_page": None if last else
                    "?state=upcoming&type=football_match&limit=100"
                    "&pagination_last_id=45164938"
                },
            })

        client = httpx.Client(transport=httpx.MockTransport(handler))
        source = SmarketsAdapter(["EPL"], client=client)
        source.fetch_raw()

        assert source.last_fetch.failed_scopes == []
        # The cursor was followed, and with its own query rather than ours.
        assert any("pagination_last_id=45164938" in url for url in requested)

    def test_a_token_that_is_genuinely_unreadable_is_still_refused(self) -> None:
        """The guard's purpose stands: a token this adapter cannot turn into a
        request must fail loudly, not return a short slate."""
        import httpx

        from src.sources.guards import SourceError
        from src.sources.smarkets import SmarketsAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "events": [
                    {"id": str(index), "name": f"A{index} vs B{index}",
                     "state": "upcoming", "bettable": True,
                     "start_datetime": "2026-08-01T18:00:00Z",
                     "full_slug": f"/sport/football/epl/2026/08/01/18-00/a{index}-b{index}"}
                    for index in range(100)
                ],
                "pagination": {"next_page": "OPAQUE-CURSOR-TOKEN"},
            })

        client = httpx.Client(transport=httpx.MockTransport(handler))
        source = SmarketsAdapter(["EPL"], client=client)
        with pytest.raises(SourceError):
            source.fetch_raw()


# ── round eleven ─────────────────────────────────────────────────────────────


class TestEverySourceIsInstrumentedTheSameWay:
    def test_no_adapter_keeps_its_failures_to_itself(self) -> None:
        """The collector reads refusals off ``source.last_fetch``, and Pinnacle
        kept a private failure list instead.

        ``getattr`` returned ``None`` and the whole mechanism skipped it in
        silence — on the sharpest book in the set.  Measured: a blocked soccer
        endpoint cost **24,464 quotes and 586 events, 91% of its rows**, and the
        run printed ``OK (2376 quotes, 12 requests)``, emitted no finding and
        exited 0.  That is verbatim the failure the instrumentation was written
        to eliminate.
        """
        import inspect

        from src.sources import registry

        for entry in registry.SOURCES:
            source = entry.replay_instance()
            body = inspect.getsource(type(source).fetch_raw)
            assert "self.last_fetch" in body, f"{entry.key} does not expose its tally"


class TestARefusalIsGradedOnWhatItCost:
    def _run(self, refuse: list[str], tmp_path):
        import httpx

        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.sources.bovada import BovadaAdapter
        from src.store import Store

        captured = {
            json.load(open(path))["endpoint"]: json.load(open(path))["body"]
            for path in glob.glob("tests/fixtures/raw/bovada__*.json")
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            blocked = any(fragment in path for fragment in refuse)
            for endpoint, body in captured.items():
                if endpoint.split(":", 1)[1].strip("/") in path:
                    return (
                        httpx.Response(429, json={"error": "slow down"})
                        if blocked
                        else httpx.Response(200, content=body)
                    )
            return (
                httpx.Response(429, json={"error": "slow down"})
                if blocked
                else httpx.Response(200, json=[])
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        source = BovadaAdapter(
            ["MLB", "EPL", "NFL", "WNBA", "MLS", "ATP"], client=client
        )
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [source], raw_store=RawStore(tmp_path / "raw"), store=store
            )
        return [f for f in result.report.findings if f.code == "scopes_refused"]

    def test_one_league_is_a_bad_afternoon(self, tmp_path) -> None:
        from src.validation import Severity

        found = self._run(["premier-league"], tmp_path)
        assert found and found[0].severity is Severity.WARNING

    def test_most_of_them_is_a_broken_feed(self, tmp_path) -> None:
        """A flat warning called one-of-nine and eight-of-nine the same thing:
        a book that lost 89% of its scopes read as PASS, exit 0, one line among a
        hundred warnings."""
        from src.validation import Severity

        found = self._run(
            ["premier-league", "nfl", "wnba", "mls", "atp"], tmp_path / "b"
        )
        assert found and found[0].severity is Severity.ERROR
        assert "broken feed" in found[0].message


class TestAnEmptySportIsDiagnosedAsAnEmptySport:
    def test_smarkets_counts_events_not_responses(self) -> None:
        """Every sibling counts payload items; Smarkets counted responses — and
        it is the one source that also keeps the events page of an empty sport,
        so an empty scope always scored 1.

        ``require_something`` could then never fire, one empty sport excused
        every refused one, and an off day was reported as an upstream format
        change with a traceback.
        """
        import httpx

        from src.sources.guards import EmptyResponseError
        from src.sources.smarkets import SmarketsAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"events": [], "pagination": {"next_page": None}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        source = SmarketsAdapter(["NHL"], client=client)
        with pytest.raises(EmptyResponseError) as caught:
            source.fetch_raw()
        # ...and it names the scope, which it could not while empty_scopes was
        # permanently empty.
        assert "hockey" in str(caught.value)


class TestAMarkerWordCanBeAClubsWholeName:
    def test_a_lone_marker_that_is_a_real_club_resolves(self) -> None:
        """Atlético Junior of Barranquilla is written "Junior" by Smarkets.
        Refusing it produced a rejection, and one rejection marks the whole
        source unhealthy — an exchange lost its health over one correctly
        spelled Colombian club.
        """
        from src.participants import resolve_open

        junior = resolve_open("Junior", Sport.SOCCER)
        assert junior is not None and junior.key == "SOCCER-junior"
        # This line used to assert the opposite — that the longer spelling
        # stayed a separate club unless an alias said otherwise — on the
        # reasoning that not merging is the safe default.  It is not, here: the
        # rejection this fix removed had at least been counted, and leaving the
        # spellings apart replaced it with a three-way split that nothing
        # reports.  See :class:`TestOneClubUnderFourSpellingsIsOneClub`.
        assert resolve_open("Atletico Junior", Sport.SOCCER).key == junior.key

    def test_a_marker_that_is_not_a_club_is_still_refused(self) -> None:
        from src.participants import resolve_open

        for fragment in ("II", "W", "Youth FC", "Reserves II", "FC"):
            assert resolve_open(fragment, Sport.SOCCER) is None, fragment


# ── round twelve ─────────────────────────────────────────────────────────────


class TestPinnacleTalliesTheScopeItActuallyRefuses:
    def _adapter(self, leagues, blocked_ids):
        import httpx

        from src.sources.pinnacle import PinnacleAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            if "/leagues/" in path and any(str(i) in path for i in blocked_ids):
                return httpx.Response(403, text="<html>cloudflare</html>")
            if "/matchups" in path:
                return httpx.Response(200, json=[{
                    "id": 1, "type": "matchup", "isLive": False,
                    "startTime": "2026-12-01T18:00:00Z",
                    "league": {"id": 246, "name": "MLB", "sport": {"id": 3}},
                    "participants": [
                        {"alignment": "home", "name": "Cincinnati Reds"},
                        {"alignment": "away", "name": "Philadelphia Phillies"},
                    ],
                }])
            if "/markets" in path:
                return httpx.Response(200, json=[{
                    "matchupId": 1, "key": "s;0;m", "type": "moneyline", "period": 0,
                    "status": "open",
                    "prices": [{"designation": "home", "price": -110},
                               {"designation": "away", "price": -110}],
                }])
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        return PinnacleAdapter(leagues, client=client)

    def test_a_refused_league_is_named_not_swallowed(self) -> None:
        """The round-11 tally was per *sport*, and Pinnacle refuses per *league*.

        Five of six soccer leagues answering Cloudflare 403 while MLS answered
        left ``failed_scopes`` empty and the health line reading ``OK (3 quotes,
        2 requests)`` — the exact shape the instrumentation was added to
        eliminate, one level below where it was added.
        """
        source = self._adapter(
            ["MLB", "EPL", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1"],
            blocked_ids=(1980, 2196, 2436, 1842, 2036),
        )
        source.fetch_raw()
        refused = source.last_fetch.failed_scopes
        assert len(refused) >= 5, refused
        assert any("EPL" in scope for scope in refused)
        assert any("LA_LIGA" in scope for scope in refused)

    def test_a_clean_run_names_nothing(self) -> None:
        source = self._adapter(["MLB"], blocked_ids=())
        source.fetch_raw()
        assert source.last_fetch.failed_scopes == []


class TestAnExistingDatabaseSurvivesAnAddedColumn:
    def test_columns_added_later_are_applied_in_place(self, tmp_path) -> None:
        """``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a
        column added to the schema is missing from yesterday's database and the
        first insert fails.

        These columns are additive and defaulted, so a row written before them
        means the same thing after — which is what separates them from the v3→v4
        change, which re-keyed every row and therefore had to refuse to mix.
        """
        import sqlite3

        from src.sources.base import SourceHealth
        from src.store import Store

        path = tmp_path / "db.sqlite3"
        with Store(path) as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="book_a", ok=True, checked_at=FETCHED, quote_count=1,
            ))

        # Simulate the older schema by dropping **every** additive column back
        # off, derived from ``_ADDED_COLUMNS`` rather than named here.
        #
        # This used to list two of them by hand, so it caught deleting those two
        # upgrade entries and nothing else: ``repaired_count``,
        # ``excluded_count`` and ``counterparty_groups`` were each individually
        # removable with the whole suite green, and an upgraded build then broke
        # against yesterday's database with "table source_health has no column
        # named repaired_count".  Deriving the list means the next column added is
        # covered the moment it is registered.
        from src.store import Store as _Store

        # Written out, **not** derived from ``_ADDED_COLUMNS``.
        #
        # Deriving it meant the test checked whatever the code happened to list:
        # removing an entry simply made the test drop one fewer column and pass.
        # Spelled out here, removing an entry fails the comparison below, and
        # adding a column requires saying so in both places — which is the point,
        # because the cost of forgetting is an upgraded build breaking against
        # yesterday's database.
        expected = {
            ("source_health", "scopes_requested"),
            ("source_health", "scopes_failed"),
            ("source_health", "scopes_refused"),
            ("source_health", "scopes_truncated"),
            ("source_health", "repaired_count"),
            ("collection_run", "scope_sports"),
            ("collection_run", "scope_leagues"),
            ("collection_run", "excluded_count"),
            ("collection_run", "migrated_from"),
            ("collection_run", "counterparty_groups"),
            ("collection_run", "jurisdiction"),
            ("collection_run", "batch_id"),
            ("collection_run", "route_scope"),
        }
        registered = {(table, column) for table, column, _ in _Store._ADDED_COLUMNS}
        assert registered == expected, (
            "a column was added to or removed from the in-place upgrade list; "
            "update this set too, deliberately"
        )
        added = {}
        for table, column in sorted(expected):
            added.setdefault(table, []).append(column)
        with sqlite3.connect(path) as raw:
            for table, columns in added.items():
                # Rebuilt rather than ``ALTER TABLE ... DROP COLUMN``, which
                # SQLite refuses on ``collection_run``: that table's DDL carries
                # ``--`` comments, and DROP COLUMN rewrites the DDL, so it fails
                # with "incomplete input".  Rebuilding works whatever the DDL
                # looks like, and it is closer to what an older build's file
                # actually is — a table that never had the columns.
                keep = [
                    entry[1] for entry in raw.execute(f"PRAGMA table_info({table})")
                    if entry[1] not in columns
                ]
                names = ", ".join(keep)
                raw.execute(f"CREATE TABLE _old AS SELECT {names} FROM {table}")
                raw.execute(f"DROP TABLE {table}")
                raw.execute(f"ALTER TABLE _old RENAME TO {table}")

        with Store(path) as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="book_a", ok=True, checked_at=FETCHED,
                scopes_requested=6,
                scopes_failed=2,
                failed_scopes=("EPL: blocked", "MLS: blocked"),
                truncated_scopes=("MLB: beyond the fallback cap",),
            ))
            row = store.query(
                "SELECT scopes_requested, scopes_failed, scopes_refused, "
                "scopes_truncated FROM source_health WHERE run_id = ?",
                (run,),
            )[0]
        assert row["scopes_requested"] == 6
        assert row["scopes_failed"] == 2
        assert "EPL: blocked" in row["scopes_refused"]
        assert "beyond the fallback cap" in row["scopes_truncated"]

        # Every registered column is back, on the table it belongs to.
        with Store(path) as store:
            for table, column in sorted(expected):
                present = {
                    entry["name"]
                    for entry in store.query(f"PRAGMA table_info({table})")
                }
                assert column in present, f"{table}.{column} was not re-added"


class TestTheComparableWindowIsMeasuredBetweenTheLegs:
    def _report(self, offsets: dict[str, int], *, shared: bool = True):
        from src.validation import ValidationReport, _check_observation_window

        quotes = []
        for index, (source, offset) in enumerate(offsets.items()):
            for selection in (Selection.HOME, Selection.AWAY):
                quotes.append(make_quote(
                    source=source,
                    selection=selection,
                    observed_at=FETCHED + timedelta(seconds=offset),
                    event_key=make_quote().event_key if shared else f"EVT-{index}",
                ))
        report = ValidationReport()
        _check_observation_window(quotes, report)
        return [f for f in report.findings
                if f.code == "collected_outside_the_comparable_window"]

    def test_two_sources_either_side_of_the_middle_are_caught(self) -> None:
        """Measured against the run's consensus, the 180-second limit is a
        360-second limit.

        Two sources 179 seconds either side of the middle are 358 seconds apart.
        ``src.arb`` refuses every market they share — correctly — and both clear
        the check by 1 second, so the coverage grid reports two sources on
        fixtures that can never produce a position between them.
        """
        found = self._report({"early": -179, "late": 179})
        assert {f.source for f in found} == {"early", "late"}
        assert "5 minutes" in found[0].message

    def test_a_source_close_to_its_only_partner_is_not_indicted(self) -> None:
        """The same defect the other way: the consensus form reported a source
        that loses nothing.

        Both exchanges here are ten minutes off the run's middle and 20 seconds
        from each other, which is the only distance that decides whether their
        shared markets pair.
        """
        found = self._report({"book": 0, "ex_a": 600, "ex_b": 620})
        assert [f.source for f in found] == ["book"]

    def test_markets_nobody_else_prices_are_not_counted_as_lost(self) -> None:
        found = self._report({"early": -179, "late": 179}, shared=False)
        assert found == []


class TestOneClubUnderFourSpellingsIsOneClub:
    @pytest.mark.parametrize("name", [
        "Junior", "Junior FC", "CD Junior FC", "Junior de Barranquilla",
        "Junior Barranquilla", "Atletico Junior",
    ])
    def test_every_venues_spelling_lands_on_one_slug(self, name: str) -> None:
        """Letting "Junior" resolve stopped a rejection and started a split.

        The rejection was counted and marked the source unhealthy; the split is
        silent — three sources price the same fixture under three participant
        keys, and the coverage grid reports three separate one-source fixtures.
        """
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug(name, sport=LeagueSport.SOCCER) == "junior"

    @pytest.mark.parametrize("name,expected", [
        ("Junior W", "juniorw"),
        ("Junior Youth", "junioryouth"),
        ("Junior de Barranquilla W", "juniorw"),
        ("Wolves W", "wolverhamptonw"),
        ("Wolverhampton Wanderers W", "wolverhamptonw"),
    ])
    def test_the_alias_reaches_the_club_carrying_a_marker(
        self, name: str, expected: str
    ) -> None:
        """An alias keyed on the club alone never matches its women's side.

        ``"Junior W"`` was rejected outright — the marker-word fix was keyed on
        the whole name, so it reached "Junior" and nothing else — and once it
        resolved it still could not meet ``"Junior de Barranquilla W"``, because
        the alias table has no key with a suffix on it.
        """
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug(name, sport=LeagueSport.SOCCER) == expected

    @pytest.mark.parametrize("name", ["W", "II", "Reserves", "Youth", "FC"])
    def test_a_marker_with_no_club_is_still_not_a_club(self, name: str) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug(name, sport=LeagueSport.SOCCER) is None

    def test_the_senior_side_stays_apart_from_the_womens_side(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        senior = _open_slug("Junior", sport=LeagueSport.SOCCER)
        womens = _open_slug("Junior W", sport=LeagueSport.SOCCER)
        youth = _open_slug("Junior Youth", sport=LeagueSport.SOCCER)
        assert len({senior, womens, youth}) == 3


class TestSmarketsKeepsTheBatchesItAlreadyPaidFor:
    """Batched requests are 141 of this source's 156, so a helper that built a
    private list and raised before returning it discarded ~90% of a pass.

    The event pages were fixed to append as they arrive; the batches beneath
    them, which are almost the whole cost of the pass, were not — so the fix
    read as complete while the responses it was written to save were still
    being thrown away.
    """

    def _client(self, *, fails: str):
        """Every sport answers, except *fails*, whose second market batch 503s."""
        import httpx

        seen: list[str] = []
        # Events ids are namespaced per sport so the market batches of one sport
        # are distinguishable from another's.
        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            if "/events/" in path and path.endswith("/markets/"):
                seen.append(path)
                if fails in path and sum(fails in p for p in seen) > 1:
                    return httpx.Response(503, text="upstream")
                return httpx.Response(200, json={"markets": []})
            if path.endswith("/events/"):
                kind = request.url.params.get("type", "x")
                return httpx.Response(200, json={
                    "events": [{"id": f"{kind}{i}", "name": f"A vs B {i}",
                                "start_date": "2026-12-01T18:00:00Z"}
                               for i in range(40)],  # two batches of 20
                    "pagination": {"next_page": None},
                })
            return httpx.Response(200, json={})

        return httpx.Client(transport=httpx.MockTransport(handler))

    def _adapter(self, leagues, *, fails: str):
        from src.sources._common import HostPacer, RetryPolicy
        from src.sources.smarkets import SmarketsAdapter

        source = SmarketsAdapter(leagues, client=self._client(fails=fails))
        # Politeness pacing and retry backoff are real seconds against a
        # transport that is not a real host.  What is under test is which
        # responses survive a failure, not how many times it was asked for.
        source._http._pacer = HostPacer(0.0)
        source._http.host_interval = 0.0
        source._http.retry = RetryPolicy(attempts=1)
        return source

    def test_a_failure_mid_batch_keeps_the_batches_already_fetched(self) -> None:
        from src.sources.guards import SourceError
        from src.schema import Sport as SchemaSport
        from src.sources._common import Tier

        source = self._adapter(["MLB"], fails="baseball_match")
        kept: list[RawResponse] = []
        with pytest.raises(SourceError):
            source._fetch_sport(SchemaSport.BASEBALL, Tier.FULL, into=kept)
        batches = [r for r in kept if "markets" in r.endpoint]
        assert batches, "every batch already fetched was thrown away"
        assert len(kept) > len(batches), "the event page should be kept too"

    def test_a_sport_that_fails_does_not_take_its_batches_with_it(self) -> None:
        """The same thing through the public entry point: one sport failing
        mid-batch leaves the responses it paid for on disk for the next replay.
        """
        source = self._adapter(["MLB", "EPL"], fails="baseball_match")
        raws = source.fetch_raw()
        assert source.last_fetch.failed_scopes, "baseball was supposed to fail"
        # The *failed* sport's batches specifically.  Asserting only that some
        # batch survived passed with the defect still in place, because the
        # healthy sport's batches satisfied it.
        assert any(raw.endpoint.startswith("markets:baseball") for raw in raws)
        assert any(raw.endpoint.startswith("markets:soccer") for raw in raws)


class TestTheWindowIsMeasuredOnPricesNotOnSourceMedians:
    """A source's median observation time describes none of its prices when the
    source takes minutes to finish.

    Nine of the ten finish inside 20 seconds, so their median stands in for
    every quote they hold and the shortcut is invisible.  Smarkets is paced to
    its own published rate limit — 155 requests 3.1 s apart — and takes 7m40s.
    On live data the median form reported all 996 of its shared selections as
    uncomparable when 72 of them were fine, and raised a warning against a
    source that had lost nothing at all.
    """

    def _findings(self, quotes):
        from src.validation import ValidationReport, _check_observation_window

        report = ValidationReport()
        _check_observation_window(quotes, report)
        return [f for f in report.findings
                if f.code == "collected_outside_the_comparable_window"]

    def test_a_slow_sources_early_markets_still_count(self) -> None:
        quotes = [
            make_quote(source="slow", event_key="A", observed_at=FETCHED),
            make_quote(source="slow", event_key="B",
                       observed_at=FETCHED + timedelta(seconds=600)),
            make_quote(source="fast", event_key="A", observed_at=FETCHED),
            make_quote(source="fast", event_key="B", observed_at=FETCHED),
        ]
        found = self._findings(quotes)
        assert {f.source for f in found} == {"slow", "fast"}
        # One of the two shared markets, not both: on "A" the two sources priced
        # at the same instant.  The median form put slow's whole pass 300
        # seconds from fast's and condemned both.
        for finding in found:
            assert "1 of " in finding.message, finding.message
            assert "'s 2 shared market(s)" in finding.message

    def test_a_long_pass_that_lost_nothing_is_not_reported(self) -> None:
        """The false-positive direction, which is the one that erodes trust in
        the report: every price this source shares was read within reach of its
        partner, and only its unshared markets are late.
        """
        quotes = [
            make_quote(source="slow", event_key="A", observed_at=FETCHED),
            make_quote(source="slow", event_key="C",
                       observed_at=FETCHED + timedelta(seconds=600)),
            make_quote(source="fast", event_key="A", observed_at=FETCHED),
        ]
        assert self._findings(quotes) == []

    def test_the_bound_is_the_detectors_own(self) -> None:
        """Two constants that must agree is a defect waiting to happen: this
        check exists only to explain a refusal ``src.arb`` makes silently.
        """
        from src.arb import MAX_OBSERVATION_SPREAD
        from src.validation import MAX_USABLE_OBSERVATION_GAP

        assert MAX_USABLE_OBSERVATION_GAP is MAX_OBSERVATION_SPREAD


# ── round thirteen ───────────────────────────────────────────────────────────


class TestOneBookIsOneCounterpartyInEveryLeague:
    """The mirror gate closed only in leagues holding 20+ shared selections.

    :data:`src.distinctness.MIN_SHARED_SELECTIONS` answers "how much overlap
    does it take to *establish* that two sources are one counterparty".  It was
    being used to answer "*where* does that fact apply", which is a different
    question with a different answer: two licences of one operator are one
    counterparty everywhere, and do not become independent in a competition
    where this pipeline happens to hold 18 shared prices instead of 20.

    Found independently by both reviewers this round, which is what a defect
    looks like when the tests pin the wrong invariant: the covering test
    asserted the mirrored league was present and the un-mirrored one absent, so
    it passed for the whole life of the bug and would have gone on passing.
    """

    def _tenants(self, *, thin_league_rows: int):
        """Two tenants of one book: a measured mirror in ITF, thin in WTA."""
        rows = []
        for index in range(25):
            key = f"TENNIS-a{index}@TENNIS-b{index}:2026-07-29"
            common = dict(
                sport=Sport.TENNIS, event_key=key, source_event_id=f"itf{index}",
                home_participant=f"TENNIS-b{index}", away_participant=f"TENNIS-a{index}",
                home_team=f"B {index}", away_team=f"A {index}",
                league="ITF", selection=Selection.HOME, commence_time=LATER,
            )
            for source in ("tenant_a", "tenant_b"):
                rows.append(make_quote(source=source, decimal_odds=1.80, **common))

        # A second competition they both quote, too thin to judge on its own.
        for index in range(thin_league_rows):
            key = f"TENNIS-w{index}@TENNIS-x{index}:2026-07-29"
            common = dict(
                sport=Sport.TENNIS, event_key=key, source_event_id=f"wta{index}",
                home_participant=f"TENNIS-x{index}", away_participant=f"TENNIS-w{index}",
                home_team=f"X {index}", away_team=f"W {index}",
                league="WTA", commence_time=LATER,
            )
            # Priced to a fat arbitrage *between the two tenants* — one side's
            # cache is stale.  Between two real books this would be money.
            rows.append(make_quote(source="tenant_a", selection=Selection.HOME,
                                   decimal_odds=2.30, **common))
            rows.append(make_quote(source="tenant_b", selection=Selection.AWAY,
                                   decimal_odds=2.05, **common))
        return rows

    def test_the_thin_league_is_gated_too(self) -> None:
        from src.arb import counterparty_groups, find_opportunities

        rows = self._tenants(thin_league_rows=9)
        report = find_opportunities(rows, one_counterparty=counterparty_groups(rows))
        assert report.opportunities == [], [
            f"{o.market.value} {o.margin:.2%} " + " / ".join(l.source for l in o.legs)
            for o in report.opportunities
        ]

    def test_the_same_prices_at_two_real_books_are_still_found(self) -> None:
        """The control.  The gate must close on one book wearing two names and
        on nothing else, or it is just breaking the detector.
        """
        from src.arb import counterparty_groups, find_opportunities

        rows = self._tenants(thin_league_rows=9)
        # Same prices, but the two tenants no longer agree in ITF, so there is
        # no evidence they are one counterparty and the gate stays open.
        rows = [
            row.model_copy(update={"decimal_odds": 1.95, "implied_probability": 1 / 1.95})
            if row.source == "tenant_b" and row.league == "ITF" else row
            for row in rows
        ]
        report = find_opportunities(rows, one_counterparty=counterparty_groups(rows))
        assert report.opportunities, "the detector stopped finding real positions"

    def test_a_pair_mirrored_only_overall_still_gates(self) -> None:
        """The second form: a pair whose overall rate says MIRROR but which has
        no single league above the floor contributed *nothing* to the gate.
        """
        from src.arb import EVERY_LEAGUE, counterparty_groups

        rows = []
        for index in range(30):  # 30 selections spread over 10 leagues: 3 each
            league = f"L{index % 10}"
            key = f"TENNIS-a{index}@TENNIS-b{index}:2026-07-29"
            common = dict(
                sport=Sport.TENNIS, event_key=key, source_event_id=f"e{index}",
                home_participant=f"TENNIS-b{index}", away_participant=f"TENNIS-a{index}",
                home_team=f"B {index}", away_team=f"A {index}", league=league,
                selection=Selection.HOME, decimal_odds=1.80, commence_time=LATER,
            )
            rows.append(make_quote(source="tenant_a", **common))
            rows.append(make_quote(source="tenant_b", **common))

        from src.distinctness import Verdict, compare_sources
        pair = compare_sources(rows, "tenant_a", "tenant_b")
        assert pair.verdict is Verdict.MIRROR
        assert pair.mirrored_leagues == (), "no single league should clear the floor"
        assert counterparty_groups(rows) == {
            EVERY_LEAGUE: [frozenset({"tenant_a", "tenant_b"})]
        }


class TestThreeNamesForOneBookAreOneCounterparty:
    """Being one counterparty is transitive; the ids assigned to it were not.

    The measurement is pairwise, so one operator running three names arrives as
    three overlapping pairs.  Assigning each pair its own minimum in turn let
    the last pair processed overwrite the first, and the gate between the two
    sources named in the *earlier* pair reopened — the group was in the gate,
    read out of the gate, and still allowed to trade against itself.
    """

    def _mapping(self, groups):
        from src.arb import EVERY_LEAGUE, _counterparties

        rows = [make_quote(source=source) for source in ("alpha", "beta", "gamma")]
        return _counterparties(rows, {EVERY_LEAGUE: groups})

    def test_three_overlapping_pairs_collapse_to_one_identity(self) -> None:
        mapping = self._mapping([
            frozenset({"alpha", "beta"}),
            frozenset({"alpha", "gamma"}),
            frozenset({"beta", "gamma"}),
        ])
        assert len(set(mapping.values())) == 1, mapping
        assert set(mapping) == {"alpha", "beta", "gamma"}

    def test_a_chain_of_pairs_is_one_identity(self) -> None:
        """No pair names alpha and gamma together; they are still one book."""
        mapping = self._mapping([
            frozenset({"alpha", "beta"}),
            frozenset({"beta", "gamma"}),
        ])
        assert mapping["alpha"] == mapping["gamma"] == mapping["beta"]

    def test_the_identity_does_not_depend_on_the_order_the_pairs_arrive(self) -> None:
        pairs = [
            frozenset({"alpha", "beta"}),
            frozenset({"beta", "gamma"}),
            frozenset({"alpha", "gamma"}),
        ]
        first = self._mapping(pairs)
        assert self._mapping(list(reversed(pairs))) == first
        assert set(first.values()) == {"alpha"}, "the smallest member names it"

    def test_two_separate_books_keep_two_identities(self) -> None:
        from src.arb import EVERY_LEAGUE, _counterparties

        rows = [make_quote(source=s) for s in ("alpha", "beta", "yankee", "zulu")]
        mapping = _counterparties(rows, {EVERY_LEAGUE: [
            frozenset({"alpha", "beta"}),
            frozenset({"yankee", "zulu"}),
        ]})
        assert mapping["alpha"] == mapping["beta"] == "alpha"
        assert mapping["yankee"] == mapping["zulu"] == "yankee"
        assert mapping["alpha"] != mapping["yankee"]


class TestAScopeIsCountedInOneUnit:
    """``scopes_requested`` is the denominator the collector grades on and
    ``failed_scopes`` is the numerator, and nothing tied them to one unit.

    Pinnacle counted requests in **sports** and refusals in **leagues**: five
    blocked soccer leagues against six configured sports gave ``share_lost =
    6/2 = 3.0`` and a message reading "refused 6 of the scopes it asked for" of
    two.  It graded ERROR there by arithmetic accident, and by the same accident
    graded the loss of a *whole sport* — 91% of this source's rows — a WARNING.
    """

    def _blocked(self, leagues, blocked_ids, **kwargs):
        import httpx

        from src.sources.pinnacle import PinnacleAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            if "/leagues/" in path and any(f"/{i}/" in path for i in blocked_ids):
                return httpx.Response(403, text="<html>cloudflare</html>")
            if "/matchups" in path:
                return httpx.Response(200, json=[{
                    "id": 1, "type": "matchup", "isLive": False, "parentId": None,
                    "units": "Regular", "startTime": "2099-12-01T18:00:00Z",
                    "league": {"id": 246, "name": "MLB", "sport": {"id": 3}},
                    "participants": [
                        {"alignment": "home", "name": "Cincinnati Reds"},
                        {"alignment": "away", "name": "Philadelphia Phillies"},
                    ],
                }])
            if "/markets" in path:
                return httpx.Response(200, json=[{
                    "matchupId": 1, "key": "s;0;m", "type": "moneyline", "period": 0,
                    "status": "open",
                    "prices": [{"designation": "home", "price": -110},
                               {"designation": "away", "price": -110}],
                }])
            return httpx.Response(200, json=[])

        return PinnacleAdapter(
            leagues, client=httpx.Client(transport=httpx.MockTransport(handler)),
            request_pause=0.0, **kwargs,
        )

    def test_refusals_are_never_more_numerous_than_requests(self) -> None:
        source = self._blocked(
            ["MLB", "EPL", "LA_LIGA", "SERIE_A", "BUNDESLIGA", "LIGUE_1"],
            blocked_ids=(1980, 2196, 2436, 1842, 2036),
        )
        source.fetch_raw()
        tally = source.last_fetch
        assert tally.scopes_requested == 6, tally.scopes_requested
        assert len(tally.failed_scopes) == 5, tally.failed_scopes
        # 5 of 6 leagues lost really is a collapse; the point is that the number
        # now measures something.
        assert len(tally.failed_scopes) / tally.scopes_requested < 1.0

    def test_a_failed_league_is_not_also_counted_as_a_failed_sport(self) -> None:
        """``_fetch_routed_leagues`` re-raises so the caller knows the sport
        failed, and the caller recorded that as a *second* refusal carrying the
        first league's error message under the sport's name.
        """
        source = self._blocked(["EPL", "LA_LIGA"], blocked_ids=(1980, 2196))
        with pytest.raises(Exception):
            source.fetch_raw()
        named = [entry.split(":")[0] for entry in source.last_fetch.failed_scopes]
        assert sorted(named) == ["EPL", "LA_LIGA"], named
        assert "soccer" not in named

    def test_the_tally_registers_a_scope_it_is_first_told_about(self) -> None:
        from src.sources._common import ScopeTally
        from src.sources.guards import EmptyResponseError

        tally = ScopeTally("book")
        tally.failed("EPL", EmptyResponseError("nope"))
        tally.produced("MLS", 3)
        assert tally.scopes_requested == 2
        # Idempotent: asking twice for one scope is one scope.
        tally.requested("EPL")
        assert tally.scopes_requested == 2


class TestTheDegradedPathIsInstrumentedToo:
    """``_fetch_sport_by_league_index`` recorded nothing at all.

    Not the leagues it deliberately omits under its cap, not the ones that
    answer 403 inside it.  A sport that entered here after its own endpoint was
    blocked and then lost most of its remaining leagues reported ``OK`` with an
    empty ``failed_scopes`` — and this is the only path tennis ever uses, and
    the one soccer uses whenever the catch-all league is configured, which is
    the default.
    """

    def _adapter(self, *, listed: int, blocked: set[int], cap: int):
        import httpx

        from src.sources.pinnacle import PinnacleAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            if path.startswith("/0.1/sports/33/matchups"):
                return httpx.Response(401, json={"error": "unauthorized"})
            if path == "/0.1/sports/33/leagues":
                return httpx.Response(200, json=[
                    {"id": 4000 + n, "name": f"ATP Town {n} - R1", "matchupCount": 100 - n}
                    for n in range(listed)
                ])
            if any(f"/leagues/{4000 + n}/" in path for n in blocked):
                return httpx.Response(403, text="<html>cloudflare</html>")
            if path.endswith("/markets/straight"):
                return httpx.Response(200, json=[{
                    "matchupId": 1, "key": "s;0;m", "type": "moneyline", "period": 0,
                    "status": "open",
                    "prices": [{"designation": "home", "price": -110},
                               {"designation": "away", "price": -110}],
                }])
            return httpx.Response(200, json=[{
                "id": 1, "type": "matchup", "isLive": False, "parentId": None,
                "units": "Regular", "startTime": "2099-12-01T18:00:00Z",
                "league": {"id": 4000, "name": "ATP Town 0 - R1", "sport": {"id": 33}},
                "participants": [{"alignment": "home", "name": "Marcos Giron"},
                                 {"alignment": "away", "name": "Cruz Hewitt"}],
            }])

        return PinnacleAdapter(
            ["ATP", "ATP_CHALLENGER"],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            request_pause=0.0, max_fallback_leagues=cap,
        )

    def test_a_league_refused_inside_the_fallback_is_named(self) -> None:
        source = self._adapter(listed=4, blocked={1, 2}, cap=4)
        source.fetch_raw()
        refused = source.last_fetch.failed_scopes
        assert len(refused) == 2, refused
        assert source.last_fetch.scopes_requested == 4

    def test_the_leagues_the_cap_leaves_out_are_reported(self) -> None:
        """A bound on request volume is politeness and stays.  Reporting the run
        as complete afterwards is not.
        """
        source = self._adapter(listed=30, blocked=set(), cap=5)
        source.fetch_raw()
        # A deliberate page/league cap is a truncation, not a refusal — see
        # ``ScopeTally.truncated``.  Filing it under ``failed_scopes`` graded a
        # run that collected five leagues as one that "returned none of the
        # rest".
        capped = [e for e in source.last_fetch.truncated_scopes if "beyond the fallback cap" in e]
        assert len(capped) == 1, source.last_fetch.truncated_scopes
        assert source.last_fetch.failed_scopes == []
        assert "25 league(s)" in capped[0]

    def test_a_fallback_that_covers_everything_reports_nothing(self) -> None:
        source = self._adapter(listed=3, blocked=set(), cap=20)
        source.fetch_raw()
        assert source.last_fetch.failed_scopes == []
        assert source.last_fetch.scopes_requested == 3


class TestReplayComparesTheWholeRow:
    """Replay is the pipeline's one guarantee that parsing is a pure function
    of the captured bytes, and it compared two fields out of twenty-odd.

    ``dedup_key`` covers identity — source, fixture, market, period, side, line,
    selection — so a comparison keyed on it that then checked only
    ``decimal_odds`` and ``observed_at`` could not see anything else the parser
    produced.  Measured against the committed fixtures, a parser rewriting one
    field per run reported **PASS** while flipping ``status`` on 3,293 rows and
    ``limit_amount`` on 2,346 — the two fields ``src.arb`` reads to decide
    whether a price is takeable and how much of it can be placed.
    """

    def _run(self, tmp_path, mutate=None):
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.sources.base import ParseOutcome
        from src.store import Store

        def rows():
            return [
                make_quote(source="book_a", selection=Selection.HOME,
                           decimal_odds=2.10, source_market_id="ml"),
                make_quote(source="book_a", selection=Selection.AWAY,
                           decimal_odds=1.95, source_market_id="ml"),
            ]

        class Rigged:
            source_key = "book_a"
            leagues = ("MLB",)

            def __init__(self, mutate=None):
                self._mutate = mutate

            def fetch_raw(self):
                return [_raw("book_a", "markets-01", {"markets": []})]

            def parse(self, raws):
                quotes = rows()
                if self._mutate is not None:
                    quotes = [self._mutate(q) for q in quotes]
                return ParseOutcome(quotes=quotes)

            def close(self) -> None:
                pass

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [Rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes, "the fixture produced no rows to compare"
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = lambda: Rigged(mutate)
            try:
                return replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved

    def test_an_unchanged_parser_still_reproduces_the_run(self, tmp_path) -> None:
        ok, problems = self._run(tmp_path)
        assert ok, problems

    @pytest.mark.parametrize("field,value", [
        ("status", QuoteStatus.SUSPENDED),
        ("limit_amount", 25.0),
        ("american_odds", -9999),
        ("implied_probability", 0.123),
        ("home_team", "Somebody Else"),
        ("raw_ref", "book_a/somewhere-else"),
    ])
    def test_a_rewritten_field_is_caught(self, tmp_path, field, value) -> None:
        """Every field *outside* ``dedup_key`` — the ones the old comparison
        could not see, because keying on identity says nothing about content.
        """
        ok, problems = self._run(
            tmp_path, mutate=lambda q: q.model_copy(update={field: value})
        )
        assert not ok, f"replay reported PASS with {field} rewritten"
        assert any(field in problem for problem in problems), problems

    def test_a_rewritten_identity_field_is_caught_as_a_lost_row(self, tmp_path) -> None:
        """``commence_time`` feeds ``event_key`` feeds ``dedup_key``, so it is
        caught by the lost/invented halves rather than by the field comparison.
        Asserted separately so that "caught" is not confused with "caught here".
        """
        ok, problems = self._run(tmp_path, mutate=lambda q: q.model_copy(
            update={"commence_time": datetime(2026, 8, 1, tzinfo=UTC)}
        ))
        assert not ok
        assert any("lost row" in problem for problem in problems), problems
        assert any("invented row" in problem for problem in problems), problems

    @staticmethod
    def _make_rigged():
        """The minimal one-book source the archive-integrity pins collect."""
        from src.sources.base import ParseOutcome

        class Rigged:
            source_key = "book_a"
            leagues = ("MLB",)

            def fetch_raw(self):
                return [_raw("book_a", "markets-01", {"markets": []})]

            def parse(self, raws):
                return ParseOutcome(quotes=[
                    make_quote(source="book_a", selection=Selection.HOME,
                               decimal_odds=2.10, source_market_id="ml"),
                    make_quote(source="book_a", selection=Selection.AWAY,
                               decimal_odds=1.95, source_market_id="ml"),
                ])

            def close(self) -> None:
                pass

        return Rigged

    def test_corrupted_bytes_suppress_the_evolution_preamble(self, tmp_path) -> None:
        """The preamble may only vouch for bytes that were actually hashed.

        Corruption raises out of ``RawStore.read`` *before* the sha check —
        ``json.loads`` on a truncated file — so the first spelling of the
        preamble condition ("no 'sha256' anywhere, some marker somewhere")
        fired 'stored bytes verified against their recorded sha256s' above a
        JSONDecodeError it had just mislabeled as parser evolution.  The
        preamble now requires EVERY problem to be a row comparison, and a
        failure to read stored bytes is always a problem (never a log note),
        so all-comparison genuinely means every read succeeded.
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        rigged = self._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            raw_path = next((tmp_path / "raw").rglob("*.json"))
            raw_path.write_text(raw_path.read_text()[: len(raw_path.read_text()) // 2])
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert any("stored bytes unreadable" in p for p in problems), problems
        assert not any("verified against their recorded sha256s" in p for p in problems), (
            problems
        )

    def test_a_sha_mismatch_is_never_downgraded_to_a_note(self, tmp_path) -> None:
        """"Stored no rows" excuses a parser raise, not archive corruption.

        A recorded sha256 that no longer matches its bytes is the one signal
        the preamble tells the reader to trust; for a source with zero stored
        rows the raise was downgraded to a log line, so the archive could rot
        invisibly under a PASS verdict.
        """
        import json

        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [self._make_rigged()()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            # The source "stored no rows" (insert wiped), and its archived
            # body no longer matches the recorded sha.
            store._conn.execute("DELETE FROM quote WHERE run_id = ?", (result.run_id,))
            store._conn.commit()
            raw_path = next((tmp_path / "raw").rglob("*.json"))
            envelope = json.loads(raw_path.read_text())
            envelope["body"] = envelope["body"] + " "
            raw_path.write_text(json.dumps(envelope))
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = self._make_rigged()
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert any("sha256" in p for p in problems), problems

    def test_replay_judges_no_adapter_by_stored_rows_not_by_health(self, tmp_path) -> None:
        """"Stored rows" is the stored fact, so it comes off the quote table.

        ``source_health.quote_count`` is what the adapter produced; the two
        diverge exactly when the insert failed or a scope filter dropped
        everything (``Store.stored_quote_counts``'s own docstring).  In that
        divergence, judging by health files a deregistered source's missing
        adapter as a *problem* — failing the replay over rows that were never
        in the database and therefore cannot be lost.
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [self._make_rigged()()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            # The insert-failed divergence: health says 2 produced, the quote
            # table holds none.
            store._conn.execute(
                "DELETE FROM quote WHERE run_id = ?", (result.run_id,)
            )
            store._conn.commit()
            saved = SOURCE_FACTORIES.pop("book_a", None)
            try:
                ok, problems = replay_run(
                    result.run_id, store=store, raw_store=raw_store
                )
            finally:
                if saved is not None:
                    SOURCE_FACTORIES["book_a"] = saved
        assert ok, problems
        assert not any("no adapter available" in p for p in problems), problems

    def test_a_deleted_raw_file_fails_the_replay_even_for_a_zero_row_source(
        self, tmp_path
    ) -> None:
        """Unreadable stored bytes are judged on provenance, not exception text.

        The first guard matched "sha256" in the exception's spelling, which
        caught exactly one of the ways ``RawStore.read`` fails: a
        ``FileNotFoundError`` for a deleted archive file (or a
        ``JSONDecodeError`` for a truncated one, or the version/field errors
        of a mangled envelope) raises *before* any hash check, carries no
        "sha256", and for a zero-row source was downgraded to a log line —
        the run's DB lists the file (the read was attempted), the archive has
        rotted, and replay reported PASS with no problems at all.
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        rigged = self._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            # Zero-row: the excuse that downgraded the raise must be active.
            store._conn.execute(
                "DELETE FROM quote WHERE run_id = ?", (result.run_id,)
            )
            store._conn.commit()
            next((tmp_path / "raw").rglob("*.json")).unlink()
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert any(
            "book_a" in p and "stored bytes unreadable" in p for p in problems
        ), problems

    def test_a_siblings_truncated_file_starves_the_preamble(self, tmp_path) -> None:
        """The preamble may not vouch over another source's unhashed bytes.

        The all-comparison condition is only as strong as the downgrade path
        feeding it: with a zero-row source's ``JSONDecodeError`` logged
        rather than filed, a run holding one truncated archive file and one
        comparison-shaped divergence elsewhere still printed "stored raw
        bytes verified against their recorded sha256s" — over bytes that
        raised before any hash was checked.  Unreadable bytes are now a
        problem for every source, which both fails the replay and starves
        the preamble's condition.
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.sources.base import ParseOutcome
        from src.store import Store

        rigged = self._make_rigged()

        class Bystander:
            source_key = "book_b"
            leagues = ("MLB",)

            def fetch_raw(self):
                return [_raw("book_b", "markets-01", {"markets": []})]

            def parse(self, raws):
                return ParseOutcome(quotes=[])

            def close(self) -> None:
                pass

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged(), Bystander()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            # book_b stored bytes and no rows; its archive file rots.
            b_path = next((tmp_path / "raw" / "book_b").rglob("*.json"))
            b_path.write_text(b_path.read_text()[: len(b_path.read_text()) // 2])
            # book_a diverges comparison-shaped, from the stored side: one
            # row vanishes from the quote table, which replay reports as
            # "row count differs / replay invented row".
            store._conn.execute(
                "DELETE FROM quote WHERE rowid = ("
                "SELECT rowid FROM quote WHERE run_id = ? LIMIT 1)",
                (result.run_id,),
            )
            store._conn.commit()
            saved_a = SOURCE_FACTORIES.get("book_a")
            saved_b = SOURCE_FACTORIES.get("book_b")
            SOURCE_FACTORIES["book_a"] = rigged
            SOURCE_FACTORIES["book_b"] = Bystander
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                for key, saved in (("book_a", saved_a), ("book_b", saved_b)):
                    if saved is None:
                        SOURCE_FACTORIES.pop(key, None)
                    else:
                        SOURCE_FACTORIES[key] = saved
        assert not ok
        assert any(
            "book_b" in p and "stored bytes unreadable" in p for p in problems
        ), problems
        assert not any(
            "verified against their recorded sha256s" in p for p in problems
        ), problems

    def test_a_sha_stripped_envelope_cannot_pass_replay(self, tmp_path) -> None:
        """Removing the recorded hash must not remove the verification.

        ``from_envelope`` skipped the sha check when the field was absent, so
        stripping the key while rewriting the body made the tamper
        undetectable: the rigged parser reads nothing off the bytes, stored
        and replayed rows matched, and the whole replay reported PASS over an
        altered archive.  A missing sha256 is now itself a refusal — every
        envelope ever written records one.
        """
        import json

        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        rigged = self._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            raw_path = next((tmp_path / "raw").rglob("*.json"))
            envelope = json.loads(raw_path.read_text())
            envelope["body"] = envelope["body"] + " "
            del envelope["sha256"]
            raw_path.write_text(json.dumps(envelope))
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert any(
            "stored bytes unreadable" in p and "sha256" in p for p in problems
        ), problems
        assert not any(
            "verified against their recorded sha256s" in p for p in problems
        ), problems

    def test_a_truncated_diff_listing_discloses_its_own_truncation(self, tmp_path) -> None:
        """The listing may bound its length, never its honesty.

        Run 1's real replay is a 627-row net delta that printed as five lost
        and five invented rows with nothing saying more existed — the one
        surface able to show a divergence's scale understated it, while the
        same file explicitly rejects a silent slice for arb output.  The
        markers deliberately contain their category's comparison phrase, so
        a truncated listing on a genuine evolution run must NOT cost the
        verdict its verified-bytes preamble — asserted here alongside the
        marker itself.
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.sources.base import ParseOutcome
        from src.store import Store

        def rows():
            out = []
            for i, line in enumerate((1.5, 2.5, 3.5, 4.5)):
                for sel, signed in ((Selection.HOME, -line), (Selection.AWAY, line)):
                    out.append(make_quote(
                        source="book_a", selection=sel, market=Market.SPREAD,
                        line=signed, decimal_odds=1.9 + i * 0.01,
                        source_market_id=f"sp{i}",
                    ))
            return out

        class Wide:
            source_key = "book_a"
            leagues = ("MLB",)

            def fetch_raw(self):
                return [_raw("book_a", "markets-01", {"markets": []})]

            def parse(self, raws):
                return ParseOutcome(quotes=rows())

            def close(self) -> None:
                pass

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [Wide()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert len(result.quotes) == 8
            # Seven of eight stored rows vanish: replay now invents seven,
            # two beyond the five the listing shows.
            store._conn.execute(
                "DELETE FROM quote WHERE rowid IN ("
                "SELECT rowid FROM quote WHERE run_id = ? LIMIT 7)",
                (result.run_id,),
            )
            store._conn.commit()
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = Wide
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert any(
            "replay invented rows: 2 more beyond the 5 shown" in p for p in problems
        ), problems
        assert "verified against" in problems[0] and "sha256" in problems[0], problems

    def test_a_marker_bearing_tamper_cannot_buy_the_preamble(self, tmp_path) -> None:
        """``comparison_only`` is structural: no problem text can vote.

        The substring gate was defeated by one tampered envelope field —
        ``"envelope_version": "9 replay lost row"`` flows verbatim into the
        unsupported-version ValueError, the "stored bytes unreadable"
        wrapper inherits the marker, and the verdict printed "bytes
        verified against their recorded sha256s" directly above the tamper
        it was vouching away.  Classification now follows how each problem
        was appended, so the same tamper earns the FAIL without the vouch.
        """
        import json

        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        rigged = self._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            raw_path = next((tmp_path / "raw").rglob("*.json"))
            envelope = json.loads(raw_path.read_text())
            envelope["envelope_version"] = "9 replay lost row"
            raw_path.write_text(json.dumps(envelope))
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert any("stored bytes unreadable" in p for p in problems), problems
        assert not any("verified against" in p for p in problems), problems

    def test_the_field_diff_overflow_marker_states_an_exact_count(self, tmp_path) -> None:
        """The listing may bound its length, and its disclosure must be a
        measured fact.

        The first stop marker said "more rows changed on replay than are
        shown" whenever the problem count crossed 20 — provably false at
        the boundary, where all 21 differences were already on screen.  The
        scan now always completes and only the listing is capped, so the
        marker's number is exact; and because the marker summarizes real
        comparisons, the evolution preamble survives above it.
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.sources.base import ParseOutcome
        from src.store import Store

        def rows():
            out = []
            for i in range(11):
                line = 1.5 + i
                for sel, signed in ((Selection.HOME, -line), (Selection.AWAY, line)):
                    out.append(make_quote(
                        source="book_a", selection=sel, market=Market.SPREAD,
                        line=signed, decimal_odds=1.9 + i * 0.001,
                        source_market_id=f"sp{i}",
                    ))
            return out

        class Wide:
            source_key = "book_a"
            leagues = ("MLB",)

            def fetch_raw(self):
                return [_raw("book_a", "markets-01", {"markets": []})]

            def parse(self, raws):
                return ParseOutcome(quotes=rows())

            def close(self) -> None:
                pass

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [Wide()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert len(result.quotes) == 22
            # Exactly 21 stored rows drift by more than the float tolerance:
            # 20 are shown, and the marker must say "1 more", not guess.
            store._conn.execute(
                "UPDATE quote SET decimal_odds = decimal_odds + 0.01 "
                "WHERE rowid IN (SELECT rowid FROM quote WHERE run_id = ? LIMIT 21)",
                (result.run_id,),
            )
            store._conn.commit()
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = Wide
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert any(
            "1 more field difference(s) changed on replay beyond the 20 shown" in p
            for p in problems
        ), problems
        assert not any("more rows changed on replay than are shown" in p for p in problems), (
            problems
        )
        assert "verified against" in problems[0], problems

    def test_a_verified_byte_divergence_names_its_possible_causes(self, tmp_path) -> None:
        """A non-migrated FAIL whose bytes verify must say what kind of claim
        it is making.

        The migration stamp was the only channel that admitted the parser
        legitimately changes, so a deliberate change — the 2026-08-09 mid-move
        guard re-parsing older tracker captures to fewer rows by design —
        convicted v4-native stored runs with bare corruption-shaped output
        ("row count differs / replay lost row") and nothing saying why.  The
        verdict stays FAIL either way; the preamble is the difference between
        a reader re-diagnosing corruption and a reader checking the recorded
        parser changes.  It must name BOTH sides of the diff — the parser's
        reading of the verified bytes AND the stored rows — because an edited
        quote table is comparison-shaped too and no sha256 covers it; the
        first wording convicted the parser alone.
        """
        ok, problems = self._run(
            tmp_path, mutate=lambda q: q.model_copy(update={"american_odds": -9999})
        )
        assert not ok
        assert problems[0].startswith("run ") and (
            "current parser's reading of those verified bytes" in problems[0]
        ), problems
        assert "edit to the stored rows" in problems[0], problems
        assert "sha256" in problems[0], problems


class TestTheProfitFloorIsCheckedAtTheBankrollYouCanActuallyStake:
    """``rounding_destroys_edge`` ran only against the notional bankroll.

    The report is denominated in a nominal 100 while the books' own stated
    limits routinely cap a position far below that, and stake rounding bites
    harder the smaller the bankroll — so the two are different questions and
    only the one nobody can act on was being asked.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _report(self, limit, *, home=1.50, away=3.05):
        from src.arb import find_opportunities

        rows = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=home,
                       source_market_id="ml").model_copy(update={"limit_amount": limit}),
            make_quote(source="pinnacle", selection=Selection.AWAY, decimal_odds=away,
                       source_market_id="ml"),
        ]
        return find_opportunities(rows, as_of=self.AS_OF)

    def test_a_capped_position_is_reported_at_the_size_it_can_be_placed(self) -> None:
        """Reported as ``margin 0.55%, guaranteed +0.50 on 100``, with the
        FanDuel leg staked 67.00 against a stated limit of 6.80.

        This asserted a rejection when the check evaluated a single bankroll —
        the 10.00 cap, where the worst outcome loses 0.85.  Stepping down finds
        a size that is genuinely placeable and genuinely cannot lose, so the
        honest answer is to report *that*: the defect was never that the market
        is untakeable, it was that the reported figure and the stakes described
        a bet nobody could make.
        """
        report = self._report(6.8)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        opportunity = report.opportunities[0]
        assert opportunity.total_stake <= 10
        assert opportunity.guaranteed_profit >= 0
        for leg in opportunity.legs:
            limit = leg.quote.limit_amount
            assert limit is None or leg.stake <= limit + 1e-9, leg.describe()

    def test_no_leg_is_ever_staked_over_the_size_its_venue_stated(self) -> None:
        """Flooring the *total* to whole units does not bound the *per-leg*
        rounding that follows it: a leg sized 6.80 caps the position at 13, and
        the split of 13 stakes that leg 7.00 — 0.20 over the published number.
        On an exchange the surplus goes unmatched; at a book that refuses an
        over-limit bet the "placeable" position is one leg, which is not a
        position at all.
        """
        from src.arb import find_opportunities

        rows = [
            make_quote(source="pinnacle", selection=Selection.HOME,
                       decimal_odds=2.20, source_market_id="ml"),
            make_quote(source="matchbook", selection=Selection.AWAY,
                       decimal_odds=2.20,
                       source_market_id="ml").model_copy(update={"limit_amount": 6.80}),
        ]
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={})
        assert report.opportunities
        for leg in report.opportunities[0].legs:
            limit = leg.quote.limit_amount
            assert limit is None or leg.stake <= limit + 1e-9, leg.describe()

    def test_a_capped_position_does_not_outrank_an_unlimited_one(self) -> None:
        """``report.opportunities.sort`` orders by ``guaranteed_profit`` and
        ``describe()`` prints it as the headline, both of which were computed at
        the notional bankroll.  A pinnacle 2.20 against a matchbook 2.20 sized
        6.80 printed ``+10.00 on 100`` and ranked above a genuinely unlimited
        position paying +2.50 — while the only version anybody could place pays
        a fiftieth of that.
        """
        from src.arb import find_opportunities

        capped = [
            make_quote(source="pinnacle", selection=Selection.HOME,
                       decimal_odds=2.20, source_market_id="a"),
            make_quote(source="matchbook", selection=Selection.AWAY, decimal_odds=2.20,
                       source_market_id="a").model_copy(update={"limit_amount": 6.80}),
        ]
        elsewhere = dict(
            event_key="MLB-XX@MLB-YY:2026-07-28", home_participant="MLB-YY",
            away_participant="MLB-XX", home_team="Y", away_team="X",
            source_event_id="e2", source_market_id="b",
        )
        unlimited = [
            make_quote(source="fanduel", selection=Selection.HOME,
                       decimal_odds=2.05, **elsewhere),
            make_quote(source="bovada", selection=Selection.AWAY,
                       decimal_odds=2.05, **elsewhere),
        ]
        report = find_opportunities(capped + unlimited, as_of=self.AS_OF, commissions={})
        assert len(report.opportunities) == 2
        first, second = report.opportunities
        assert first.max_total_stake is None, "the unlimited position must rank first"
        assert second.max_total_stake is not None
        assert first.guaranteed_profit > second.guaranteed_profit

    def test_limits_too_small_for_one_unit_cannot_be_placed(self) -> None:
        """The reachable form of "this cannot be placed".

        The branch that used to report it tested ``limit_amount <= 0``, which
        :class:`src.schema.Quote` refuses outright — so it could not run, while
        the case that *can* happen printed ``guaranteed +5.00 on 100`` and
        ranked by it.
        """
        report = self._report(0.4, home=2.10, away=2.10)
        assert report.opportunities == []
        assert "cannot_be_placed_at_the_stated_limits" in [
            d.code for d in report.diagnostics
        ]

    def test_a_market_stating_no_limit_is_unaffected(self) -> None:
        report = self._report(None)
        assert len(report.opportunities) == 1
        assert report.opportunities[0].max_total_stake is None

    def test_a_cap_the_position_survives_is_still_reported(self) -> None:
        """The control: a limit that binds but leaves the position risk-free
        must not be rejected, or the fix is just breaking the detector.
        """
        report = self._report(200.0, home=2.10, away=2.10)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        cap = report.opportunities[0].max_total_stake
        assert cap is not None and cap < 1e9

    def test_the_unreachable_zero_limit_state_cannot_be_built(self) -> None:
        """Pinning why the old branch was dead, so it is not reintroduced."""
        import pytest as _pytest

        with _pytest.raises(Exception):
            make_quote(limit_amount=0.0)
        with _pytest.raises(Exception):
            make_quote(limit_amount=-5.0)


class TestAPriceCitesTheResponseItCameFrom:
    """``observed_at`` and ``raw_ref`` must name the bytes the price is in.

    SX Bet attributed every row to the ``markets`` response — the one its own
    module docstring says "carries no prices at all" — while every price came
    from ``orders``.  So the pointer back to the evidence pointed somewhere the
    evidence is not, and ``observed_at`` was the metadata fetch, systematically
    early by the whole metadata-to-orders gap of a 135-request pass.  That
    timestamp feeds the stale-leg gate and the comparable-observation window.
    """

    def _parsed(self):
        import glob

        from src.raw_store import RawStore
        from src.sources.sxbet import parse_sxbet

        store = RawStore("tests/fixtures/raw")
        raws = [store.read(p) for p in sorted(glob.glob("tests/fixtures/raw/sxbet__*.json"))]
        return parse_sxbet(raws)

    def test_every_row_points_at_an_orders_response(self) -> None:
        quotes = self._parsed().quotes
        assert quotes
        assert all("orders" in q.raw_ref for q in quotes), {
            q.raw_ref for q in quotes if "orders" not in q.raw_ref
        }

    def test_the_metadata_response_is_kept_as_identity_provenance(self) -> None:
        """Not discarded — it is what established the fixture, exactly as
        Pinnacle separates its matchups response from its markets one."""
        quotes = self._parsed().quotes
        assert all(q.identity_raw_ref and "markets" in q.identity_raw_ref for q in quotes)

    def test_observed_at_is_the_orders_fetch(self) -> None:
        import glob

        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        orders_times = {
            store.read(p).fetched_at
            for p in sorted(glob.glob("tests/fixtures/raw/sxbet__*orders*.json"))
        }
        assert all(q.observed_at in orders_times for q in self._parsed().quotes)


class TestATruncatedPageIsNotAnEmptyOrderBook:
    """``/orders`` returns at most 100 records and says so nowhere.

    Two captured responses asked for 25 and 23 market hashes and **both** came
    back with exactly 100 orders covering 23 markets.  The two that fell off
    were filed as ``no_resting_order_for_outcome`` — a skip whose comment reads
    "Nobody is offering this side.  Ordinary on a thin order book" — so an API
    page limit was recorded as a fact about the venue's liquidity, and
    validation then faulted the venue for the market being absent.
    """

    def test_the_captured_truncation_is_named_as_truncation(self) -> None:
        import glob

        from src.raw_store import RawStore
        from src.sources.sxbet import parse_sxbet

        store = RawStore("tests/fixtures/raw")
        raws = [store.read(p) for p in sorted(glob.glob("tests/fixtures/raw/sxbet__*.json"))]
        skipped = parse_sxbet(raws).skipped
        assert skipped["orders_response_truncated"] == 4, dict(skipped)
        # And the genuinely-empty ones keep their own reason.
        assert skipped["no_resting_order_for_outcome"] == 4, dict(skipped)

    def test_a_batch_at_the_cap_is_split_and_asked_again(self) -> None:
        import httpx

        from src.sources.sxbet import ORDERS_PAGE_CAP, SxBetAdapter

        asked: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            if path.endswith("/orders"):
                hashes = request.url.params.get("marketHashes", "").split(",")
                asked.append(len(hashes))
                # Full page while the batch is large; a short one once split.
                count = ORDERS_PAGE_CAP if len(hashes) > 4 else 3
                return httpx.Response(200, json={"data": [
                    {"marketHash": hashes[0], "orderStatus": "ACTIVE"} for _ in range(count)
                ]})
            return httpx.Response(200, json={"data": []})

        source = SxBetAdapter(
            ["MLB"], client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        source._fetch_orders(Sport.BASEBALL, [f"0x{n:064x}" for n in range(20)])
        assert asked[0] == 20, asked
        assert min(asked) <= 4, asked
        assert sum(asked[1:]) >= 20, "the split batches must cover every hash"


class TestKalshiChecksStatusOnTheThingThatCarriesThePrice:
    """The status test ran once per *fixture*, in ``_accept_game``.

    That runs only for the first market of a game, so every other market of the
    same game was never checked at all — and the row builder hard-codes
    ``QuoteStatus.ACTIVE``.  A settled or closed contract beside an active one
    was therefore published as a takeable price at its stale last ask.  When the
    closed one came *first* it was worse: the skip was counted and the row
    emitted anyway, because the next market re-entered ``_accept_game`` and
    established the fixture, leaving the counter asserting a drop that had not
    happened.  Both sibling exchanges test this per market.
    """

    def _capture(self, closed: set[str]):
        """The real MLB capture with named tickers marked closed."""
        import glob

        from src.raw_store import RawStore
        from src.sources.kalshi import parse_kalshi

        store = RawStore("tests/fixtures/raw")
        raws = []
        for path in sorted(glob.glob("tests/fixtures/raw/kalshi__*.json")):
            raw = store.read(path)
            payload = raw.json()
            if isinstance(payload, dict) and payload.get("markets"):
                for market in payload["markets"]:
                    if market.get("ticker") in closed:
                        market["status"] = "closed"
                raw = dataclasses.replace(raw, body=json.dumps(payload))
            raws.append(raw)
        return parse_kalshi(raws)

    #: One fixture, two contracts.  Closing either leaves the other active.
    PAIR = (
        "KXMLBGAME-26JUL302210SEALAD-SEA",
        "KXMLBGAME-26JUL302210SEALAD-LAD",
    )

    def test_both_sides_are_published_when_both_are_active(self) -> None:
        published = {q.source_selection_id for q in self._capture(set()).quotes}
        assert set(self.PAIR) <= published, "the control produced nothing to lose"

    @pytest.mark.parametrize("index", [0, 1])
    def test_a_closed_contract_is_not_published_beside_an_active_one(
        self, index: int
    ) -> None:
        """Parametrised over *which* one is closed: the defect only showed on
        the sibling that was not the first market of its fixture, and closing
        the first was the ordering that also mis-counted the skip.
        """
        closed, other = self.PAIR[index], self.PAIR[1 - index]
        outcome = self._capture({closed})
        published = {q.source_selection_id for q in outcome.quotes}
        assert closed not in published
        assert other in published, "only the closed contract should be dropped"
        assert outcome.skipped["market_status:closed"] == 1, dict(outcome.skipped)


class TestKambiReadsThePeriodRatherThanInferringIt:
    """``TEAM_TOTAL_AFFIXES`` pairs a prefix with an **empty** suffix.

    ``endswith("")`` is vacuously true, so the pair was a bare prefix test with
    no anchor on the right, and whatever trailed the club name went to
    ``canonical_participant`` — which is deliberately loose and resolves
    "Cincinnati Reds - First 5 Innings" to MLB-CIN.  The rule then returned
    ``Period.FULL_GAME`` regardless, so a five-inning team total would be stored
    and compared as a nine-inning one.
    """

    def _side(self, label):
        from src.sources.betrivers_kambi import TEAM_TOTAL_AFFIXES, _team_total_side
        from src.leagues import league as get_league
        from src.participants import canonical_participant
        from src.sources.betrivers_kambi import _Fixture

        competition = get_league("MLB")
        home = canonical_participant("Cincinnati Reds", competition)
        away = canonical_participant("Philadelphia Phillies", competition)
        fixture = _Fixture(
            event_id="1", sport=Sport.BASEBALL, competition=competition,
            home=home, away=away, commence_time=LATER, base_key="k",
        )
        return _team_total_side(label, TEAM_TOTAL_AFFIXES[Sport.BASEBALL], fixture)

    def test_the_unqualified_team_total_still_resolves(self) -> None:
        from src.schema import Side

        assert self._side("Total Runs by Cincinnati Reds") is Side.HOME
        assert self._side("Total Runs by Philadelphia Phillies") is Side.AWAY

    @pytest.mark.parametrize("label", [
        "Total Runs by Cincinnati Reds - First 5 Innings",
        "Total Runs by Cincinnati Reds - Inning 1",
        "Total Runs by Philadelphia Phillies - First 5 Innings",
    ])
    def test_a_period_qualified_team_total_is_refused(self, label: str) -> None:
        assert self._side(label) is None


class TestFanDuelReadsTheFeedsOwnLivenessFlag:
    """Pregame scope was decided by ``commence_time <= captured_at``, a proxy
    for a question the payload answers directly.

    On the captured tennis slate the two agree on 186 of 187 fixtures and
    disagree on one — a market whose ``openDate`` is 99 seconds after the fetch
    and whose ``inPlay`` is already ``true``.  972 captured markets carry the
    flag and 46 of them are true.
    """

    def test_the_flag_is_read_from_the_captured_bytes(self) -> None:
        import glob

        from src.raw_store import RawStore
        from src.sources.fanduel import parse_fanduel

        store = RawStore("tests/fixtures/raw")
        raws = [store.read(p) for p in sorted(glob.glob("tests/fixtures/raw/fanduel__*.json"))]
        outcome = parse_fanduel(raws)
        assert outcome.skipped["market_in_play"] > 0, dict(outcome.skipped)
        assert outcome.quotes, "the flag must not empty the slate"


class TestMatchbookDoesNotPublishAPriceNothingIsAvailableAt:
    """The comment said a stated zero "must not be recorded as" no size stated,
    and the next line did exactly that, because the schema refuses a
    non-positive limit and ``None`` was the only value left.

    The effect was a leg that can match nothing reading as a leg of *unknown*
    size, which ``max_total_stake`` treats as no constraint — so a position
    resting on it was reported uncapped.
    """

    def test_a_zero_size_price_is_dropped_not_published_unlimited(self) -> None:
        import glob
        import json

        from src.raw_store import RawStore
        from src.sources.matchbook import parse_matchbook

        store = RawStore("tests/fixtures/raw")
        paths = sorted(glob.glob("tests/fixtures/raw/matchbook__*.json"))
        assert paths, "no matchbook capture to work from"

        zeroed = 0
        raws = []
        for path in paths:
            raw = store.read(path)
            payload = raw.json()
            if isinstance(payload, dict):
                for event in payload.get("events") or []:
                    for market in event.get("markets") or []:
                        for runner in market.get("runners") or []:
                            for price in runner.get("prices") or []:
                                if price.get("side") == "back":
                                    price["available-amount"] = 0
                                    zeroed += 1
                raw = dataclasses.replace(raw, body=json.dumps(payload))
            raws.append(raw)
        assert zeroed, "the capture has no back prices to zero"

        outcome = parse_matchbook(raws)
        # Not one surviving row with an unknown size: every back price in the
        # capture now states zero, so every one that reached the size check was
        # dropped there rather than published as "no size stated".  (The counter
        # is lower than *zeroed* because prices in out-of-scope markets are
        # dropped earlier, for their own counted reasons.)
        assert outcome.skipped["available_amount_not_positive"] > 0
        assert [q for q in outcome.quotes if q.limit_amount is None] == []


class TestSmarketsJudgesAStartAgainstThePriceNotThePassStart:
    """The started-game gate compared the kick-off against the **earliest**
    response in the capture, which is the moment the pass began.

    This venue is paced to its own published rate limit — 3.1 s between
    requests, 48 to 156 requests — so a pass runs two and a half to eight
    minutes, and the quotes batch pricing the last sport lands minutes after the
    events page that listed it.  A fixture starting inside that window cleared
    the gate and was emitted ACTIVE and pregame off an in-play price.  Matchbook,
    given the same fixture and the same fetch time, emitted nothing and counted
    the skip — because its pass is seconds long, so its earliest response is
    also its latest and the two readings coincide.
    """

    def _capture(self, *, quotes_offset_seconds: int):
        """The real capture, with the quotes response dated later in the pass
        and the fixture starting between it and the events page."""
        import glob

        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        raws = [
            store.read(p)
            for p in sorted(glob.glob("tests/fixtures/raw/smarkets__*.json"))
        ]
        assert raws, "no smarkets capture to work from"
        began = min(raw.fetched_at for raw in raws)
        priced_at = began + timedelta(seconds=quotes_offset_seconds)
        kickoff = began + timedelta(seconds=quotes_offset_seconds // 2)

        out = []
        for raw in raws:
            payload = raw.json()
            if raw.endpoint.startswith("events") and isinstance(payload, dict):
                for event in payload.get("events") or []:
                    event["start_datetime"] = kickoff.strftime("%Y-%m-%dT%H:%M:%SZ")
                raw = dataclasses.replace(raw, body=json.dumps(payload))
            elif raw.endpoint.startswith("quotes"):
                raw = dataclasses.replace(raw, fetched_at=priced_at)
            out.append(raw)
        return out

    def test_a_fixture_that_starts_mid_pass_is_not_priced_pregame(self) -> None:
        from src.sources.smarkets import parse_smarkets

        outcome = parse_smarkets(self._capture(quotes_offset_seconds=480))
        assert outcome.quotes == [], [
            (q.event_key, q.observed_at.isoformat(), q.commence_time.isoformat())
            for q in outcome.quotes[:3]
        ]
        assert outcome.skipped["event_already_started"] > 0, dict(outcome.skipped)

    def test_a_fixture_still_ahead_of_the_price_is_kept(self) -> None:
        """The control.  A pass that finishes before kick-off loses nothing."""
        from src.sources.smarkets import parse_smarkets

        import glob

        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        raws = [
            store.read(p)
            for p in sorted(glob.glob("tests/fixtures/raw/smarkets__*.json"))
        ]
        outcome = parse_smarkets(raws)
        assert outcome.quotes, "the untouched capture must still produce rows"


class TestAnOrderBookIsNotASportsbook:
    """``source_prices_itself_to_lose`` applied the sportsbook reading to every
    venue, contradicting validation on exactly the venues validation carves out.

    A sportsbook holds an edge on every market it posts, so a sum below 1.0
    there is evidence about the parser.  An order book is not that: it shows
    what two strangers left resting, and a briefly crossed book is a real —
    small, fleeting — intra-venue arbitrage.  The detector deleted the venue
    from the market with the message "which does not happen" about something
    that does, and took the surviving cross-venue position with it.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _report(self, source, home, away):
        from src.arb import find_opportunities

        rows = [
            make_quote(source=source, selection=Selection.HOME,
                       decimal_odds=home, source_market_id="ml"),
            make_quote(source=source, selection=Selection.AWAY,
                       decimal_odds=away, source_market_id="ml"),
            # A normal sportsbook beside it: overround above 1.0.
            make_quote(source="fanduel", selection=Selection.HOME,
                       decimal_odds=1.90, source_market_id="m2"),
            make_quote(source="fanduel", selection=Selection.AWAY,
                       decimal_odds=1.95, source_market_id="m2"),
        ]
        return find_opportunities(rows, as_of=self.AS_OF, commissions={})

    def test_a_cent_of_crossing_on_an_exchange_is_not_a_parser_fault(self) -> None:
        """Kalshi's two best asks at 0.5000 / 0.4995.  This module's own notes
        record its tightest live moneylines summing to exactly 1.0000 — one cent
        of movement away from here.
        """
        report = self._report("kalshi", 1 / 0.5000, 1 / 0.4995)
        assert [d.code for d in report.diagnostics] == []

    def test_the_same_prices_at_a_sportsbook_are_still_a_fault(self) -> None:
        report = self._report("bovada", 1 / 0.5000, 1 / 0.4995)
        assert "source_prices_itself_to_lose" in [d.code for d in report.diagnostics]

    def test_a_large_crossed_book_is_still_a_fault_on_an_exchange(self) -> None:
        """The floor is widened, not removed: a market crossed by more than a
        couple of percent does not survive the seconds it takes to collect it.
        """
        report = self._report("kalshi", 1 / 0.45, 1 / 0.45)
        assert "source_prices_itself_to_lose" in [d.code for d in report.diagnostics]

    def test_the_trap_gate_is_validations_own(self) -> None:
        """The flat 0.99 floor this pinned was retired: per-market severity is
        judged on the net-of-commission sum, and the bid-read-as-ask trap the
        floor was calibrated against is caught at the source level by rate —
        where a systematic fault actually lives."""
        from src.validation import MIN_SUB_UNITY_MARKETS

        # The rate threshold this once pinned is gone: live data put honest
        # sources at 15-20% by that measure and a realistic trap at 18%, so the
        # bands overlapped.  The gate is a median now, and its only tunable is
        # how many markets a middle needs to mean anything.
        assert MIN_SUB_UNITY_MARKETS >= 3


class TestAPinnedClubStaysPinnedUnderAMarker:
    """``_SOCCER_DISAMBIGUATIONS`` was looked up on the whole name, so any
    identity marker defeated it.

    "Barcelona SC W" missed the pin, fell through to the stripping below, lost
    ``SC`` as a club-type token and landed on ``barcelonaw`` — the same slug as
    "Barcelona W", which is the exact merge the pin exists to prevent.  It is
    the identical hole the alias path had, one lookup over, and it stayed open
    when that one was closed.
    """

    @pytest.mark.parametrize("pinned,other", [
        ("Barcelona SC", "Barcelona"),
        ("Barcelona SC W", "Barcelona W"),
        ("Barcelona SC II", "Barcelona II"),
        ("CD Nacional", "Nacional"),
        ("CD Nacional II", "Nacional II"),
        ("CD Nacional Reserves", "Nacional Reserves"),
    ])
    def test_the_two_clubs_never_share_a_slug(self, pinned, other) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        left = _open_slug(pinned, sport=LeagueSport.SOCCER)
        right = _open_slug(other, sport=LeagueSport.SOCCER)
        assert left and right and left != right, (pinned, left, other, right)

    def test_the_marker_still_separates_the_womens_side(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        senior = _open_slug("Barcelona SC", sport=LeagueSport.SOCCER)
        womens = _open_slug("Barcelona SC W", sport=LeagueSport.SOCCER)
        assert senior != womens


class TestTheLinesSurfaceSaysWhyTheDetectorRefuses:
    """A sum below 1.0 was printed the same whether the detector would act on it
    or refuse it outright.

    Commission was the first way these two surfaces contradicted each other and
    was fixed; the other two gates stayed open.  A fanduel/kalshi pair prints a
    7.2% apparent edge and is rejected as ``legs_do_not_void_together``; two
    mirrored tenants print an edge while the run reports no opportunities and
    emits no diagnostic at all, so nothing anywhere explains the contradiction.
    """

    def _note(self, legs, one_counterparty=None):
        from src.collector import _why_the_detector_would_refuse

        return _why_the_detector_would_refuse(legs, one_counterparty or {})

    def test_a_settlement_clash_is_named(self) -> None:
        legs = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10),
            make_quote(source="kalshi", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        note = self._note(legs)
        assert note is not None and "void together" in note

    def test_one_book_under_two_names_is_named(self) -> None:
        from src.arb import EVERY_LEAGUE

        legs = [
            make_quote(source="tenant_a", selection=Selection.HOME, decimal_odds=2.10),
            make_quote(source="tenant_b", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        note = self._note(
            legs, {EVERY_LEAGUE: [frozenset({"tenant_a", "tenant_b"})]}
        )
        assert note is not None and "one counterparty" in note

    def test_an_ordinary_pair_gets_no_note(self) -> None:
        legs = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=2.30),
        ]
        assert self._note(legs) is None


class TestTheChargeAndTheSettlementRuleArePinnedPerVenue:
    """The registry checked only that each venue *has* an entry, never what it
    says, so the numbers the whole margin calculation rests on were unpinned.

    Changing Polymarket's fee shape from ``min(p, 1−p)`` to ``p×(1−p)`` left the
    suite fully green.  At even money that halves the modelled fee — net odds
    move 1.9048 to 1.9512, i.e. 0.0125 of implied probability **per leg**, which
    is larger than the entire cross-book edge the detector exists to find, and
    in the edge-manufacturing direction.

    These are the published standard rates, i.e. the least favourable reading:
    no promotional or volume tier is assumed, because assuming one invents edge
    that a real account would not get.
    """

    EXPECTED = {
        "an_bet365": "no commission (the venue's margin is already in the price)",
        "an_betmgm": "no commission (the venue's margin is already in the price)",
        "an_betrivers": "no commission (the venue's margin is already in the price)",
        "an_caesars": "no commission (the venue's margin is already in the price)",
        "an_bovada": "no commission (the venue's margin is already in the price)",
        "an_draftkings": "no commission (the venue's margin is already in the price)",
        "an_fanduel": "no commission (the venue's margin is already in the price)",
        "an_onexbet": "no commission (the venue's margin is already in the price)",
        "an_open": "no commission (the venue's margin is already in the price)",
        "an_hardrock": "no commission (the venue's margin is already in the price)",
        "an_fanatics": "no commission (the venue's margin is already in the price)",
        "an_bally": "no commission (the venue's margin is already in the price)",
        "an_parx": "no commission (the venue's margin is already in the price)",
        "an_unibet": "no commission (the venue's margin is already in the price)",
        "an_thescore": "no commission (the venue's margin is already in the price)",
        "vi_draftkings": "no commission (the venue's margin is already in the price)",
        "vi_caesars": "no commission (the venue's margin is already in the price)",
        "vi_hardrock": "no commission (the venue's margin is already in the price)",
        "vi_fanatics": "no commission (the venue's margin is already in the price)",
        "vi_bet365": "no commission (the venue's margin is already in the price)",
        "vi_betmgm": "no commission (the venue's margin is already in the price)",
        "vi_fanduel": "no commission (the venue's margin is already in the price)",
        "vi_betrivers": "no commission (the venue's margin is already in the price)",
        "vsin_circa": "no commission (the venue's margin is already in the price)",
        "betmgm": "no commission (the venue's margin is already in the price)",
        "betrivers_kambi": "no commission (the venue's margin is already in the price)",
        "bovada": "no commission (the venue's margin is already in the price)",
        "cloudbet": "no commission (the venue's margin is already in the price)",
        "caesars": "no commission (the venue's margin is already in the price)",
        "draftkings": "no commission (the venue's margin is already in the price)",
        "fanduel": "no commission (the venue's margin is already in the price)",
        "hardrock": "no commission (the venue's margin is already in the price)",
        "kalshi": '0.07 × p×(1−p) per contract, charged on entry',
        "leovegas_kambi": "no commission (the venue's margin is already in the price)",
        "matchbook": '2.00% of net winnings',
        "onexbet": "no commission (the venue's margin is already in the price)",
        "pinnacle": "no commission (the venue's margin is already in the price)",
        "polymarket": '0.05 × min(p, 1−p) per contract, charged on entry',
        "smarkets": '2.00% of net winnings',
        "sxbet": '5.00% of net winnings',
    }

    REGIMES = {
        "an_bet365": 'void_and_refund',
        "an_betmgm": 'void_and_refund',
        "an_betrivers": 'void_and_refund',
        "an_caesars": 'void_and_refund',
        "an_bovada": 'void_and_refund',
        "an_draftkings": 'void_and_refund',
        "an_fanduel": 'void_and_refund',
        "an_onexbet": 'void_and_refund',
        "an_open": 'void_and_refund',
        "an_hardrock": 'void_and_refund',
        "an_fanatics": 'void_and_refund',
        "an_bally": 'void_and_refund',
        "an_parx": 'void_and_refund',
        "an_unibet": 'void_and_refund',
        "an_thescore": 'void_and_refund',
        "vi_draftkings": 'void_and_refund',
        "vi_caesars": 'void_and_refund',
        "vi_hardrock": 'void_and_refund',
        "vi_fanatics": 'void_and_refund',
        "vi_bet365": 'void_and_refund',
        "vi_betmgm": 'void_and_refund',
        "vi_fanduel": 'void_and_refund',
        "vi_betrivers": 'void_and_refund',
        "vsin_circa": 'void_and_refund',
        "betmgm": 'void_and_refund',
        "betrivers_kambi": 'void_and_refund',
        "bovada": 'void_and_refund',
        "cloudbet": 'void_and_refund',
        "caesars": 'void_and_refund',
        "draftkings": 'void_and_refund',
        "fanduel": 'void_and_refund',
        "hardrock": 'void_and_refund',
        "kalshi": 'settle_make_up_game',
        "leovegas_kambi": 'void_and_refund',
        "matchbook": 'void_and_refund',
        "onexbet": 'void_and_refund',
        "pinnacle": 'void_and_refund',
        "polymarket": 'resolve_fifty_fifty',
        "smarkets": 'void_and_refund',
        "sxbet": 'void_and_refund',
    }

    def test_every_venues_charge_is_what_it_is(self) -> None:
        from src.commission import COMMISSIONS

        assert {key: value.describe() for key, value in COMMISSIONS.items()} == self.EXPECTED

    def test_every_venues_settlement_regime_is_what_it_is(self) -> None:
        from src.settlement import SETTLEMENT

        assert {key: regime.value for key, regime in SETTLEMENT.items()} == self.REGIMES

    def test_the_two_fee_shapes_are_not_interchangeable(self) -> None:
        """Why the shape matters and not just the rate: at even money the two
        differ by half the fee, and they differ in opposite directions away from
        it."""
        from src.commission import ContractFeeCommission

        published = ContractFeeCommission(rate=0.05, shape="min_p_q")
        other = ContractFeeCommission(rate=0.05, shape="p_times_q")
        at_even = [published.net_decimal(2.0), other.net_decimal(2.0)]
        assert abs(at_even[0] - at_even[1]) > 0.04, at_even


class TestPolymarketReadsTheLeagueFromThePayload:
    """The competition came entirely from the *request's* tag slug.

    It matters because participant resolution is not injective across leagues —
    ``canonical_participant("San Francisco Giants", NFL)`` is NFL-NYG and
    ``("New York Giants", MLB)`` is MLB-SF — so one cross-tagged event becomes a
    wrong-league fixture with entirely *plausible* participants rather than a
    loud refusal, and nothing downstream can see it.

    The guard has to compare the **competition**, not the text.  Comparing
    ``seriesSlug`` verbatim rejected 124 real events on the first live run and
    marked the source unhealthy, because that field carries the season:
    ``mls-2025`` and ``nfl-2026`` against routes named ``mls`` and ``nfl``.  The
    committed capture holds only the unsuffixed ``mlb``, so the test written
    against it passed while the live slate broke.
    """

    def _capture(self, *, series=None, team_league=None):
        import glob

        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        raws = []
        for path in sorted(glob.glob("tests/fixtures/raw/polymarket__*.json")):
            raw = store.read(path)
            payload = raw.json()
            if isinstance(payload, list) and payload:
                if series is not None:
                    payload[0]["seriesSlug"] = series
                if team_league is not None:
                    for team in payload[0].get("teams") or []:
                        team["league"] = team_league
                raw = dataclasses.replace(raw, body=json.dumps(payload))
            raws.append(raw)
        return raws

    def _refusals(self, **edits):
        from src.sources.polymarket import parse_polymarket

        outcome = parse_polymarket(self._capture(**edits))
        return outcome, [
            r for r in outcome.rejections
            if r.reason == "event_league_disagrees_with_its_route"
        ]

    def test_the_untouched_capture_still_parses(self) -> None:
        outcome, named = self._refusals()
        assert outcome.quotes
        assert named == []

    def test_an_event_tagged_into_the_wrong_competition_is_refused(self) -> None:
        """Restating the *authoritative* field — the one every team carries."""
        outcome, named = self._refusals(team_league="nfl")
        assert named, [r.reason for r in outcome.rejections]
        assert all("nfl" in rejection.detail for rejection in named)
        assert outcome.quotes, "only the restated events should be refused"

    @pytest.mark.parametrize("series", ["mlb-2025", "mlb-2026", "mlb"])
    def test_a_season_suffixed_series_is_the_same_competition(self, series) -> None:
        """The regression the first version of this guard caused: 124 live
        events refused because the venue names its seasons.
        """
        outcome, named = self._refusals(series=series)
        assert named == [], [r.detail for r in named]
        assert outcome.quotes

    def test_a_competition_this_adapter_does_not_route_is_not_judged(self) -> None:
        """Abstaining is the only honest answer for a name we cannot resolve:
        the guard exists to catch a *wrong* competition, not to insist the venue
        spell its own the way we do.
        """
        outcome, named = self._refusals(series="some-cup-nobody-routes")
        assert named == []
        assert outcome.quotes

    def test_the_participants_it_would_have_produced_are_plausible(self) -> None:
        """Why this has to be a refusal rather than a best effort: the wrong
        answer does not look wrong.
        """
        from src.leagues import league as get_league
        from src.participants import canonical_participant

        crossed = canonical_participant("San Francisco Giants", get_league("NFL"))
        assert crossed is not None and crossed.key == "NFL-NYG"
        other = canonical_participant("New York Giants", get_league("MLB"))
        assert other is not None and other.key == "MLB-SF"


def _health_card(data, source_key: str):
    """The per-source card for *source_key*, wherever the report nests it."""
    for run in data.get("runs") or []:
        for entry in run.get("health") or run.get("sources") or []:
            if entry.get("key") == source_key or entry.get("source_key") == source_key:
                return entry
    raise AssertionError(f"no health card for {source_key} in {sorted(data)}")


class TestTheDashboardShowsWhatASourceWasRefused:
    """``scopes_requested`` and ``scopes_refused`` reached the store and the CLI
    summary and stopped there.

    ``grep`` for either name in the reporting layer returned nothing, so the
    per-source card showed ``ok: true`` for a book that had lost most of its
    leagues — the exact state the instrumentation exists to make visible, hidden
    again by the surface most likely to be read.
    """

    def test_the_per_source_card_carries_the_scope_columns(self, tmp_path) -> None:
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="book_a", ok=True, checked_at=FETCHED, quote_count=7,
                scopes_requested=6,
                # Two messages, one distinct scope — the dashboard must print
                # the distinct count, not ``len(scopes_refused)``.
                scopes_failed=1,
                failed_scopes=("mlb: metadata half", "mlb: order-book half"),
            ))
            store.save_quotes(run, [make_quote(source="book_a")])
            store.finish_run(
                run, finished_at=FETCHED, report=ValidationReport()
            )
            data = build_report(store)

        card = _health_card(data, "book_a")
        assert card["scopes_requested"] == 6
        assert card["scopes_failed"] == 1
        assert card["scopes_refused"] == [
            "mlb: metadata half", "mlb: order-book half",
        ]
        assert card["scopes_failed"] != len(card["scopes_refused"])

    def test_a_migrated_row_with_refusal_messages_is_not_shown_as_clean(
        self, tmp_path
    ) -> None:
        """``scopes_failed`` defaults to 0 on additive upgrade.  A pre-upgrade
        row that still names refusals must not render as having lost nothing.
        """
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="book_a", ok=True, checked_at=FETCHED, quote_count=7,
                scopes_requested=2,
                scopes_failed=0,
                failed_scopes=("EPL: blocked", "MLS: blocked"),
            ))
            store.save_quotes(run, [make_quote(source="book_a")])
            store.finish_run(
                run, finished_at=FETCHED, report=ValidationReport()
            )
            data = build_report(store)

        card = _health_card(data, "book_a")
        assert card["scopes_failed"] == 2
        assert card["scopes_refused"] == ["EPL: blocked", "MLS: blocked"]

    def test_colon_qualified_scope_names_are_not_collapsed(self, tmp_path) -> None:
        """Pinnacle fallback scopes are ``MLB:246`` / ``MLB:247``.  Splitting
        refusal messages on the first colon alone counted both as one.
        """
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="pinnacle", ok=True, checked_at=FETCHED, quote_count=7,
                scopes_requested=3,
                scopes_failed=0,
                failed_scopes=(
                    "MLB:246: HTTP 403: blocked",
                    "MLB:247: HTTP 403: blocked",
                ),
            ))
            store.save_quotes(run, [make_quote(source="pinnacle")])
            store.finish_run(
                run, finished_at=FETCHED, report=ValidationReport()
            )
            data = build_report(store)

        card = _health_card(data, "pinnacle")
        assert card["scopes_failed"] == 2, card
        assert card["scopes_refused"][0].startswith("MLB:246:")

    def test_a_source_that_lost_nothing_carries_an_empty_list(self, tmp_path) -> None:
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="book_a", ok=True, checked_at=FETCHED, quote_count=7,
                scopes_requested=3,
            ))
            store.save_quotes(run, [make_quote(source="book_a")])
            store.finish_run(
                run, finished_at=FETCHED, report=ValidationReport()
            )
            data = build_report(store)

        card = _health_card(data, "book_a")
        assert card["scopes_refused"] == []


class TestAThinOrderBookIsNotAMispairedRow:
    """``prices_disagree_with_every_other_source`` applied the sportsbook
    reading to an order book and failed a live run for it.

    A sportsbook posts both sides of a market it is making, so a price nothing
    like the consensus there means the row is not the bet it claims to be —
    which is exactly what the check's own message says.  An exchange posts
    whatever somebody left resting, with nobody market-making to hold it near
    fair value, so its best *takeable* price on a side nobody wants can be
    anything at all.

    Live: Matchbook's Mannarino/Michelsen moneyline, home 1.400 against a
    1.408/1.417/1.423 consensus and away 1.05 against ~2.9 — with 1660.56
    resting behind the home price and 6.64 behind the away one.  Six such rows
    out of 514 graded the run an ERROR.

    The discriminator needs no new threshold: every fault the check names — a
    handicap on the wrong competitor, a selection mapped to the wrong side, a
    market filed as another market — moves **every** side of the market
    together.  One outlying side beside a side that agrees to three decimal
    places is the one thing none of them can produce.
    """

    ORDER_DRIVEN = frozenset({"matchbook", "smarkets", "kalshi", "polymarket", "sxbet"})

    def _rows(self, *, suspect, home, away, count=12):
        """*count* fixtures priced by three books plus one suspect venue."""
        rows = []
        for index in range(count):
            key = f"TENNIS-a{index}@TENNIS-b{index}:2026-07-29"
            common = dict(
                sport=Sport.TENNIS, league="ATP", event_key=key,
                source_event_id=f"e{index}", home_participant=f"TENNIS-b{index}",
                away_participant=f"TENNIS-a{index}", home_team=f"B {index}",
                away_team=f"A {index}", commence_time=LATER, market=Market.MONEYLINE,
            )
            for source, h, a in (
                ("bovada", 1.417, 2.950),
                ("fanduel", 1.408, 2.920),
                ("pinnacle", 1.423, 2.807),
                (suspect, home, away),
            ):
                rows.append(make_quote(source=source, selection=Selection.HOME,
                                       decimal_odds=h, **common))
                rows.append(make_quote(source=source, selection=Selection.AWAY,
                                       decimal_odds=a, **common))
        return rows

    def _errors(self, rows):
        from src.validation import Severity, validate

        report = validate(rows, order_book_sources=self.ORDER_DRIVEN)
        return [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
            and f.severity is Severity.ERROR
        ]

    def test_one_thin_side_on_an_exchange_does_not_fail_the_run(self) -> None:
        """The live shape: the home price agrees, the away price is whatever was
        resting."""
        assert self._errors(
            self._rows(suspect="matchbook", home=1.400, away=1.050)
        ) == []

    def test_the_same_shape_at_a_sportsbook_still_fails(self) -> None:
        """A book that is making the market cannot post 1.05 against a 2.9
        consensus and mean it."""
        found = self._errors(self._rows(suspect="bovada2", home=1.400, away=1.050))
        assert found and found[0].source == "bovada2"

    def test_a_swapped_mapping_on_an_exchange_still_fails(self) -> None:
        """The control that matters: a selection mapped to the wrong side moves
        *both* prices, which is what the check exists to catch and what the
        exemption must not swallow."""
        found = self._errors(
            self._rows(suspect="matchbook", home=2.920, away=1.408)
        )
        assert found and found[0].source == "matchbook"

    def test_an_exchange_that_agrees_is_never_reported(self) -> None:
        assert self._errors(
            self._rows(suspect="matchbook", home=1.410, away=2.930)
        ) == []


class TestTheWomensMarkerReachesEveryVenueThatNeedsIt:
    """The guard existed and was wired into **one** of ten adapters.

    ``competition_marker`` synthesises the marker a venue puts on the
    *competition* rather than on the teams, so a women's fixture and the men's
    fixture of the same two clubs do not collapse onto one event key.  It was
    imported by ``src/sources/pinnacle.py`` and by nothing else, while FanDuel's
    own soccer page carries, by name, "English Women's Championship", "German
    Frauen-Bundesliga", "Mexican Liga MX Femenil", "Norwegian Toppserien
    Ladies", "Australian A-League - Women" and "Friendlies Women's
    International" — every one of them producing a men's key.

    Nothing downstream can see the result: ``dedup_key`` contains ``source`` so
    the UNIQUE constraint is blind across venues, the participant pair is
    identical so ``participant_pair_disagreement`` cannot fire, and
    ``_fixture_outliers`` deliberately does not compare ``commence_time``.  The
    recorded cost of this shape at Pinnacle was an 11.2% "guaranteed" position
    ranked first in the whole run.
    """

    #: Every adapter that maps an unrecognised competition into a catch-all
    #: league, and therefore stops reading the venue's own name for it.
    NEEDS_MARKER = (
        "pinnacle", "fanduel", "betrivers_kambi", "leovegas_kambi",
        "smarkets", "matchbook", "sxbet",
        "an_draftkings", "an_caesars", "an_bet365", "an_open",
        "an_fanduel", "an_betrivers", "an_betmgm",
        "an_bovada", "an_onexbet",
        "an_hardrock", "an_fanatics", "an_bally",
        # Same Action Network class, so the same catch-all exposure.
        "an_parx", "an_unibet", "an_thescore",
    )

    #: The rest configure one named competition per route, so a women's fixture
    #: cannot arrive under a men's league key in the first place.
    IMMUNE = (
        "betmgm", "bovada", "caesars", "cloudbet", "draftkings", "hardrock",
        "kalshi", "onexbet", "polymarket", "vi_draftkings", "vi_caesars",
        "vi_hardrock", "vi_fanatics", "vi_bet365",
        # VegasInsider routes name one league each (see its ``ROUTES``), so a
        # women's fixture cannot arrive under a men's key.
        "vi_betmgm", "vi_fanduel", "vi_betrivers",
        "vsin_circa",
    )

    def test_every_catch_all_adapter_applies_the_marker(self) -> None:
        import importlib

        for key in self.NEEDS_MARKER:
            if key.endswith("kambi"):
                module = "src.sources.betrivers_kambi"
            elif key.startswith("an_"):
                module = "src.sources.actionnetwork"
            else:
                module = f"src.sources.{key}"
            source = importlib.import_module(module)
            assert hasattr(source, "competition_marker"), (
                f"{key} maps unrecognised competitions to a catch-all league and "
                "does not apply the competition marker"
            )

    def test_the_immune_adapters_are_immune_for_the_stated_reason(self) -> None:
        """Pins the claim rather than the absence: each of these routes names
        exactly one competition, so there is nothing to disambiguate."""
        from src.sources.bovada import LEAGUE_PATHS
        from src.sources.polymarket import TAG_ROUTES

        assert all(path.league for path in LEAGUE_PATHS)
        assert all(route.league for route in TAG_ROUTES)

    def test_the_two_lists_account_for_every_registered_source(self) -> None:
        """The lists above partition the registry, and nothing said so.

        Both are hand-written, so a source added to the registry joined neither
        — and the failure mode is silent in the direction that costs money: the
        eleventh adapter maps unrecognised competitions to a catch-all, is on
        neither list, and the marker test above simply never asks about it.  A
        women's fixture then arrives under the men's league key and is joined to
        the men's game across venues, which is the fault this whole class
        exists to prevent.

        Checked as a partition, in both directions: every registered key is on
        exactly one list, and neither list names a key that is not registered.
        """
        from src.sources.registry import keys

        registered = set(keys())
        listed = [*self.NEEDS_MARKER, *self.IMMUNE]
        assert len(listed) == len(set(listed)), "a source is on both lists"
        assert set(listed) == registered, {
            "on neither list": sorted(registered - set(listed)),
            "not registered": sorted(set(listed) - registered),
        }


class TestTheWomensMarkerSpeaksMoreThanTwoLanguages:
    """``\\bwomen'?s?\\b|\\bladies\\b|\\bfem(?:enin[ao])?\\b`` matched English and
    Spanish and nothing else.

    So the German Frauen-Bundesliga, Italian Serie A Femminile, Brazilian
    Campeonato Brasileiro Feminino, Mexican Liga MX Femenil, Dutch Eredivisie
    Vrouwen, French D1 Féminine, the Scandinavian leagues and the WSL all
    produced a **men's** event key — the exact fault this marker exists to
    prevent, in every language but two.  Brazil is not hypothetical:
    ``football/brazil`` is a default Kambi path and Pinnacle routes unrecognised
    soccer into the catch-all.
    """

    WOMENS = [
        "Norwegian Toppserien Women", "German Frauen-Bundesliga",
        "Italy - Serie A Femminile", "Brazil - Campeonato Brasileiro Feminino",
        "Mexico - Liga MX Femenil", "Netherlands - Eredivisie Vrouwen",
        "English Women's Championship", "Spain - Liga F Femenino",
        "France - D1 Feminine", "Australian A-League - Women",
        "Norwegian Toppserien Ladies", "Denmark - Kvindeliga",
        "Sweden - Damallsvenskan", "England - WSL", "USA - NWSL",
        "Club Friendlies Women",
    ]

    MENS = [
        "Germany - Bundesliga", "Netherlands - Eredivisie", "Spain - La Liga",
        "Brazil - Serie A", "England - Premier League", "Amsterdam Cup",
        "Denmark - Superliga", "Sweden - Allsvenskan", "France - Ligue 1",
        "Italy - Serie A", "Mexico - Liga MX", "USA - Major League Soccer",
    ]

    @pytest.mark.parametrize("name", WOMENS)
    def test_a_womens_competition_is_marked(self, name: str) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import competition_marker

        assert competition_marker(name, LeagueSport.SOCCER) == "w", name

    @pytest.mark.parametrize("name", MENS)
    def test_a_mens_competition_is_never_marked(self, name: str) -> None:
        """The direction that would be expensive: mislabelling a whole men's
        competition splits it from every other venue."""
        from src.leagues import Sport as LeagueSport
        from src.participants import competition_marker

        assert competition_marker(name, LeagueSport.SOCCER) is None, name

    def test_players_are_still_never_marked(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import competition_marker

        assert competition_marker("ITF Women W15 Monastir", LeagueSport.TENNIS) is None


class TestOneMarkerSpeltFourWaysIsOneMarker:
    """The markers were five independent tokens, so the venues that write them
    out never met the venues that abbreviate.

    That also made the synthesised marker useless in practice: it emits a bare
    ``w``, which could only ever join a venue that also writes ``w``.
    """

    @pytest.mark.parametrize("names,expected", [
        (["Aston Villa W", "Aston Villa Women", "Aston Villa Womens",
          "Aston Villa Ladies"], "astonvillaw"),
        (["Sturm Graz Reserves", "Sturm Graz Reserve", "Sturm Graz Res"],
         "sturmgrazreserves"),
        (["Barcelona Jr", "Barcelona Junior"], "barcelonajunior"),
    ])
    def test_one_side_under_several_spellings_is_one_side(self, names, expected) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert {_open_slug(n, sport=LeagueSport.SOCCER) for n in names} == {expected}

    @pytest.mark.parametrize("left,right", [
        ("Aston Villa", "Aston Villa W"),
        ("Aston Villa W", "Aston Villa Youth"),
        ("Sturm Graz II", "Sturm Graz B"),
        ("Sturm Graz II", "Sturm Graz Reserves"),
    ])
    def test_different_sides_are_still_different(self, left, right) -> None:
        """``II``, ``B`` and ``C`` stay apart on purpose: they are ordinals a
        club can field simultaneously, and an unmade join is recoverable where a
        false one is not."""
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug(left, sport=LeagueSport.SOCCER) != _open_slug(
            right, sport=LeagueSport.SOCCER
        )


class TestADoublesEntryDoesNotFailTheWholeTenant:
    """Kambi graded an unresolvable competitor a rejection with no escape, and
    one rejection marks the whole source failed.

    A doubles entry names two players a side, so it is deliberately
    unresolvable — but it is a market this collector does not cover, not a
    participant it failed to recognise.  Both tenants collect ATP, WTA and
    Challenger.  Pinnacle records the same thing happening to it: "failed the
    entire Pinnacle run over one doubles match … discarding 2300 good rows
    across five other sports."  Every other adapter already carried the guard.
    """

    def _parsed_with(self, home: str, away: str):
        import glob

        from src.raw_store import RawStore
        from src.sources.betrivers_kambi import parse_kambi

        store = RawStore("tests/fixtures/raw")
        raws = [
            store.read(path)
            for path in sorted(glob.glob("tests/fixtures/raw/betrivers_kambi__*.json"))
        ]
        # Into the *newest* listView, because ``latest_capture`` correctly
        # discards the older pass this fixture set also contains.
        injected = False
        for index in reversed(range(len(raws))):
            if not raws[index].endpoint.startswith("listview"):
                continue  # betoffer payloads carry "events" too
            payload = raws[index].json()
            if not isinstance(payload, dict) or not payload.get("events"):
                continue
            entry = json.loads(json.dumps(payload["events"][0]))
            event = entry.get("event") if "event" in entry else entry
            event["id"] = 999999999
            event["homeName"], event["awayName"] = home, away
            payload["events"].append(entry)
            raws[index] = dataclasses.replace(raws[index], body=json.dumps(payload))
            injected = True
            break
        assert injected, "no listView response to inject into"
        return parse_kambi(raws)

    def test_a_doubles_fixture_is_a_counted_skip(self) -> None:
        outcome = self._parsed_with("R Galloway / E King", "F Reynolds / J Watt")
        assert outcome.skipped["doubles_or_team_pairing"] == 1, dict(outcome.skipped)
        assert not [r for r in outcome.rejections if r.reason == "unknown_participant"]

    def test_a_genuinely_unknown_club_is_still_a_rejection(self) -> None:
        """The control: the guard must cover doubles and nothing else, or it
        hides the fault it was carved out of."""
        outcome = self._parsed_with("Nonexistent Athletic", "Imaginary Rovers")
        assert [r for r in outcome.rejections if r.reason == "unknown_participant"]


class TestTheEnvelopeDocstringDescribesTheEnvelopeCode:
    """``latest_capture``'s prose said a mixed batch "applies the time window to
    the rest"; the code discards every unstamped response outright.

    The behaviour is the safe one and the prose overstated it, in the one
    direction a reader would rely on.
    """

    def test_a_mixed_batch_keeps_only_the_stamped_pass(self) -> None:
        import dataclasses as dc

        from src.sources._common import latest_capture

        early = _raw("book", "page-01", {})
        middle = dc.replace(_raw("book", "page-02", {}), fetched_at=FETCHED + timedelta(seconds=1))
        stamped = dc.replace(
            _raw("book", "page-03", {}),
            fetched_at=FETCHED + timedelta(seconds=2),
            capture_id="cap-A",
        )
        kept = latest_capture([early, middle, stamped])
        assert [raw.endpoint for raw in kept] == ["page-03"]

    def test_an_all_unstamped_batch_still_uses_the_window(self) -> None:
        import dataclasses as dc

        from src.sources._common import latest_capture

        early = _raw("book", "page-01", {})
        later = dc.replace(_raw("book", "page-02", {}), fetched_at=FETCHED + timedelta(seconds=1))
        assert len(latest_capture([early, later])) == 2


class TestSxBetJudgesAStartAgainstThePriceNotThePassStart:
    """The same defect fixed for Smarkets, one adapter over.

    The fixture-level gate uses the earliest response in the capture — the
    moment the pass began — while the price is read from an ``orders`` response
    fetched much later; a full pass here is 135 requests.  It matters more here
    because this venue keeps a market ACTIVE in play: 19 of the 99 captured
    baseball markets had a ``gameTime`` before their own page's ``fetched_at``
    and every one said ``status: "ACTIVE"``, so the timestamp is the only
    defence and there is no liveness flag to fall back on.
    """

    def _capture(self, *, orders_offset_seconds: int):
        import glob

        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        raws = [
            store.read(p)
            for p in sorted(glob.glob("tests/fixtures/raw/sxbet__*.json"))
        ]
        began = min(raw.fetched_at for raw in raws)
        priced_at = began + timedelta(seconds=orders_offset_seconds)
        kickoff = began + timedelta(seconds=orders_offset_seconds // 2)

        out = []
        for raw in raws:
            if raw.endpoint.startswith("orders"):
                raw = dataclasses.replace(raw, fetched_at=priced_at)
            elif raw.endpoint.startswith("markets"):
                payload = raw.json()
                data = payload.get("data")
                markets = data.get("markets") if isinstance(data, dict) else data
                for market in markets or []:
                    if isinstance(market, dict) and market.get("gameTime"):
                        market["gameTime"] = int(kickoff.timestamp())
                raw = dataclasses.replace(raw, body=json.dumps(payload))
            out.append(raw)
        return out

    def test_a_fixture_that_starts_mid_pass_is_not_priced_pregame(self) -> None:
        from src.sources.sxbet import parse_sxbet

        outcome = parse_sxbet(self._capture(orders_offset_seconds=600))
        assert outcome.quotes == [], [
            (q.event_key, q.observed_at.isoformat(), q.commence_time.isoformat())
            for q in outcome.quotes[:3]
        ]
        assert outcome.skipped["event_already_started"] > 0, dict(outcome.skipped)

    def test_the_untouched_capture_still_produces_rows(self) -> None:
        import glob

        from src.raw_store import RawStore
        from src.sources.sxbet import parse_sxbet

        store = RawStore("tests/fixtures/raw")
        raws = [
            store.read(p)
            for p in sorted(glob.glob("tests/fixtures/raw/sxbet__*.json"))
        ]
        assert parse_sxbet(raws).quotes


class TestTheLinesNoteCoversTheRefusalThatActuallyFires:
    """The annotation explained two gates and stayed silent on the one that had
    fired.

    On the committed slate **all three** sub-1.0 cross-book groups are refused
    as ``stale_leg``, and it returned ``None`` for every one — so the surface
    still contradicted the detector on exactly the rows where they disagreed,
    which is the contradiction the annotation was written to close.
    """

    def _note(self, legs, one_counterparty=None):
        from src.collector import _why_the_detector_would_refuse

        return _why_the_detector_would_refuse(legs, one_counterparty or {})

    def test_legs_too_far_apart_in_time_are_named(self) -> None:
        from src.arb import MAX_OBSERVATION_SPREAD

        legs = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10,
                       observed_at=FETCHED),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=2.30,
                       observed_at=FETCHED + MAX_OBSERVATION_SPREAD + timedelta(seconds=1)),
        ]
        note = self._note(legs)
        assert note is not None and "apart" in note

    def test_legs_inside_the_window_are_not_named(self) -> None:
        legs = [
            make_quote(source="fanduel", selection=Selection.HOME, decimal_odds=2.10,
                       observed_at=FETCHED),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=2.30,
                       observed_at=FETCHED + timedelta(seconds=5)),
        ]
        assert self._note(legs) is None


class TestTheDashboardPageRendersWhatWasRefused:
    """The columns reached the stored payload and stopped there.

    ``report_assets`` contained no reference to either name, so a source that
    answered but lost most of its leagues rendered a green "responded normally"
    card byte-identical to a clean one — and the two tests named for this
    asserted ``build_report``'s dict, not the page, so they passed either way.
    """

    def test_the_page_reads_the_scope_columns(self) -> None:
        from src import report_assets

        source = report_assets.__file__
        text = pathlib.Path(source).read_text()
        assert "scopes_refused" in text, "the page never mentions what was refused"
        assert "scopes_requested" in text
        assert "scopes_failed" in text, "overview still counts messages, not scopes"
        assert "scopes_truncated" in text, "overview never mentions a cut-short scope"

    def test_a_refused_source_does_not_render_as_normal(self) -> None:
        """The pill is the thing an operator actually looks at — on the overview
        cards, not only the drill-down."""
        from src import report_assets

        text = pathlib.Path(report_assets.__file__).read_text()
        assert "function scopesFailedOf(" in text, "legacy refusal helper missing"
        start = text.index("function renderSources()")
        end = text.index("\nfunction ", start + 1)
        block = text[start:end]
        assert "scopesFailedOf" in block, "overview cards ignore the distinct count"
        assert "scopes_truncated" in block, "overview cards ignore cut-short scopes"
        assert "cut short" in block
        assert "responded normally" in block
        assert "refused" in block, "a refused source still renders as normal"

    def test_overview_cards_carry_distinct_refusals_and_truncations(
        self, tmp_path
    ) -> None:
        """``scopes_refused.length`` on the overview printed 'refused 2 of 1'
        for one scope with two messages, and a page-cap-only source stayed
        green as 'responded normally' because ``scopes_truncated`` was unread.
        """
        import shutil
        import subprocess

        from src.report import build_report, render_page
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed")

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="book_a", ok=True, checked_at=FETCHED, quote_count=7,
                event_count=1, request_count=2, raw_bytes=100, latency_ms=1.0,
                scopes_requested=1, scopes_failed=1,
                failed_scopes=("mlb: metadata half", "mlb: order-book half"),
            ))
            store.save_health(run, SourceHealth(
                source_key="book_b", ok=True, checked_at=FETCHED, quote_count=3,
                event_count=1, request_count=1, raw_bytes=50, latency_ms=1.0,
                scopes_requested=1,
                truncated_scopes=("mlb: beyond the page cap",),
            ))
            store.save_quotes(run, [
                make_quote(source="book_a"),
                make_quote(source="book_b", event_key="E2"),
            ])
            store.finish_run(run, finished_at=FETCHED, report=ValidationReport())
            page = tmp_path / "dashboard.html"
            page.write_text(render_page(build_report(store)), encoding="utf-8")

        result = subprocess.run(
            [node, str(pathlib.Path(__file__).parent / "dashboard_smoke.mjs"),
             str(page)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "overview cards use distinct refusals" in result.stdout
        assert "overview cards name truncated scopes" in result.stdout

class TestAWholeNumberStrikeIsADifferentContract:
    """``strike_type: "greater"`` is a strict inequality.

    On a whole strike NO wins when the number is landed on exactly, while
    :func:`src.arb.settlement_outcomes` models a whole spread or total as a push
    for both sides.  The YES pairing is the dangerous direction — a modelled
    floor of zero against a real loss of that leg's stake.  All 272 captured
    strikes are ``x.5``; this is a guard against a shape the venue has not sent
    rather than a fix for one it has, and the comment asserting it could not
    happen is now a check.
    """

    def _parse(self, strike):
        import glob

        from src.raw_store import RawStore
        from src.sources.kalshi import parse_kalshi

        store = RawStore("tests/fixtures/raw")
        raws, touched = [], 0
        for path in sorted(glob.glob("tests/fixtures/raw/kalshi__*.json")):
            raw = store.read(path)
            payload = raw.json()
            if isinstance(payload, dict) and payload.get("markets"):
                for market in payload["markets"]:
                    if market.get("floor_strike") is not None:
                        market["floor_strike"] = strike
                        touched += 1
                raw = dataclasses.replace(raw, body=json.dumps(payload))
            raws.append(raw)
        assert touched, "no strike markets in the capture"
        return parse_kalshi(raws)

    def test_a_half_strike_is_collected(self) -> None:
        outcome = self._parse(3.5)
        assert not outcome.skipped.get("whole_number_strike")

    def test_a_whole_strike_is_refused(self) -> None:
        outcome = self._parse(3.0)
        assert outcome.skipped["whole_number_strike"] > 0, dict(outcome.skipped)


class TestAMarkerIsNotAppliedTwice:
    """A venue that marks the competition usually leaves the teams unmarked —
    that is why ``competition_marker`` exists — but not always, and not
    consistently within one venue.

    Brazil's U20 championship proved it on the first live run after the marker
    was wired into the other adapters: Kambi names the competition *Campeonato
    Brasileiro U20* **and** the teams *Palmeiras-SP U20*.  Appending the marker
    produced ``palmeirasspu20u20`` for the fixture while the betoffer's own
    outcome labels still said ``palmeirasspu20``, so the two no longer matched
    and 36 rows became ``unknown_selection`` rejections — which marks the whole
    source failed.  3,194 quotes with 36 rejections became 3,230 with none.
    """

    @pytest.mark.parametrize("name,marker,expected", [
        ("Palmeiras-SP U20", "u20", "Palmeiras-SP U20"),
        ("RB Bragantino-SP U20", "u20", "RB Bragantino-SP U20"),
        ("Aston Villa Women", "w", "Aston Villa Women"),
        ("Freiburg (W)", "w", "Freiburg (W)"),
        ("Aston Villa Ladies", "w", "Aston Villa Ladies"),
        ("Utrecht", "w", "Utrecht w"),
        ("Palmeiras-SP", "u20", "Palmeiras-SP u20"),
    ])
    def test_a_name_that_already_carries_the_marker_is_left_alone(
        self, name, marker, expected
    ) -> None:
        from src.participants import with_marker

        assert with_marker(name, marker) == expected

    def test_the_two_forms_land_on_one_participant_key(self) -> None:
        """The point of folding the spellings first: a venue writing "Women"
        and one writing "(W)" must not end up with two keys either."""
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug, with_marker

        slugs = {
            _open_slug(with_marker(name, "w"), sport=LeagueSport.SOCCER)
            for name in ("Aston Villa", "Aston Villa Women", "Aston Villa W",
                         "Aston Villa Ladies")
        }
        assert len(slugs) == 1, slugs

    def test_no_marker_means_no_change(self) -> None:
        from src.participants import with_marker

        assert with_marker("Utrecht", None) == "Utrecht"
        assert with_marker(None, "w") is None

    def test_every_adapter_that_marks_goes_through_the_helper(self) -> None:
        """Six adapters apply the marker and each did its own f-string; the
        defect had to be fixed in one place or it would come back in five."""
        import pathlib

        for key in ("pinnacle", "fanduel", "betrivers_kambi", "smarkets",
                    "matchbook", "sxbet"):
            text = pathlib.Path(f"src/sources/{key}.py").read_text()
            assert "with_marker(" in text, key
            assert '{marker}"' not in text, f"{key} still builds the name by hand"


# ── round fifteen ────────────────────────────────────────────────────────────


class TestAThreeWaySwapIsStillAMispairing:
    """The thin-book exemption was written as "fewer sides outlying than seen",
    and that is false on a three-way market.

    A selection mapped to the wrong side is a *permutation*: it moves home and
    away and leaves the draw exactly where it was.  So 2 of 3 sides outlying
    read as a thin book and the swap was exempted outright — on a constructed
    EPL slate with matchbook's home and away exchanged, the run reported **10
    positions at margin 13.57%, guaranteed +14.27 on 100** with no
    price-agreement finding at all.  ``margin_implausibly_large`` does not save
    it: the whole band a 0.15 deviation produces sits under ``REFUSE_MARGIN``.

    The rule is *exactly one* outlying side, which also makes it apply where its
    rationale is strongest and the old form could not reach — a venue quoting
    only one side of a market, which is 122 of the 447 order-driven
    (source, market) pairs on the captured slate.
    """

    ORDER_DRIVEN = frozenset({"matchbook", "smarkets", "kalshi", "polymarket", "sxbet"})

    def _slate(self, *, suspect, swap, draws, thin=False):
        rows = []
        for index in range(10):
            key = f"SOCCER-a{index}@SOCCER-b{index}:2026-07-29"
            common = dict(
                sport=Sport.SOCCER, league="EPL", event_key=key,
                source_event_id=f"e{index}", home_participant=f"SOCCER-b{index}",
                away_participant=f"SOCCER-a{index}", home_team=f"B{index}",
                away_team=f"A{index}", commence_time=LATER, market=Market.MONEYLINE,
            )
            for source in ("bovada", "fanduel", "pinnacle"):
                rows.append(make_quote(source=source, selection=Selection.HOME,
                                       decimal_odds=2.174, **common))
                rows.append(make_quote(source=source, selection=Selection.AWAY,
                                       decimal_odds=3.448, **common))
                if draws:
                    rows.append(make_quote(source=source, selection=Selection.DRAW,
                                           decimal_odds=3.571, **common))
            home, away = (3.448, 2.174) if swap else (2.174, 3.448)
            if thin:
                home = 1.05  # one side nobody wants, the rest honest
            rows.append(make_quote(source=suspect, selection=Selection.HOME,
                                   decimal_odds=home, **common))
            rows.append(make_quote(source=suspect, selection=Selection.AWAY,
                                   decimal_odds=away, **common))
            if draws:
                rows.append(make_quote(source=suspect, selection=Selection.DRAW,
                                       decimal_odds=3.571, **common))
        return rows

    def _errors(self, rows):
        from src.validation import Severity, validate

        report = validate(rows, order_book_sources=self.ORDER_DRIVEN)
        return [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
            and f.severity is Severity.ERROR
        ]

    def test_a_three_way_swap_on_an_exchange_is_caught(self) -> None:
        assert self._errors(self._slate(suspect="matchbook", swap=True, draws=True))

    def test_a_two_way_swap_on_an_exchange_is_still_caught(self) -> None:
        assert self._errors(self._slate(suspect="matchbook", swap=True, draws=False))

    def test_an_honest_three_way_exchange_is_not_reported(self) -> None:
        assert self._errors(self._slate(suspect="matchbook", swap=False, draws=True)) == []

    def test_one_thin_side_of_a_three_way_is_still_exempt(self) -> None:
        """The case the exemption exists for, on a three-way market."""
        assert self._errors(
            self._slate(suspect="matchbook", swap=False, draws=True, thin=True)
        ) == []

    def test_the_swap_reaches_the_detector_when_it_is_not_caught(self) -> None:
        """What the exemption was costing: this is the money the defect made."""
        from src.arb import find_opportunities

        rows = self._slate(suspect="matchbook", swap=True, draws=True)
        report = find_opportunities(rows, as_of=FETCHED, commissions={})
        assert report.opportunities, "the constructed swap must be profitable on paper"
        assert self._errors(rows), "and validation must be what stops it"


class TestNoLegIsStakedOverItsLimitAtAnyBankroll:
    """The placeable rebuild was gated on ``cap < total_stake``.

    Whenever the floored cap landed at or *above* the bankroll the whole guard
    was skipped, and the per-leg rounding still overflows there: a leg sized
    51.80 caps the position at 100.06, floors to exactly 100.00, and the split
    staked it 52.00.  Every limit in [51.77, 52.00) does it — a band, not a
    knife edge — and the test covering the invariant only ever exercised a limit
    deep inside the guarded region.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _report(self, limit):
        from src.arb import find_opportunities

        rows = [
            make_quote(source="pinnacle", selection=Selection.AWAY,
                       decimal_odds=2.20, source_market_id="m"),
            make_quote(source="matchbook", selection=Selection.HOME, decimal_odds=2.05,
                       source_market_id="m").model_copy(update={"limit_amount": limit}),
        ]
        return find_opportunities(rows, as_of=self.AS_OF, commissions={})

    @pytest.mark.parametrize("limit", [round(51.70 + n * 0.01, 2) for n in range(31)])
    def test_across_the_whole_boundary_band(self, limit) -> None:
        for opportunity in self._report(limit).opportunities:
            for leg in opportunity.legs:
                stated = leg.quote.limit_amount
                assert stated is None or leg.stake <= stated + 1e-9, (
                    f"limit {limit}: {leg.source} staked {leg.stake}"
                )

    def test_the_published_cap_is_a_size_that_can_be_placed(self) -> None:
        """``_build_opportunity`` recomputes the cap from the limits every time,
        so the rebuild carried back the very number the step-down had rejected —
        a position staked at 12 printed "limit-capped bankroll: 13.00", and an
        operator sizing up to it places the bet this loop exists to prevent.
        """
        from src.arb import find_opportunities

        rows = [
            make_quote(source="pinnacle", selection=Selection.HOME,
                       decimal_odds=2.20, source_market_id="m"),
            make_quote(source="matchbook", selection=Selection.AWAY, decimal_odds=2.20,
                       source_market_id="m").model_copy(update={"limit_amount": 6.80}),
        ]
        opportunity = find_opportunities(
            rows, as_of=self.AS_OF, commissions={}
        ).opportunities[0]
        assert opportunity.max_total_stake == opportunity.total_stake

    def test_a_loss_is_not_stated_as_a_double_negative(self) -> None:
        report = self._report(0.4)
        for diagnostic in report.diagnostics:
            assert "loses -" not in diagnostic.detail, diagnostic.detail


class TestOneWomensSideIsOneKeyInEveryLanguage:
    """``_SUFFIX_SPELLINGS`` held four English/Spanish spellings while the
    competition pattern had just been taught ten languages.

    ``with_marker`` decides "is this name already marked?" by folding through
    that map, so Kambi's ``Frauen-Bundesliga`` / ``Freiburg Frauen`` was marked
    a second time — the very defect ``with_marker`` was written for, in the
    languages the same round had added.  A split fixture never joins, so there
    is no rejection and no coverage finding: it is silent.
    """

    SPELLINGS = ["Freiburg W", "Freiburg Women", "Freiburg Womens", "Freiburg Ladies",
                 "Freiburg Frauen", "Freiburg (W)", "Freiburg Fem"]

    def test_every_spelling_is_one_key(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        slugs = {_open_slug(n, sport=LeagueSport.SOCCER) for n in self.SPELLINGS}
        assert len(slugs) == 1, slugs

    @pytest.mark.parametrize("name", SPELLINGS)
    def test_a_marked_name_is_not_marked_again(self, name) -> None:
        from src.participants import with_marker

        assert with_marker(name, "w") == name

    def test_the_womens_side_never_becomes_the_mens(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        mens = _open_slug("Freiburg", sport=LeagueSport.SOCCER)
        assert all(
            _open_slug(n, sport=LeagueSport.SOCCER) != mens for n in self.SPELLINGS
        )

    @pytest.mark.parametrize("names", [
        ["Barcelona SC W", "Barcelona SC Women", "Barcelona SC Ladies", "Barcelona SC Fem"],
        ["CD Nacional Res", "CD Nacional Reserves", "CD Nacional Reserve"],
    ])
    def test_a_pinned_club_folds_its_suffixes_too(self, names) -> None:
        """The fold happens *after* the disambiguation early return, so the two
        pinned clubs were the only ones still splitting.  Every unpinned club
        folded correctly, which is why nothing noticed."""
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        slugs = {_open_slug(n, sport=LeagueSport.SOCCER) for n in names}
        assert len(slugs) == 1, slugs

    def test_the_pin_still_separates_the_clubs_it_pins(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug("Barcelona SC W", sport=LeagueSport.SOCCER) != _open_slug(
            "Barcelona W", sport=LeagueSport.SOCCER
        )


class TestTheReadmeCountsTheVenuesThatStateASize:
    """The README said two venues publish a size and that no sportsbook does.

    Four do, on every row — and one of them, Pinnacle, is a sportsbook.  Both
    claims predate the work that made ``limit_amount`` load-bearing.
    """

    def test_exactly_the_documented_venues_state_a_size(self) -> None:
        import glob

        from src.collector import SOURCE_FACTORIES
        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        stating = set()
        for key, factory in SOURCE_FACTORIES.items():
            paths = sorted(glob.glob(f"tests/fixtures/raw/{key}__*.json"))
            if not paths:
                continue
            source = factory()
            try:
                quotes = source.parse([store.read(p) for p in paths]).quotes
            finally:
                source.close()
            if quotes and all(q.limit_amount is not None for q in quotes):
                stating.add(key)
        assert stating == {"kalshi", "matchbook", "sxbet", "pinnacle"}, stating

    def test_the_readme_says_four_and_names_the_sportsbook(self) -> None:
        import pathlib

        text = pathlib.Path("README.md").read_text()
        assert "Four of them publish" in text
        assert "Pinnacle" in text


class TestTwoSourcesHaveNoConsensusAboutAStartTime:
    """``start_time_disagreement`` measured each source against the *median*
    start, and a median of two is the midpoint.

    Each source therefore sat at half the gap and the tolerance was silently
    doubled — the check could not see any disagreement under 2x its own
    threshold.  40% of shared events on the captured slate have exactly two
    sources, and the one event whose span exceeds its threshold — a 6h30m tennis
    gap between FanDuel and Matchbook — is also the top-ranked position the
    detector reports.  The check that exists to say "one of these books may be
    describing a different fixture" was silent on the fixture the money was on.

    ``_check_price_agreement`` documents this exact reasoning and guards on
    three sources for it; this check guarded on two.
    """

    def _findings(self, offsets):
        from src.validation import validate

        rows = []
        for source, offset in offsets.items():
            for selection in (Selection.HOME, Selection.AWAY):
                rows.append(make_quote(
                    source=source,
                    sport=Sport.TENNIS,
                    league="ATP",
                    event_key="TENNIS-a@TENNIS-b:2026-07-29",
                    home_participant="TENNIS-b", away_participant="TENNIS-a",
                    home_team="B", away_team="A", source_event_id="e1",
                    selection=selection,
                    commence_time=LATER + timedelta(hours=offset),
                ))
        return [
            f for f in validate(rows).findings
            if f.code == "start_time_disagreement"
        ]

    def test_a_gap_between_two_sources_is_measured_whole(self) -> None:
        """Tennis tolerates 6 hours.  6h30m apart, each sits 3h15m from the
        midpoint — comfortably inside, which is why this was invisible."""
        found = self._findings({"fanduel": 0, "matchbook": 6.5})
        assert {f.source for f in found} == {"fanduel", "matchbook"}, found

    def test_neither_of_two_is_named_the_outlier(self) -> None:
        """With two there is no way to say which is wrong, and saying so is the
        finding — the message must not claim a consensus that does not exist."""
        found = self._findings({"fanduel": 0, "matchbook": 6.5})
        assert len(found) == 2
        assert all("median" not in f.message for f in found), [f.message for f in found]

    def test_a_gap_inside_the_tolerance_is_still_quiet(self) -> None:
        assert self._findings({"fanduel": 0, "matchbook": 5.0}) == []

    def test_three_sources_still_name_the_one_that_is_wrong(self) -> None:
        """The median is right once there is a consensus to deviate from: one
        book out of step must not indict the two that agree."""
        found = self._findings({"fanduel": 0, "matchbook": 0, "bovada": 8})
        assert {f.source for f in found} == {"bovada"}, found


class TestOneBooksFaultDoesNotDiscardEveryOtherBook:
    """Three refusals abandoned the whole market over one book's problem.

    The codebase had already fixed this once, for ``source_prices_itself_to_lose``,
    with the reasoning written down: "with ten it costs nine books' worth of
    comparison every time one adapter has a bad day, which is the opposite of
    what adding sources is for."  The same shape survived in three neighbours.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    T = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

    def _report(self, rows):
        from src.arb import find_opportunities

        return find_opportunities(rows, as_of=self.AS_OF, commissions={})

    def _pair(self):
        """A genuine 4.76% position between two innocent books."""
        return [
            make_quote(source="bookA", selection=Selection.HOME,
                       decimal_odds=2.10, observed_at=self.T, source_market_id="m"),
            make_quote(source="bookB", selection=Selection.AWAY,
                       decimal_odds=2.10, observed_at=self.T, source_market_id="m"),
        ]

    def test_a_duplicating_book_is_dropped_not_the_market(self) -> None:
        rows = self._pair() + [
            make_quote(source="bookC", selection=Selection.HOME,
                       decimal_odds=1.90, observed_at=self.T, source_market_id="m"),
            make_quote(source="bookC", selection=Selection.HOME,
                       decimal_odds=1.95, observed_at=self.T, source_market_id="m2"),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1
        assert {leg.source for leg in report.opportunities[0].legs} == {"bookA", "bookB"}
        named = [d for d in report.diagnostics if d.code == "duplicate_selection"]
        assert named and "bookC" in named[0].detail, "the message must name the book"

    def test_an_inverted_book_does_not_take_the_market_with_it(self) -> None:
        """An inverted mapping does not trip ``source_prices_itself_to_lose``:
        home 1.10 against away 9.00 is a perfectly healthy overround."""
        rows = self._pair() + [
            make_quote(source="bookC", selection=Selection.HOME,
                       decimal_odds=1.10, observed_at=self.T, source_market_id="m"),
            make_quote(source="bookC", selection=Selection.AWAY,
                       decimal_odds=9.00, observed_at=self.T, source_market_id="m"),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1
        assert {leg.source for leg in report.opportunities[0].legs} == {"bookA", "bookB"}

    def test_an_implausible_edge_with_nothing_underneath_is_still_refused(self) -> None:
        report = self._report([
            make_quote(source="bookA", selection=Selection.HOME,
                       decimal_odds=2.10, observed_at=self.T, source_market_id="m"),
            make_quote(source="bookB", selection=Selection.AWAY,
                       decimal_odds=9.00, observed_at=self.T, source_market_id="m"),
        ])
        assert report.opportunities == []
        assert "margin_implausibly_large" in [d.code for d in report.diagnostics]

    @pytest.mark.parametrize("minutes", [20, -20])
    def test_a_stale_but_better_price_does_not_destroy_a_simultaneous_one(
        self, minutes
    ) -> None:
        """Parametrised over the sign because which leg is "the stale one" is
        not knowable up front: the straggler may be the newest price or the
        oldest, depending which way the pass ran."""
        rows = self._pair() + [
            make_quote(source="bookC", selection=Selection.HOME, decimal_odds=2.60,
                       observed_at=self.T + timedelta(minutes=minutes),
                       source_market_id="m"),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        assert {leg.source for leg in report.opportunities[0].legs} == {"bookA", "bookB"}

    def test_a_genuinely_stale_pair_is_still_refused(self) -> None:
        rows = [
            make_quote(source="bookA", selection=Selection.HOME, decimal_odds=2.10,
                       observed_at=self.T, source_market_id="m"),
            make_quote(source="bookB", selection=Selection.AWAY, decimal_odds=2.10,
                       observed_at=self.T + timedelta(minutes=20), source_market_id="m"),
        ]
        report = self._report(rows)
        assert report.opportunities == []
        assert "stale_leg" in [d.code for d in report.diagnostics]


class TestThePublishedCapKeepsTheVenuesAnswer:
    """Clamping the cap to the reported bankroll threw information away.

    Where the stated limits permit more than the notional bankroll, the true cap
    is the useful number — an operator sizing up wants to know the position
    holds to 1,000 — and ``min(cap, total_stake)`` printed "limit-capped
    bankroll: 100.00", which reads as a constraint where there is none.  It
    appeared in the live arb output on the first run after the clamp went in.

    So the *ceiling* is clamped, because that is as far as the per-leg split has
    been verified, and the *published* cap keeps the venues' own answer whenever
    the step-down did not reduce anything.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _opportunity(self, limit, home=2.20, away=2.20):
        from src.arb import find_opportunities

        rows = [
            make_quote(source="pinnacle", selection=Selection.HOME,
                       decimal_odds=home, source_market_id="m"),
            make_quote(source="matchbook", selection=Selection.AWAY, decimal_odds=away,
                       source_market_id="m").model_copy(update={"limit_amount": limit}),
        ]
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={})
        return report.opportunities[0] if report.opportunities else None

    def test_a_generous_limit_reports_the_venues_cap_not_the_bankroll(self) -> None:
        opportunity = self._opportunity(500.0)
        assert opportunity.total_stake == 100
        assert opportunity.max_total_stake > 100, opportunity.max_total_stake

    def test_a_binding_limit_reports_the_size_that_was_found(self) -> None:
        opportunity = self._opportunity(6.80)
        assert opportunity.max_total_stake == opportunity.total_stake < 100

    @pytest.mark.parametrize("limit", [6.8, 51.8, 200.0, 500.0])
    def test_no_leg_is_over_its_limit_at_any_of_them(self, limit) -> None:
        opportunity = self._opportunity(limit)
        assert opportunity is not None
        for leg in opportunity.legs:
            stated = leg.quote.limit_amount
            assert stated is None or leg.stake <= stated + 1e-9


# ── round sixteen ────────────────────────────────────────────────────────────


class TestARefusedBookStaysRefused:
    """A retry narrowed ``chosen`` and left ``eligible`` untouched.

    The two retries that follow re-enumerate ``eligible``, so a settlement or
    freshness narrowing rebuilt the very assignment the margin check had just
    refused — and the settlement path re-applied ``min_margin`` and freshness
    but **not** ``REFUSE_MARGIN``.  Measured: FanDuel with an inverted mapping
    (home 1.10 / away 9.00) against Pinnacle and Kalshi published ``margin
    41.27%, guaranteed +70.10 on 100`` with **zero diagnostics**, while the same
    two rows without Kalshi were correctly refused.  Adding a venue that never
    appears in the reported position reversed the refusal.

    An inverted mapping cannot be caught by ``source_prices_itself_to_lose``:
    1/1.10 + 1/9.00 is 1.020, a perfectly healthy overround.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    T = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, minutes=0):
        return make_quote(
            source=source, selection=selection, decimal_odds=odds,
            observed_at=self.T + timedelta(minutes=minutes), source_market_id="m",
        )

    def _report(self, rows):
        from src.arb import find_opportunities

        return find_opportunities(rows, as_of=self.AS_OF)

    def test_a_settlement_narrowing_cannot_re_admit_it(self) -> None:
        rows = [
            self._q("fanduel", Selection.HOME, 1.10),
            self._q("fanduel", Selection.AWAY, 9.00),
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("pinnacle", Selection.AWAY, 1.80),
            self._q("kalshi", Selection.HOME, 1.80),
            self._q("kalshi", Selection.AWAY, 2.10),
        ]
        report = self._report(rows)
        assert report.opportunities == [], [
            f"{o.margin:.2%} " + "/".join(l.source for l in o.legs)
            for o in report.opportunities
        ]
        assert report.diagnostics, "and it must say why"

    def test_a_freshness_narrowing_cannot_re_admit_it(self) -> None:
        rows = [
            self._q("fanduel", Selection.HOME, 1.10),
            self._q("fanduel", Selection.AWAY, 9.00),
            self._q("pinnacle", Selection.HOME, 2.10, minutes=10),
            self._q("pinnacle", Selection.AWAY, 1.30, minutes=10),
            self._q("bovada", Selection.HOME, 1.30),
            self._q("bovada", Selection.AWAY, 2.05),
        ]
        report = self._report(rows)
        assert report.opportunities == []
        assert report.diagnostics

    def test_the_control_still_refuses_without_the_third_venue(self) -> None:
        report = self._report([
            self._q("fanduel", Selection.HOME, 1.10),
            self._q("fanduel", Selection.AWAY, 9.00),
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("pinnacle", Selection.AWAY, 1.80),
        ])
        assert report.opportunities == []
        assert "margin_implausibly_large" in [d.code for d in report.diagnostics]

    def test_a_real_position_beside_an_inverted_book_still_survives(self) -> None:
        """The retry must still do its job: dropping the inverted book has to
        leave the genuine position standing."""
        report = self._report([
            self._q("fanduel", Selection.HOME, 1.10),
            self._q("fanduel", Selection.AWAY, 9.00),
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.10),
        ])
        assert len(report.opportunities) == 1
        assert {l.source for l in report.opportunities[0].legs} == {"pinnacle", "bovada"}


class TestASpreadsTwoHalvesAreOneMarket:
    """``_check_price_agreement`` grouped on the **raw** line.

    A spread states its halves at opposite numbers — home -1.5, away +1.5 — so
    they never shared a market key, every (order-driven source, spread) pair had
    exactly one side by construction, and the round-15 "exactly one outlying
    side" exemption applied unconditionally.  A swapped spread at Matchbook or
    SX Bet was silent while the detector reported 10 positions at 7.5%.

    It also made the rule's own justification an artifact: of the 124
    "one-sided" order-driven pairs on the captured slate, 120 were spreads and
    only 3 were moneylines.
    """

    ORDER_DRIVEN = frozenset({"matchbook", "smarkets", "kalshi", "polymarket", "sxbet"})

    def _slate(self, *, suspect, swap, market, line=1.5):
        rows = []
        first = Selection.HOME if market is Market.SPREAD else Selection.OVER
        second = Selection.AWAY if market is Market.SPREAD else Selection.UNDER
        for index in range(10):
            key = f"MLB-a{index}@MLB-b{index}:2026-07-29"
            common = dict(
                sport=Sport.BASEBALL, league="MLB", event_key=key,
                source_event_id=f"e{index}", home_participant=f"MLB-b{index}",
                away_participant=f"MLB-a{index}", home_team=f"B{index}",
                away_team=f"A{index}", commence_time=LATER, market=market,
            )
            lines = (-line, line) if market is Market.SPREAD else (line, line)
            for source in ("bovada", "fanduel", "pinnacle"):
                rows.append(make_quote(source=source, selection=first,
                                       decimal_odds=2.174, line=lines[0], **common))
                rows.append(make_quote(source=source, selection=second,
                                       decimal_odds=1.60, line=lines[1], **common))
            near, far = (1.60, 2.174) if swap else (2.174, 1.60)
            rows.append(make_quote(source=suspect, selection=first,
                                   decimal_odds=near, line=lines[0], **common))
            rows.append(make_quote(source=suspect, selection=second,
                                   decimal_odds=far, line=lines[1], **common))
        return rows

    def _errors(self, rows):
        from src.validation import Severity, validate

        report = validate(rows, order_book_sources=self.ORDER_DRIVEN)
        return [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
            and f.severity is Severity.ERROR
        ]

    @pytest.mark.parametrize("suspect", ["matchbook", "sxbet", "kalshi", "polymarket"])
    def test_a_swapped_spread_on_an_exchange_is_caught(self, suspect) -> None:
        assert self._errors(
            self._slate(suspect=suspect, swap=True, market=Market.SPREAD)
        ), suspect

    def test_an_honest_spread_on_an_exchange_is_not_reported(self) -> None:
        assert self._errors(
            self._slate(suspect="matchbook", swap=False, market=Market.SPREAD)
        ) == []

    def test_a_swapped_total_is_still_caught(self) -> None:
        """Totals state both sides at one number, so this changes nothing for
        them — asserted so the regrouping cannot quietly break them."""
        assert self._errors(
            self._slate(suspect="matchbook", swap=True, market=Market.TOTAL)
        )


class TestThePublishedCapIsOneThatWasVerified:
    """Republishing the venues' floored cap unchecked told an operator a
    position held to a size at which it does not.

    The step-down only ever verified up to ``min(stated_cap, total_stake)``, so
    the branch that reduced nothing published a number nothing had tested:
    ``pinnacle 1.50`` vs ``bovada 3.05`` limited to 33.30 reported
    ``limit-capped bankroll: 101.00``, and at 101 the position both overstakes
    the binding leg and loses 0.35.  A sweep found 628 such pairs in the
    ordinary band.

    The loop also used to stop at the first size whose legs fit and reject if
    that size was not risk-free — throwing away positions that pay at a smaller
    one.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _rows(self, limit, home=1.50, away=3.05):
        return [
            make_quote(source="pinnacle", selection=Selection.HOME,
                       decimal_odds=home, source_market_id="m"),
            make_quote(source="bovada", selection=Selection.AWAY, decimal_odds=away,
                       source_market_id="m").model_copy(update={"limit_amount": limit}),
        ]

    def _report(self, limit, **kwargs):
        from src.arb import find_opportunities

        return find_opportunities(
            self._rows(limit), as_of=self.AS_OF, commissions={}, **kwargs
        )

    @pytest.mark.parametrize("limit", [6.8, 31.5, 33.3, 51.8, 120.0, 500.0])
    def test_the_position_holds_at_the_cap_it_publishes(self, limit) -> None:
        reported = self._report(limit).opportunities
        if not reported:
            return  # refused outright, which is its own answer
        cap = reported[0].max_total_stake
        assert cap is not None
        at_cap = self._report(limit, total_stake=cap).opportunities
        assert at_cap, f"the published cap {cap} is not placeable at all"
        for leg in at_cap[0].legs:
            stated = leg.quote.limit_amount
            assert stated is None or leg.stake <= stated + 1e-9, (
                f"cap {cap}: {leg.source} staked {leg.stake} over {stated}"
            )
        assert at_cap[0].guaranteed_profit >= -1e-9

    def test_a_size_that_pays_is_not_thrown_away(self) -> None:
        """Refused outright at a limit of 31.50 while a smaller whole-unit size
        fits both limits and guarantees a profit."""
        reported = self._report(31.5).opportunities
        assert reported, "a payable size exists and must be found"
        assert reported[0].guaranteed_profit > 0


class TestNarrowingCanOnlyLowerTheMargin:
    """Why the settlement retry does not re-check ``REFUSE_MARGIN``.

    A gate was briefly added there while fixing the exclusion leak, and it was
    unreachable: ``_best_assignment`` maximises the margin over the sources it
    is given, and every assignment available on a narrowed set is also available
    on the full one — so narrowing can only lower the margin, and the full set
    has already cleared ``REFUSE_MARGIN`` to reach that line.

    Pinned as a property rather than argued in a comment, because the comment is
    what the dead gate was standing in for.  ``min_margin`` is a different
    matter — it is a floor, and lowering the margin can cross it, so that one is
    re-checked.
    """

    def test_no_subset_ever_beats_the_full_set(self) -> None:
        import random

        from src.arb import _best_assignment

        random.seed(11)
        needed = {Selection.HOME, Selection.AWAY}
        checked = 0
        for _ in range(400):
            sources = [f"s{index}" for index in range(random.randint(3, 5))]
            best = {
                source: {
                    selection: make_quote(
                        source=source, selection=selection,
                        decimal_odds=round(random.uniform(1.2, 6.0), 3),
                    )
                    for selection in needed
                }
                for source in sources
            }
            full = _best_assignment(
                needed=needed, eligible=sources, best=best,
                require_distinct_sources=True, commissions={}, counterparties={},
            )
            if full is None:
                continue
            for dropped in sources:
                narrowed = [s for s in sources if s != dropped]
                subset = _best_assignment(
                    needed=needed, eligible=narrowed, best=best,
                    require_distinct_sources=True, commissions={}, counterparties={},
                )
                if subset is None:
                    continue
                checked += 1
                assert subset[1] >= full[1] - 1e-12, (
                    f"dropping {dropped} improved the margin, so narrowing is not "
                    "monotone and the settlement path does need its own gate"
                )
        assert checked > 500, checked


class TestABooksOwnShortFormStillJoins:
    """FanDuel writes the city where every other book writes the club.

    The alias table encoded FanDuel's EPL short forms from an earlier slate and
    was never extended to MLS or to the promoted clubs, so nine soccer fixtures
    sat under two keys — six of them as two single-book events rather than one
    cross-book market.  Every cross-book check is conditioned on rows that
    already share a key, so nothing reported it.

    Curated one at a time and deliberately **not** inferred from a prefix: on
    the same capture "Inter" is Internazionale and not Inter Miami, "Paris FC"
    is not Paris Saint-Germain, "Racing Club" is not Racing Santander,
    "Deportivo" is not Deportivo Pasto, and "Tigre" is not Tigres FC
    Zipaquira.  A short-into-long rule would fuse every one of those.
    """

    @pytest.mark.parametrize("short,long", [
        ("Colorado", "Colorado Rapids"), ("Columbus", "Columbus Crew"),
        ("Coventry", "Coventry City"), ("Ipswich", "Ipswich Town"),
        ("Philadelphia", "Philadelphia Union"), ("Hull", "Hull City"),
        ("Atlanta Utd", "Atlanta United"), ("DC Utd", "D.C. United"),
        ("Minnesota Utd", "Minnesota United"), ("Kansas City", "Sporting Kansas City"),
    ])
    def test_the_two_spellings_are_one_club(self, short, long) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug(short, sport=LeagueSport.SOCCER) == _open_slug(
            long, sport=LeagueSport.SOCCER
        )

    @pytest.mark.parametrize("one,other", [
        ("Inter", "Inter Miami"), ("Paris FC", "Paris Saint-Germain"),
        ("Racing Club", "Racing Santander"), ("Deportivo", "Deportivo Pasto"),
        ("Tigre", "Tigres FC Zipaquira"), ("Athletic Club", "Athletico Paranaense-PR"),
    ])
    def test_two_different_clubs_are_never_fused(self, one, other) -> None:
        """The direction that costs money: a false merge puts two clubs' prices
        on one fixture."""
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug(one, sport=LeagueSport.SOCCER) != _open_slug(
            other, sport=LeagueSport.SOCCER
        )


class TestASplitGivenNameStillJoins:
    """Tennis has no alias table — the names are unbounded — so the slug is the
    only mechanism that can reconcile spellings.

    Bovada writes "Soon Woo Kwon" where FanDuel and Matchbook write "Soonwoo
    Kwon", and a dot-joined slug meant ``kwon.soon.woo`` never met
    ``kwon.soonwoo``.  Measured before changing it: across the 320 tennis
    participants in the captured corpus, dropping the separator merges exactly
    one pair, and it is that one.
    """

    def test_a_given_name_split_in_two_lands_on_one_key(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug("Soon Woo Kwon", sport=LeagueSport.TENNIS) == _open_slug(
            "Soonwoo Kwon", sport=LeagueSport.TENNIS
        )

    def test_name_order_still_does_not_matter(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug("Xiyu Wang", sport=LeagueSport.TENNIS) == _open_slug(
            "Wang Xiyu", sport=LeagueSport.TENNIS
        )

    def test_two_different_players_are_not_merged_on_the_captured_corpus(self) -> None:
        """The risk the separator was carrying: joining tokens can in principle
        fuse two people.  Asserted against every player the fixtures hold."""
        import glob

        from src.collector import SOURCE_FACTORIES
        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        names = {}
        for key, factory in SOURCE_FACTORIES.items():
            paths = sorted(glob.glob(f"tests/fixtures/raw/{key}__*.json"))
            if not paths:
                continue
            source = factory()
            try:
                quotes = source.parse([store.read(p) for p in paths]).quotes
            finally:
                source.close()
            for quote in quotes:
                if quote.sport is not Sport.TENNIS:
                    continue
                names.setdefault(quote.home_participant, set()).add(quote.home_team)
                names.setdefault(quote.away_participant, set()).add(quote.away_team)
        assert names, "no tennis rows to check"
        # Every key holding more than one spelling must hold spellings of one
        # person — checked by their surnames agreeing once folded.
        from src.participants import _fold

        for key, spellings in names.items():
            folded = {
                frozenset(_fold(name).lower().replace("-", " ").split())
                for name in spellings
            }
            if len(folded) > 1:
                shared = set.intersection(*(set(f) for f in folded))
                assert shared, f"{key} holds unrelated names: {sorted(spellings)}"


class TestAWomensCompetitionIsMarkedInItsOwnSpelling:
    """The marker patterns are ASCII and the venues are not.

    "France - D1 Féminine" and "Première Ligue Féminine" matched nothing and
    produced a **men's** event key — the fault this mechanism exists to prevent
    — while the unaccented spelling of the same competition matched, so whether
    it worked depended on which venue wrote it.  The test that covered this
    cited the accented name in its docstring and parametrised the unaccented
    one.
    """

    WOMENS = [
        "France - D1 Féminine", "France - D1 Feminine", "Première Ligue Féminine",
        "D1 Féminin", "Italy - Serie A Femminile", "Brazil - Campeonato Feminino",
        "Mexico - Liga MX Femenil", "Spain - Liga F Femenino", "Spain - Liga F Femenina",
        "Germany - Frauen-Bundesliga", "Netherlands - Eredivisie Vrouwen",
        "England - WSL", "USA - NWSL", "Denmark - Kvindeliga",
        "Sweden - Damallsvenskan", "Norwegian Toppserien Women",
        "English Women's Championship", "Norwegian Toppserien Ladies",
        "Australian A-League - Women", "Club Friendlies Women",
    ]

    MENS = [
        "France - Ligue 1", "Italy - Serie A", "Brazil - Serie A", "Spain - La Liga",
        "Germany - Bundesliga", "Netherlands - Eredivisie", "Première Ligue",
        "England - Premier League", "Mexico - Liga MX", "Amsterdam Cup",
        "Denmark - Superliga", "Sweden - Allsvenskan", "USA - Major League Soccer",
        "Copa Libertadores", "Copa Sudamericana", "England - Championship",
    ]

    @pytest.mark.parametrize("name", WOMENS)
    def test_a_womens_competition_is_marked(self, name: str) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import competition_marker

        assert competition_marker(name, LeagueSport.SOCCER) == "w", name

    @pytest.mark.parametrize("name", MENS)
    def test_a_mens_competition_is_never_marked(self, name: str) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import competition_marker

        assert competition_marker(name, LeagueSport.SOCCER) is None, name


class TestARepairedRowIsNotAaDroppedOne:
    """118 Kambi rows were counted as skipped and published anyway.

    ``skipped`` is documented as "counts of out-of-scope *input*", the dashboard
    renders the total as "seen but out of scope", and the note read "so the row
    is not kept" — the exact opposite of what happens.  The row is emitted
    carrying a derived American price because the feed's own two formats
    disagreed.
    """

    def _parsed(self, key):
        import glob

        from src.collector import SOURCE_FACTORIES
        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        paths = sorted(glob.glob(f"tests/fixtures/raw/{key}__*.json"))
        source = SOURCE_FACTORIES[key]()
        try:
            return source.parse([store.read(p) for p in paths])
        finally:
            source.close()

    def test_the_repair_is_counted_where_it_belongs(self) -> None:
        outcome = self._parsed("betrivers_kambi")
        assert outcome.repaired["feed_american_odds_disagreed_with_decimal"] == 54
        assert not any("american" in reason for reason in outcome.skipped)

    def test_the_rows_it_describes_are_in_the_output(self) -> None:
        outcome = self._parsed("betrivers_kambi")
        assert len(outcome.quotes) == 1107

    def test_no_source_counts_a_published_row_as_skipped(self) -> None:
        from src.collector import SOURCE_FACTORIES

        for key in SOURCE_FACTORIES:
            import glob

            if not glob.glob(f"tests/fixtures/raw/{key}__*.json"):
                continue
            outcome = self._parsed(key)
            assert not (set(outcome.repaired) & set(outcome.skipped)), key


class TestEveryDroppedRecordIsCountedExactlyOnce:
    """Kalshi judged a fixture once per market and counted the drop twice.

    19 markets on one already-started game produced ``game_already_started: 19``
    **and** ``market_on_out_of_scope_game: 19`` — 38 counted drops for 19
    records — and on the rejecting branches one bad ticker would have become 19
    separate rejections, so the health line read "19 record(s) rejected" for one
    bad fixture.
    """

    def test_kalshi_counts_one_drop_per_dropped_market(self) -> None:
        import glob

        from src.raw_store import RawStore
        from src.sources.kalshi import parse_kalshi

        store = RawStore("tests/fixtures/raw")
        outcome = parse_kalshi([
            store.read(p) for p in sorted(glob.glob("tests/fixtures/raw/kalshi__*.json"))
        ])
        assert outcome.skipped["game_already_started"] == 19, dict(outcome.skipped)
        assert "market_on_out_of_scope_game" not in outcome.skipped
        assert outcome.rejections == []


class TestTheTennisSlugDoesNotFuseTwoPeople:
    """The separator-free slug is the only mechanism reconciling tennis
    spellings, and its risk is a false merge — two different players on one key.

    Audited across every tennis display name the captured corpus holds: no name
    carries two keys, and no key holds two names sharing no token.  The residual
    shape is narrow because the sort happens before the join, so a split given
    name only meets its joined form when the halves sort adjacent.
    """

    def _corpus(self):
        import glob

        from src.collector import SOURCE_FACTORIES
        from src.raw_store import RawStore

        store = RawStore("tests/fixtures/raw")
        names: dict[str, set[str]] = {}
        for key, factory in SOURCE_FACTORIES.items():
            paths = sorted(glob.glob(f"tests/fixtures/raw/{key}__*.json"))
            if not paths:
                continue
            source = factory()
            try:
                quotes = source.parse([store.read(p) for p in paths]).quotes
            finally:
                source.close()
            for quote in quotes:
                if quote.sport is not Sport.TENNIS:
                    continue
                names.setdefault(quote.home_team, set()).add(quote.home_participant)
                names.setdefault(quote.away_team, set()).add(quote.away_participant)
        assert len(names) > 300, len(names)
        return names

    def test_no_display_name_carries_two_keys(self) -> None:
        split = {n: k for n, k in self._corpus().items() if len(k) > 1}
        assert split == {}, split

    def test_no_key_holds_two_unrelated_names(self) -> None:
        import collections

        from src.participants import _fold

        by_key = collections.defaultdict(set)
        for name, keys in self._corpus().items():
            for key in keys:
                by_key[key].add(name)
        fused = []
        for key, names in by_key.items():
            tokens = [
                set(_fold(name).lower().replace("-", " ").split()) for name in names
            ]
            if len(tokens) > 1 and not set.intersection(*tokens):
                fused.append((key, sorted(names)))
        assert fused == [], fused

    def test_a_split_that_does_not_sort_adjacent_stays_apart(self) -> None:
        """Evidence the collision surface is narrow rather than an accident:
        an interior split at a non-adjacent point does not merge."""
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug

        assert _open_slug("Ana Li Chen", sport=LeagueSport.TENNIS) != _open_slug(
            "Anali Chen", sport=LeagueSport.TENNIS
        )


# ── round seventeen ──────────────────────────────────────────────────────────


class TestAScopeFilterCannotReopenTheCounterpartyGate:
    """The gate was measured from the rows the caller handed in, and every
    scoped command hands in fewer.

    Two Kambi tenants that are one counterparty across 30 ATP moneylines share
    18 WTA selections — under ``MIN_SHARED_SELECTIONS`` — so ``arb --league
    WTA`` measured UNDECIDED, opened the gate, and reported a position with both
    legs at one book and no diagnostic.  Round 16 fixed where a mirror is
    *filed*; this is where it is *measured*, and narrowing can only ever weaken
    it.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _slate(self):
        rows = []
        for index in range(30):  # ATP: byte-identical, establishes the mirror
            key = f"TENNIS-atp{index}@TENNIS-atph{index}:2026-07-29"
            common = dict(
                sport=Sport.TENNIS, league="ATP", event_key=key,
                source_event_id=f"atp{index}", home_participant=f"TENNIS-atph{index}",
                away_participant=f"TENNIS-atp{index}", home_team=f"H{index}",
                away_team=f"A{index}", commence_time=LATER, selection=Selection.HOME,
                decimal_odds=1.80,
            )
            for source in ("betrivers_kambi", "leovegas_kambi"):
                rows.append(make_quote(source=source, **common))
        for index in range(9):  # WTA: 18 selections, too few to judge alone
            key = f"TENNIS-wta{index}@TENNIS-wtah{index}:2026-07-29"
            common = dict(
                sport=Sport.TENNIS, league="WTA", event_key=key,
                source_event_id=f"wta{index}", home_participant=f"TENNIS-wtah{index}",
                away_participant=f"TENNIS-wta{index}", home_team=f"WH{index}",
                away_team=f"WA{index}", commence_time=LATER,
            )
            rows.append(make_quote(source="betrivers_kambi", selection=Selection.HOME,
                                   decimal_odds=2.20, **common))
            rows.append(make_quote(source="leovegas_kambi", selection=Selection.AWAY,
                                   decimal_odds=2.20, **common))
        return rows

    def test_the_whole_run_measures_the_gate_and_the_scope_only_narrows(self) -> None:
        from src.arb import counterparty_groups, find_opportunities

        rows = self._slate()
        measured = counterparty_groups(rows)          # the whole run
        wta = [q for q in rows if q.league == "WTA"]  # what a scoped command sees
        report = find_opportunities(wta, as_of=self.AS_OF, one_counterparty=measured)
        assert report.opportunities == [], [
            "/".join(l.source for l in o.legs) for o in report.opportunities
        ]

    def test_the_collector_measures_before_it_narrows(self, tmp_path) -> None:
        """Through the real ``collect_once``, because the mechanism being right
        proves nothing if the caller does not use it — the defect *was* the
        wiring."""
        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.sources.base import ParseOutcome
        from src.store import Store

        rows = self._slate()

        def rigged(key):
            class Rigged:
                source_key = key
                leagues = ("ATP", "WTA")

                def fetch_raw(self):
                    return [_raw(key, "listview-01", {"events": []})]

                def parse(self, raws):
                    return ParseOutcome(
                        quotes=[q for q in rows if q.source == key]
                    )

                def close(self) -> None:
                    pass

            return Rigged()

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged("betrivers_kambi"), rigged("leovegas_kambi")],
                raw_store=raw_store, store=store, as_of=self.AS_OF,
                leagues=["WTA"],
            )
        assert result.arb is not None
        assert result.arb.opportunities == [], [
            "/".join(l.source for l in o.legs) for o in result.arb.opportunities
        ]

    def test_measuring_from_the_narrowed_rows_is_what_opened_it(self) -> None:
        """Pins the defect itself, so the fix cannot be undone by passing the
        scoped rows again."""
        from src.arb import counterparty_groups, find_opportunities

        rows = self._slate()
        wta = [q for q in rows if q.league == "WTA"]
        as_if_narrowed = counterparty_groups(wta)
        assert as_if_narrowed == {}, "18 shared selections cannot establish a mirror"
        leaked = find_opportunities(wta, as_of=self.AS_OF, one_counterparty=as_if_narrowed)
        assert leaked.opportunities, "this is the shape the fix must prevent"


class TestARefusalNarrowsToTheBestRemainingPosition:
    """Three retries each searched one narrowing and stopped.

    The margin retry excluded only the book holding the longest price — throwing
    away the leg a real position needed — and then dropped the market with no
    diagnostic at all, so a 36.9% apparent edge judged a mapping fault was
    summarised as "1 cross-book market, 0 rejected".  The freshness retry
    excluded one source at a time, which cannot recover a slate with two venues
    in each of two time clusters — the shape a sequential ten-source pass
    produces by construction.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    T = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, minutes=0):
        return make_quote(
            source=source, selection=selection, decimal_odds=odds,
            observed_at=self.T + timedelta(minutes=minutes), source_market_id="m",
        )

    def _report(self, rows, commissions=None):
        from src.arb import find_opportunities

        return find_opportunities(rows, as_of=self.AS_OF, commissions=commissions)

    def test_the_position_under_an_implausible_edge_is_found(self) -> None:
        report = self._report([
            self._q("fanduel", Selection.HOME, 2.846),
            self._q("bovada", Selection.AWAY, 3.579),
            self._q("pinnacle", Selection.HOME, 1.545),
            self._q("betrivers_kambi", Selection.AWAY, 1.534),
        ], commissions={})
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        assert {l.source for l in report.opportunities[0].legs} == {"bovada", "pinnacle"}

    def test_the_narrowing_does_not_re_select_the_fault(self) -> None:
        """Taking the *largest* surviving margin re-selects it: with an inverted
        book present, the exclusion that keeps its long price looks best.

        The fourth argument of ``_q`` is ``minutes``, not a line — so passing
        ``10`` put Pinnacle's legs ten minutes from everybody else's, past the
        three-minute window, and the market was refused ``stale_leg``.
        ``report.opportunities`` was empty and the loop below never ran: this
        class's whole point was asserted over nothing, and it would have passed in
        exactly the failure mode its sibling
        ``test_an_impossible_edge_is_never_deleted_silently`` exists to prevent.
        """
        report = self._report([
            self._q("fanduel", Selection.HOME, 1.10),
            self._q("fanduel", Selection.AWAY, 9.00),
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("pinnacle", Selection.AWAY, 1.30),
            self._q("bovada", Selection.HOME, 1.30),
            self._q("bovada", Selection.AWAY, 2.05),
        ])
        assert report.opportunities, [
            (d.code, d.detail) for d in report.diagnostics
        ]
        for opportunity in report.opportunities:
            assert "fanduel" not in {l.source for l in opportunity.legs}, (
                opportunity.describe()
            )

    @pytest.mark.parametrize("third", [1.90, None])
    def test_an_impossible_edge_is_never_deleted_silently(self, third) -> None:
        """Whatever the narrowing finds — a negative margin, a positive one
        under the floor, or nothing at all — the market must not simply vanish.

        A market in which a 41% apparent edge was seen, judged a mapping fault
        and deleted was summarised as "1 cross-book market, 0 rejected", which
        reads as "compared, no edge".
        """
        rows = [
            self._q("bookA", Selection.HOME, 2.10),
            self._q("bookB", Selection.AWAY, 9.00),
        ]
        if third is not None:
            rows.append(self._q("bookC", Selection.AWAY, third))
        report = self._report(rows, commissions={})
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["margin_implausibly_large"], [
            (d.code, d.detail[:70]) for d in report.diagnostics
        ]

    def test_a_thin_but_real_position_under_the_fault_is_reported(self) -> None:
        """The other side of the same branch: when the narrowing does find
        something above the floor, it is a position and must be published."""
        report = self._report([
            self._q("bookA", Selection.HOME, 2.10),
            self._q("bookB", Selection.AWAY, 9.00),
            self._q("bookC", Selection.AWAY, 1.93),
        ], commissions={})
        assert len(report.opportunities) == 1
        assert {l.source for l in report.opportunities[0].legs} == {"bookA", "bookC"}

    def test_a_market_deleted_for_an_impossible_edge_is_never_silent(self) -> None:
        """"0 rejected" on a market where a mapping fault was seen and the
        market deleted reads as "compared, no edge"."""
        report = self._report([
            self._q("fanduel", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 9.00),
        ], commissions={})
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["margin_implausibly_large"]

    def test_two_time_clusters_do_not_destroy_the_simultaneous_position(self) -> None:
        report = self._report([
            self._q("fanduel", Selection.HOME, 2.30), self._q("fanduel", Selection.AWAY, 1.70),
            self._q("bovada", Selection.HOME, 2.20), self._q("bovada", Selection.AWAY, 1.80),
            self._q("pinnacle", Selection.HOME, 1.70, 10),
            self._q("pinnacle", Selection.AWAY, 2.30, 10),
            self._q("betrivers_kambi", Selection.HOME, 2.10, 10),
            self._q("betrivers_kambi", Selection.AWAY, 1.90, 10),
        ], commissions={})
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        legs = report.opportunities[0].legs
        assert {l.source for l in legs} == {"pinnacle", "betrivers_kambi"}
        spread = max(l.quote.observed_at for l in legs) - min(
            l.quote.observed_at for l in legs
        )
        assert spread == timedelta(0)

    def test_a_genuinely_stale_pair_is_still_refused(self) -> None:
        report = self._report([
            self._q("fanduel", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.10, 10),
        ], commissions={})
        assert report.opportunities == []
        assert "stale_leg" in [d.code for d in report.diagnostics]


class TestAWomensYouthSideIsNotTheSeniorSide:
    """``competition_marker`` returned on the first pattern that matched.

    "Frauen-Bundesliga" and "Frauen-Bundesliga U17" both gave ``w``, so a
    women's U17 fixture and the senior women's fixture between the same clubs
    produced byte-identical event keys — the participant pair is the same, so no
    orientation or pairing check can see it, and soccer's 30-hour clustering
    joins them.
    """

    def _key(self, competition, team="Freiburg"):
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug, competition_marker, with_marker

        marker = competition_marker(competition, LeagueSport.SOCCER)
        return _open_slug(with_marker(team, marker), sport=LeagueSport.SOCCER)

    def test_the_three_sides_of_one_club_are_three_keys(self) -> None:
        mens = self._key("Germany - Bundesliga")
        senior = self._key("Germany - Frauen-Bundesliga")
        youth = self._key("Germany - Frauen-Bundesliga U17")
        assert len({mens, senior, youth}) == 3, (mens, senior, youth)

    @pytest.mark.parametrize("competition,expected", [
        ("Germany - Frauen-Bundesliga U17", "w u17"),
        ("Italy - Campionato Femminile U19", "w u19"),
        ("England - Women's U19", "w u19"),
        ("Germany - Bundesliga U19", "u19"),
        ("Germany - Frauen-Bundesliga", "w"),
        ("England - Premier League", None),
    ])
    def test_every_marker_the_name_carries_is_returned(self, competition, expected) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import competition_marker

        assert competition_marker(competition, LeagueSport.SOCCER) == expected

    @pytest.mark.parametrize("team", [
        "Freiburg", "Freiburg U17", "Freiburg Frauen", "Freiburg Frauen U17",
        "Freiburg W", "Freiburg U17 Frauen",
    ])
    def test_every_venue_spelling_lands_on_one_key(self, team) -> None:
        """Including the two marker *orders*: joined in arrival order,
        ``Freiburg U17 w`` and ``Freiburg w u17`` were two keys for one side."""
        assert self._key("Germany - Frauen-Bundesliga U17", team) == self._key(
            "Germany - Frauen-Bundesliga U17", "Freiburg"
        )


class TestASpreadsTwoContractsStayApart:
    """Round 16 keyed the price-agreement market on ``abs(line)``.

    That grouped a contract's two halves — right — and fused the two *different*
    contracts a symmetric ladder posts: ``home -1.5 / away +1.5`` and ``home
    +1.5 / away -1.5``.  133 fixtures in the corpus have a source posting both,
    and collapsing them gave a venue thin on one side of each a second outlying
    side and an ERROR it had not earned.
    """

    ORDER_DRIVEN = frozenset({"matchbook", "smarkets", "kalshi", "polymarket", "sxbet"})

    def _fixture(self, index, market, line, prices):
        key = f"MLB-a{index}@MLB-b{index}:2026-07-29"
        common = dict(
            sport=Sport.BASEBALL, league="MLB", event_key=key,
            source_event_id=f"e{index}", home_participant=f"MLB-b{index}",
            away_participant=f"MLB-a{index}", home_team=f"B{index}",
            away_team=f"A{index}", commence_time=LATER, market=market,
        )
        first = Selection.HOME if market is Market.SPREAD else Selection.OVER
        second = Selection.AWAY if market is Market.SPREAD else Selection.UNDER
        lines = (line, -line) if market is Market.SPREAD else (line, line)
        rows = []
        for source, (near, far) in prices.items():
            rows.append(make_quote(source=source, selection=first,
                                   decimal_odds=near, line=lines[0], **common))
            rows.append(make_quote(source=source, selection=second,
                                   decimal_odds=far, line=lines[1], **common))
        return rows

    def _errors(self, rows):
        from src.validation import Severity, validate

        report = validate(rows, order_book_sources=self.ORDER_DRIVEN)
        return [
            f for f in report.findings
            if f.code == "prices_disagree_with_every_other_source"
            and f.severity is Severity.ERROR
        ]

    def test_an_honest_thin_symmetric_ladder_is_not_reported(self) -> None:
        rows = []
        honest = {"bovada": (2.174, 1.60), "fanduel": (2.174, 1.60), "pinnacle": (2.174, 1.60)}
        mirror = {"bovada": (1.60, 2.174), "fanduel": (1.60, 2.174), "pinnacle": (1.60, 2.174)}
        for index in range(10):
            rows += self._fixture(index, Market.SPREAD, -1.5, {**honest, "matchbook": (1.05, 1.60)})
            rows += self._fixture(index, Market.SPREAD, 1.5, {**mirror, "matchbook": (1.60, 1.05)})
        assert self._errors(rows) == []

    def test_a_swapped_spread_is_still_caught(self) -> None:
        rows = []
        for index in range(10):
            rows += self._fixture(index, Market.SPREAD, -1.5, {
                "bovada": (2.174, 1.60), "fanduel": (2.174, 1.60),
                "pinnacle": (2.174, 1.60), "matchbook": (1.60, 2.174)})
        assert self._errors(rows)

    def test_a_swapped_total_is_still_caught(self) -> None:
        rows = []
        for index in range(10):
            rows += self._fixture(index, Market.TOTAL, 8.5, {
                "bovada": (2.174, 1.60), "fanduel": (2.174, 1.60),
                "pinnacle": (2.174, 1.60), "matchbook": (1.60, 2.174)})
        assert self._errors(rows)


class TestARepairedRowSurvivesToTheOperator:
    """``repaired`` was a write-only counter: nothing persisted it, no health
    line mentioned it, no dashboard cell showed it.

    Before it existed those rows were at least counted in the persisted
    ``skipped`` table — mislabelled, but with a number an operator could watch.
    Introducing the field and stopping there traded a wrong label for silence.
    """

    def test_it_reaches_the_health_line_the_row_and_its_own_table(self, tmp_path) -> None:
        from src.sources.base import SourceHealth
        from src.store import Store

        health = SourceHealth(
            source_key="betrivers_kambi", ok=True, checked_at=FETCHED,
            quote_count=1107, skipped_count=700, repaired_count=54,
        )
        assert "54 repaired" in health.summary()

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.save_health(run, health)
            store.save_repaired(run, "betrivers_kambi", {"feed_american_odds": 54})
            row = store.query(
                "SELECT repaired_count FROM source_health WHERE run_id = ?", (run,)
            )[0]
            detail = store.query(
                "SELECT reason, count FROM repaired WHERE run_id = ?", (run,)
            )[0]
        assert row["repaired_count"] == 54
        assert detail["count"] == 54


class TestASkipNoteNeverContradictsTheMarket:
    """A baseball handicap is a run line — a core market — and the dashboard
    said it was "not one of the four kinds collected here".

    The reason *code* was wrong too: ``market_type_out_of_scope`` for a market
    type that is in scope.  Smarkets does not fetch the ladder because its
    published rate limit does not leave room, which is a coverage choice and
    belongs under its own reason.
    """

    def test_the_in_scope_markets_are_filed_as_a_coverage_choice(self) -> None:
        import glob

        from src.raw_store import RawStore
        from src.sources.smarkets import parse_smarkets

        store = RawStore("tests/fixtures/raw")
        outcome = parse_smarkets([
            store.read(p) for p in sorted(glob.glob("tests/fixtures/raw/smarkets__*.json"))
        ])
        honest = {
            reason: count for reason, count in outcome.skipped.items()
            if reason.startswith("market_in_scope_but_not_fetched")
        }
        assert sum(honest.values()) >= 300, dict(outcome.skipped)
        assert any("HANDICAP" in reason for reason in honest)
        assert any("OVER_UNDER" in reason for reason in honest)
        # ...and no *bare* full-game spread or total is still filed as out of
        # scope.  The names that remain there carry a period this pipeline does
        # not collect ("FIRST_QUARTER_HANDICAP") or a market that is not one of
        # the four ("OVER_UNDER_3_WAY", "HOME_TEAM_HITS_OVER_UNDER"), which is
        # what that reason is for.
        assert not any(
            reason.endswith(":HANDICAP") or reason.endswith(":OVER_UNDER")
            for reason in outcome.skipped
            if reason.startswith("market_type_out_of_scope")
        )

    def test_the_new_reason_has_a_note(self) -> None:
        from src.report import SKIP_NOTES

        prefixes = {prefix for prefix, _ in SKIP_NOTES}
        assert "market_in_scope_but_not_fetched" in prefixes


class TestABankrollBelowOneUnitIsRefused:
    """``stake_candidates`` floors every leg to zero, every settlement outcome
    then returns exactly zero, ``is_risk_free`` passes, and
    ``rounding_destroys_edge`` never fires — so ``--stake 0.5`` printed "margin
    9.09%, guaranteed +0.00 on 0" with both legs at stake 0.00.
    """

    def test_it_is_refused_rather_than_reported(self) -> None:
        from src.arb import find_opportunities

        rows = [
            make_quote(source="a", selection=Selection.HOME, decimal_odds=2.20),
            make_quote(source="b", selection=Selection.AWAY, decimal_odds=2.20),
        ]
        with pytest.raises(ValueError, match="below one"):
            find_opportunities(rows, total_stake=0.5)

    def test_one_whole_unit_is_refused_with_a_reason_not_a_traceback(self) -> None:
        """Where the boundary actually is, corrected.

        This test asserted that "a bankroll of exactly one increment can hold a
        position and must not be refused", then looped over
        ``report.opportunities`` — which is **empty**, because one whole unit
        cannot cover two legs at all: whichever leg got the unit, the other would
        be staked nothing, and that is not a position.  So the claim was false and
        nothing checked it either way.

        What must hold is the distinction the class is about: below one unit is an
        argument the caller cannot mean and raises; at one unit is a real bankroll
        that simply cannot buy this market, so it is *refused with a reason*.
        """
        from src.arb import find_opportunities

        rows = [
            make_quote(source="a", selection=Selection.HOME, decimal_odds=2.20),
            make_quote(source="b", selection=Selection.AWAY, decimal_odds=2.20),
        ]
        report = find_opportunities(rows, total_stake=1.0, as_of=None)
        assert report.comparable_group_count == 1
        assert report.opportunities == []
        assert [d.code for d in report.diagnostics] == ["rounding_destroys_edge"]

        # Two units is the smallest bankroll this market can take, and it is taken.
        wider = find_opportunities(rows, total_stake=2.0, as_of=None)
        assert len(wider.opportunities) == 1
        assert wider.opportunities[0].total_stake == 2.0


class TestADoubleheaderTickerIsReadable:
    """Kalshi suffixes a doubleheader's ticker with ``G1``/``G2``.

    Found by live acceptance, not by the fixtures: ``26JUL291310ATLNYMG1`` read
    the trailing ordinal as part of the home code, no split resolved, and the
    fixture was refused as an unreadable ticker.  One rejection marks the whole
    source unhealthy, so 556 good rows were graded broken over one doubleheader
    — and the two halves of it were lost.
    """

    @pytest.mark.parametrize("ticker,teams,ordinal", [
        ("26JUL291310ATLNYMG1", "ATLNYM", "G1"),
        ("26JUL291310ATLNYMG2", "ATLNYM", "G2"),
        ("26JUL291310ATLNYM", "ATLNYM", None),
        ("26JUL302210SEALAD", "SEALAD", None),
    ])
    def test_the_ordinal_is_read_apart_from_the_team_codes(
        self, ticker, teams, ordinal
    ) -> None:
        from src.sources.kalshi import _GAME_PART

        match = _GAME_PART.match(ticker)
        assert match is not None, ticker
        assert match.group("teams") == teams
        assert match.group("game") == ordinal

    @pytest.mark.parametrize("ticker", [
        "26JUL291310ATLNYMG1", "26JUL291310ATLNYMG2", "26JUL291310ATLNYM",
    ])
    def test_both_halves_of_a_doubleheader_split(self, ticker) -> None:
        from src.leagues import league as get_league
        from src.sources.kalshi import _split_codes

        assert _split_codes(ticker, get_league("MLB")) == ("ATL", "NYM")

    def test_an_ambiguous_ticker_is_still_refused(self) -> None:
        """The ordinal must not be bought at the cost of the guard it sits
        beside: choosing the wrong split does not lose a fixture, it swaps which
        side every price refers to."""
        from src.leagues import league as get_league
        from src.sources.kalshi import _split_codes

        assert _split_codes("26JUL291310ZZZZZZ", get_league("MLB")) is None


# ── round eighteen ───────────────────────────────────────────────────────────


class TestTheMarkerReachesThePricedSideToo:
    """The competition marker was applied to the fixture's participants and
    nowhere else.

    So in the **only** case it exists for — the venue marks the competition and
    not the teams — the fixture's keys carried the marker and every
    competitor-named price still resolved to the unmarked slug, matched neither
    side, and was *rejected*.  One such fixture marks the whole source unhealthy.

    Four of ten adapters were affected: FanDuel, both Kambi tenants and
    Matchbook, which read the priced side from a competitor *name*.  Pinnacle,
    Smarkets, SX Bet, Bovada, Kalshi and Polymarket read it from a structured
    field and are immune.
    """

    def _kambi_payload(self, competition):
        event = {
            "id": 5001, "name": "Palmeiras-SP - Corinthians-SP",
            "englishName": "Palmeiras-SP - Corinthians-SP",
            "homeName": "Palmeiras-SP", "awayName": "Corinthians-SP",
            "state": "NOT_STARTED", "start": "2099-12-01T18:00:00Z",
            "group": competition,
            "path": [
                {"termKey": "football", "name": "Football"},
                {"termKey": "brazil", "name": "Brazil"},
                {"termKey": "comp", "name": competition},
            ],
        }
        offers = {"betOffers": [{
            "id": 900001, "eventId": 5001, "closed": "2099-12-01T18:00:00Z",
            "betOfferType": {"englishName": "Match"},
            "criterion": {"englishLabel": "Full Time", "label": "Full Time"},
            "outcomes": [
                {"id": 1, "type": "OT_ONE", "participant": "Palmeiras-SP",
                 "englishLabel": "Palmeiras-SP", "odds": 2100, "oddsAmerican": "+110"},
                {"id": 2, "type": "OT_CROSS", "englishLabel": "Draw",
                 "odds": 3400, "oddsAmerican": "+240"},
                {"id": 3, "type": "OT_TWO", "participant": "Corinthians-SP",
                 "englishLabel": "Corinthians-SP", "odds": 3600, "oddsAmerican": "+260"},
            ],
        }], "events": [event]}
        return [
            _raw("betrivers_kambi", "listview:football/brazil", {"events": [{"event": event}]}),
            _raw("betrivers_kambi", "betoffer:football/brazil:batch-01", offers),
        ]

    def test_a_womens_competition_prices_all_three_legs(self) -> None:
        from src.sources.betrivers_kambi import parse_kambi

        outcome = parse_kambi(self._kambi_payload("Campeonato Brasileiro Feminino"))
        assert outcome.rejections == [], [
            (r.reason, r.detail[:70]) for r in outcome.rejections
        ]
        assert len(outcome.quotes) == 3
        assert {q.selection for q in outcome.quotes} == {
            Selection.HOME, Selection.DRAW, Selection.AWAY
        }

    def test_the_marked_key_is_the_one_that_reaches_the_rows(self) -> None:
        from src.sources.betrivers_kambi import parse_kambi

        outcome = parse_kambi(self._kambi_payload("Campeonato Brasileiro Feminino"))
        assert all(q.home_participant.endswith("w") for q in outcome.quotes)
        assert all(q.away_participant.endswith("w") for q in outcome.quotes)

    def test_an_unmarked_competition_is_unaffected(self) -> None:
        from src.sources.betrivers_kambi import parse_kambi

        outcome = parse_kambi(self._kambi_payload("Campeonato Brasileiro Serie A"))
        assert outcome.rejections == []
        assert len(outcome.quotes) == 3
        assert not any(q.home_participant.endswith("w") for q in outcome.quotes)

    def test_every_name_reading_adapter_carries_the_marker_to_the_price(self) -> None:
        """Structural: the three that resolve a priced side from a name must all
        pass the fixture's marker through, or this comes back in whichever one
        was missed."""
        import pathlib

        for key in ("betrivers_kambi", "matchbook", "fanduel"):
            text = pathlib.Path(f"src/sources/{key}.py").read_text()
            assert "marker: str | None" in text, key
            assert text.count("with_marker(") >= 2, (
                f"{key} applies the marker in fewer than two places, so the "
                "priced side is not going through it"
            )


class TestAnUnnamedTennisTourIsNotDiscarded:
    """Pinnacle returned ``None`` for a tour its prefix table does not name, and
    the caller turns that into a counted skip.

    ``src.leagues`` records why ``TENNIS_OTHER`` exists — "an adapter … has to
    either guess a tour or discard the match" — and Matchbook, SX Bet and
    Smarkets all take the catch-all.  This adapter alone discarded, so on a
    Davis Cup or United Cup week the sharpest book in the set contributed
    nothing for those matches and the loss was one bucket among hundreds.
    """

    @pytest.mark.parametrize("name,expected", [
        ("ATP Challenger Bonn - R1", "ATP_CHALLENGER"),
        ("ATP Los Cabos - R1", "ATP"),
        ("WTA Washington - R16", "WTA"),
        ("ITF Men Pitesti - R1", "ITF"),
        ("Davis Cup", "TENNIS_OTHER"),
        ("United Cup", "TENNIS_OTHER"),
        ("Laver Cup", "TENNIS_OTHER"),
        ("Mens UTR Pro Series, Argentina", "TENNIS_OTHER"),
    ])
    def test_the_tour_or_the_catch_all(self, name, expected) -> None:
        from src.schema import Sport as SchemaSport
        from src.sources.pinnacle import canonical_league_key

        assert canonical_league_key(SchemaSport.TENNIS, 999_999, name) == expected

    def test_the_catch_all_is_a_registered_league(self) -> None:
        from src.leagues import is_known
        from src.sources.pinnacle import TENNIS_CATCH_ALL

        assert is_known(TENNIS_CATCH_ALL)


class TestADoubleheaderKeepsItsTeamTotals:
    """The period-qualifier guard could not tell a scoring window from a
    doubleheader marker.

    It exists to stop "Cincinnati Reds - First 5 Innings" being absorbed into
    the club name, and "White Sox - Game 2" has the same shape — so every team
    total on a doubleheader was dropped into ``unmapped_criterion:baseball``,
    the bucket that legitimately holds hundreds of props, and the loss was
    invisible.
    """

    def _side(self, label):
        from datetime import datetime as _dt

        from src.leagues import league as get_league
        from src.participants import canonical_participant
        from src.sources.betrivers_kambi import (
            TEAM_TOTAL_AFFIXES, _Fixture, _team_total_side,
        )

        competition = get_league("MLB")
        fixture = _Fixture(
            event_id="1", sport=Sport.BASEBALL, competition=competition,
            home=canonical_participant("Chicago White Sox", competition),
            away=canonical_participant("Cleveland Guardians", competition),
            commence_time=_dt(2099, 12, 1, tzinfo=UTC), base_key="k",
        )
        return _team_total_side(label, TEAM_TOTAL_AFFIXES[Sport.BASEBALL], fixture)

    @pytest.mark.parametrize("label,expected", [
        ("Total Runs by White Sox", Side.HOME),
        ("Total Runs by White Sox - Game 2", Side.HOME),
        ("Total Runs by Cleveland Guardians - Game 2", Side.AWAY),
        ("Total Runs by White Sox - Game 1", Side.HOME),
    ])
    def test_a_doubleheader_team_total_still_resolves(self, label, expected) -> None:
        assert self._side(label) is expected

    @pytest.mark.parametrize("label", [
        "Total Runs by White Sox - First 5 Innings",
        "Total Runs by White Sox - Inning 1",
    ])
    def test_a_period_qualified_team_total_is_still_refused(self, label) -> None:
        """The guard the ordinal must not weaken: a five-inning team total is
        not a nine-inning one."""
        assert self._side(label) is None


class TestAWomensChallengerIsNotAMensOne:
    """FanDuel's tour table tested the *tier* before the *tour*.

    ``matchbook.py`` and ``sxbet.py`` both record the live cost of exactly that
    — 252 markets on one capture filed as ``ATP_CHALLENGER`` — and were fixed;
    this table kept the original order and dodged it only because FanDuel
    happens to write "WTA Vancouver 2026" and "West Vancouver Challenger 2026"
    rather than combining the two.
    """

    @pytest.mark.parametrize("name,expected", [
        ("WTA Challenger Tampico", "WTA"),
        ("Women's Challenger Buenos Aires", "WTA"),
        ("WTA Vancouver 2026", "WTA"),
        ("West Vancouver Challenger 2026", "ATP_CHALLENGER"),
        ("ATP Challenger Liberec", "ATP_CHALLENGER"),
        ("ATP Washington 2026", "ATP"),
        ("ITF Monastir", "ITF"),
    ])
    def test_the_tour_wins_over_the_tier(self, name, expected) -> None:
        from src.sources.fanduel import tennis_league_for

        assert tennis_league_for(name) == expected

    def test_all_three_adapters_agree(self) -> None:
        """The same string must not file three ways across three books."""
        from src.sources.fanduel import tennis_league_for
        from src.sources.matchbook import _tennis_league

        for name in ("WTA Challenger Tampico", "ATP Challenger Liberec", "ITF Monastir"):
            assert tennis_league_for(name) == _tennis_league(name), name


class TestTheGatesAreSearchedTogetherNotInSequence:
    """Freshness and settlement were two narrowings in a row, each
    irreversible, and they composed badly in both directions.

    A window chosen on pre-settlement margin was persisted, so the settlement
    narrowing could only look inside it and a real same-regime position in an
    *earlier* window became unreachable.  And the settlement narrowing had no
    freshness retry of its own, so narrowing to one regime could hand back stale
    legs while a simultaneous same-regime position sat underneath.  On
    randomised realistic slates one or other fired on 1,046 of 40,000 trials;
    on the captured slate 710 of 724 cross-source groups span more than the
    window, so this is the geometry the data has.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    T = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, seconds=0):
        return make_quote(
            source=source, selection=selection, decimal_odds=odds,
            observed_at=self.T + timedelta(seconds=seconds), source_market_id="m",
        )

    def _report(self, rows):
        from src.arb import find_opportunities

        return find_opportunities(rows, as_of=self.AS_OF, commissions={})

    def test_a_settlement_narrowing_that_leaves_stale_legs_finds_the_fresh_pair(
        self,
    ) -> None:
        """Adding an off-regime venue that appears in no reported position must
        not destroy a simultaneous same-regime one."""
        rows = [
            self._q("kalshi", Selection.HOME, 2.40),
            self._q("pinnacle", Selection.AWAY, 2.40),
            self._q("fanduel", Selection.HOME, 2.20, -36000),
            self._q("bovada", Selection.HOME, 2.10),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        assert {l.source for l in report.opportunities[0].legs} == {"pinnacle", "bovada"}

    def test_an_earlier_window_is_reachable_after_the_late_one_fails(self) -> None:
        """The late window scores higher *before* settlement is considered and
        used to win outright; its only assignment then failed the regime test
        and the early window's real position was unreachable."""
        rows = [
            self._q("matchbook", Selection.HOME, 1.380),
            self._q("matchbook", Selection.AWAY, 3.497),
            self._q("pinnacle", Selection.HOME, 1.496, 20),
            self._q("pinnacle", Selection.AWAY, 2.872, 20),
            self._q("smarkets", Selection.HOME, 1.521, 40),
            self._q("smarkets", Selection.AWAY, 2.825, 40),
            self._q("sxbet", Selection.HOME, 1.569, 300),
            self._q("sxbet", Selection.AWAY, 2.766, 300),
            self._q("kalshi", Selection.HOME, 1.428, 340),
            self._q("kalshi", Selection.AWAY, 3.350, 340),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        legs = report.opportunities[0].legs
        assert {l.source for l in legs} == {"matchbook", "smarkets"}
        spread = max(l.quote.observed_at for l in legs) - min(
            l.quote.observed_at for l in legs
        )
        assert spread <= timedelta(seconds=180)

    def test_a_genuinely_unpairable_market_is_still_refused(self) -> None:
        """The control: two venues that neither void together nor were priced
        together must still be refused, and say which."""
        rows = [
            self._q("kalshi", Selection.HOME, 2.40),
            self._q("pinnacle", Selection.AWAY, 2.40, 36000),
        ]
        report = self._report(rows)
        assert report.opportunities == []
        assert report.diagnostics


class TestAThinStatedSizeDoesNotCostTheMarket:
    """The limit gate is the one refusal with no retry.

    ``_best_assignment`` picks on price alone, so an exchange top-of-book with
    0.60 resting behind it wins the leg and then cannot be placed — and the
    whole market went with it, while the same market without that venue paid
    2.44%.  Softer and more common: at a limit of 6.64 the market *was*
    reported, as +0.30 on a bankroll of 12 rather than the +2.50 on 100
    available underneath, and then ranked by that +0.30.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _report(self, limit):
        from src.arb import find_opportunities

        thin = make_quote(source="matchbook", selection=Selection.AWAY,
                          decimal_odds=2.10, source_market_id="m")
        if limit is not None:
            thin = thin.model_copy(update={"limit_amount": limit})
        rows = [
            thin,
            make_quote(source="pinnacle", selection=Selection.AWAY,
                       decimal_odds=2.05, source_market_id="m"),
            make_quote(source="fanduel", selection=Selection.HOME,
                       decimal_odds=2.05, source_market_id="m"),
        ]
        return find_opportunities(rows, as_of=self.AS_OF, commissions={})

    def test_an_unplaceable_leg_falls_through_to_the_position_underneath(self) -> None:
        report = self._report(0.60)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        assert {l.source for l in report.opportunities[0].legs} == {"pinnacle", "fanduel"}

    def test_the_candidate_that_pays_more_wins_even_at_a_lower_margin(self) -> None:
        """Margin is not money: 3.60% capped at 12 pays +0.30, 2.44% on 100 pays
        +2.50, and the published report is ordered by the latter."""
        report = self._report(6.64)
        assert len(report.opportunities) == 1
        opportunity = report.opportunities[0]
        assert opportunity.guaranteed_profit > 2.0, opportunity.describe()
        assert {l.source for l in opportunity.legs} == {"pinnacle", "fanduel"}

    def test_an_unlimited_venue_still_wins_on_price(self) -> None:
        report = self._report(None)
        assert {l.source for l in report.opportunities[0].legs} == {"matchbook", "fanduel"}


class TestOneRecordIsCountedOnce:
    """Kalshi queued a market before the guard that rejects it, so a record with
    no ``event_ticker`` was rejected *and* counted again in the second pass as
    ``market_on_out_of_scope_game`` — whose note says "belongs to a fixture that
    was not collected", which is not what happened.
    """

    def test_a_market_with_no_event_ticker_is_counted_once(self) -> None:
        import glob

        from src.raw_store import RawStore
        from src.sources.kalshi import parse_kalshi

        store = RawStore("tests/fixtures/raw")
        raws, injected = [], False
        for path in sorted(glob.glob("tests/fixtures/raw/kalshi__*.json")):
            raw = store.read(path)
            payload = raw.json()
            if not injected and isinstance(payload, dict) and payload.get("markets"):
                broken = json.loads(json.dumps(payload["markets"][0]))
                broken["ticker"] = "KXMLBGAME-BROKEN"
                broken["event_ticker"] = ""
                payload["markets"].append(broken)
                raw = dataclasses.replace(raw, body=json.dumps(payload))
                injected = True
            raws.append(raw)
        assert injected
        outcome = parse_kalshi(raws)
        assert len(outcome.rejections) == 1
        assert outcome.rejections[0].reason == "unreadable_game_ticker"
        assert outcome.skipped.get("market_on_out_of_scope_game", 0) == 0
        assert len(outcome.quotes) == 590


class TestAPinnedClubSortsItsMarkersToo:
    """The disambiguation early return folds the suffix spellings and did not
    sort them, so a competition supplying two markers split the key by the order
    they arrived in."""

    @pytest.mark.parametrize("names", [
        ["Barcelona SC", "Barcelona SC U17", "Barcelona SC Frauen", "Barcelona SC W U17"],
        ["CD Nacional", "CD Nacional U17", "CD Nacional Women"],
    ])
    def test_one_side_is_one_key_whatever_the_venue_wrote(self, names) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug, competition_marker, with_marker

        marker = competition_marker("Germany - Frauen-Bundesliga U17", LeagueSport.SOCCER)
        keys = {
            _open_slug(with_marker(name, marker), sport=LeagueSport.SOCCER)
            for name in names
        }
        assert len(keys) == 1, keys

    def test_the_pin_still_separates_the_clubs_it_pins(self) -> None:
        from src.leagues import Sport as LeagueSport
        from src.participants import _open_slug, competition_marker, with_marker

        marker = competition_marker("Germany - Frauen-Bundesliga U17", LeagueSport.SOCCER)
        pinned = _open_slug(with_marker("Barcelona SC", marker), sport=LeagueSport.SOCCER)
        other = _open_slug(with_marker("Barcelona", marker), sport=LeagueSport.SOCCER)
        assert pinned != other


class TestTheRepairedCountReachesThePage:
    """The counter was introduced, persisted, and rendered nowhere — so the rows
    it describes appeared on the dashboard *less* than before it existed: they
    had at least been in the skipped breakdown, mislabelled."""

    def test_the_payload_carries_the_per_reason_detail(self, tmp_path) -> None:
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.save_health(run, SourceHealth(
                source_key="betrivers_kambi", ok=True, checked_at=FETCHED,
                quote_count=5, repaired_count=54,
            ))
            store.save_repaired(run, "betrivers_kambi", {"feed_american_odds": 54})
            store.save_quotes(run, [make_quote(source="betrivers_kambi")])
            store.finish_run(run, finished_at=FETCHED, report=ValidationReport())
            data = build_report(store)
        assert any(
            entry["source"] == "betrivers_kambi" and entry["count"] == 54
            for entry in data["repaired"]
        ), data.get("repaired")

    def test_the_page_renders_it(self) -> None:
        import pathlib

        text = pathlib.Path("src/report_assets.py").read_text()
        assert "DATA.repaired" in text, "the page never reads the repaired detail"
        assert "rebuilt and kept" in text


class TestASubUnitStakeIsAnArgumentError:
    """``find_opportunities`` refuses it, and reaching that refusal from the
    command line means a traceback — the failure ``_positive`` exists to
    remove."""

    def test_the_parser_refuses_it(self) -> None:
        import argparse

        from src.collector import _positive

        with pytest.raises(argparse.ArgumentTypeError, match="at least one"):
            _positive("stake")("0.5")

    def test_one_whole_unit_is_accepted(self) -> None:
        from src.collector import _positive

        assert _positive("stake")("1") == 1.0

    def test_other_arguments_are_unaffected(self) -> None:
        from src.collector import _positive

        assert _positive("count")("3") == 3


# ── round nineteen ───────────────────────────────────────────────────────────


class TestABindingSizeMovesTheLegNotTheVenue:
    """The candidate search ranged over source **subsets**; what has to be
    optimised is an **assignment**.

    When a chosen leg's stated size binds, the legal move is to take that
    selection from the next-best counterparty and keep the venue's other leg —
    which no subset expresses.  Dropping the venue fails in both shapes the live
    data has: two venues state sizes, so one removal still lands on a limited
    leg; or the venue also holds the best price on another selection, so
    removing it removes the good leg too.

    Measured: 51 of 2,834 arb-bearing markets lost money this way, and 617 of
    2,244 cross-source groups on the captured slate have two or more
    limit-stating sources — Pinnacle states a size on every one of its 29,446
    quotes, so "only one venue states a size", which is what the covering test
    exercised, is the case that does *not* occur.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, limit=None, **kwargs):
        row = make_quote(source=source, selection=selection, decimal_odds=odds,
                         source_market_id="m", **kwargs)
        return row if limit is None else row.model_copy(update={"limit_amount": limit})

    def _report(self, rows, **kwargs):
        from src.arb import find_opportunities

        return find_opportunities(rows, as_of=self.AS_OF, commissions={}, **kwargs)

    @pytest.mark.parametrize("thin", [8.31, 2.00, 0.50])
    def test_two_limit_stating_venues_do_not_shrink_the_position(self, thin) -> None:
        """One removal still lands on a limited leg, so the position was
        published at a fraction of its size — +0.42 on 17, and at a thinner size
        +0.05 on **4**, while +1.97 on 100 sat underneath."""
        rows = [
            self._q("sxbet", Selection.HOME, 2.240, thin),
            self._q("sxbet", Selection.AWAY, 1.758),
            self._q("matchbook", Selection.AWAY, 2.044, 3.00),
            self._q("matchbook", Selection.HOME, 1.930),
            self._q("bovada", Selection.HOME, 2.150),
            self._q("bovada", Selection.AWAY, 1.859),
            self._q("fanduel", Selection.HOME, 1.870),
            self._q("fanduel", Selection.AWAY, 1.961),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        opportunity = report.opportunities[0]
        assert opportunity.guaranteed_profit > 1.5, opportunity.describe()
        assert {l.source for l in opportunity.legs} == {"bovada", "fanduel"}

    def test_a_venue_holding_two_best_prices_keeps_the_good_leg(self) -> None:
        """Removing the venue removes the good leg too, and the drop-candidate
        then falls below two counterparties — so the market was published at
        +1.23 on 23 while +2.33 on 50 was available."""
        soccer = dict(
            sport=Sport.SOCCER, league="EPL", event_key="SOCCER-a@SOCCER-b:2026-07-29",
            home_participant="SOCCER-b", away_participant="SOCCER-a",
            home_team="B", away_team="A", source_event_id="e1", commence_time=LATER,
        )
        rows = [
            self._q("fanduel", Selection.HOME, 2.895, None, **soccer),
            self._q("fanduel", Selection.DRAW, 3.113, 44.73, **soccer),
            self._q("fanduel", Selection.AWAY, 2.907, None, **soccer),
            self._q("betrivers_kambi", Selection.HOME, 3.649, None, **soccer),
            self._q("betrivers_kambi", Selection.DRAW, 3.461, 7.80, **soccer),
            self._q("betrivers_kambi", Selection.AWAY, 2.281, 8.29, **soccer),
        ]
        report = self._report(rows, total_stake=50)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        opportunity = report.opportunities[0]
        assert opportunity.guaranteed_profit > 2.5, opportunity.describe()
        # +2.58 on 41 of the 50: the bankroll is a ceiling, not a quota, so the
        # position that guarantees most is not the one that spends most.
        assert opportunity.total_stake == 41

    def test_no_leg_is_ever_over_its_stated_size(self) -> None:
        """The search may move a leg; it may never overstake one."""
        rows = [
            self._q("sxbet", Selection.HOME, 2.240, 8.31),
            self._q("sxbet", Selection.AWAY, 1.758),
            self._q("matchbook", Selection.AWAY, 2.044, 3.00),
            self._q("matchbook", Selection.HOME, 1.930),
            self._q("bovada", Selection.HOME, 2.150),
            self._q("bovada", Selection.AWAY, 1.859),
        ]
        for opportunity in self._report(rows).opportunities:
            for leg in opportunity.legs:
                stated = leg.quote.limit_amount
                assert stated is None or leg.stake <= stated + 1e-9, leg.describe()

    def test_the_search_stays_cheap(self) -> None:
        """Ten sources, ten observation times, every venue stating a size — the
        shape that would blow up a naive enumeration."""
        import random
        import time

        random.seed(3)
        rows = []
        for index in range(10):
            for selection in (Selection.HOME, Selection.AWAY):
                rows.append(self._q(
                    f"s{index}", selection, round(random.uniform(1.8, 2.3), 3),
                    round(random.uniform(2, 500), 2),
                    observed_at=FETCHED + timedelta(seconds=index * 20),
                ))
        started = time.perf_counter()
        for _ in range(20):
            self._report(rows)
        assert (time.perf_counter() - started) / 20 < 0.05


class TestARefusalDoesNotBlameTheClockForAFlatMarket:
    """``stale_leg`` claimed "no set of these books priced this market close
    enough together" whenever nothing survived — including when a simultaneous
    pair existed and simply had no edge.

    No money at stake; it sends an operator to look at collection timing for a
    market that is not arbitrageable.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    T = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, seconds=0):
        return make_quote(source=source, selection=selection, decimal_odds=odds,
                          observed_at=self.T + timedelta(seconds=seconds),
                          source_market_id="m")

    def _detail(self, rows):
        from src.arb import find_opportunities

        report = find_opportunities(rows, as_of=self.AS_OF, commissions={})
        assert report.opportunities == []
        return report.diagnostics[0].detail

    def test_a_flat_simultaneous_pair_is_named_as_such(self) -> None:
        detail = self._detail([
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("pinnacle", Selection.AWAY, 1.90),
            self._q("bovada", Selection.HOME, 1.75),
            self._q("bovada", Selection.AWAY, 1.85),
            self._q("fanduel", Selection.HOME, 2.00, 400),
            self._q("fanduel", Selection.AWAY, 2.00, 400),
        ])
        assert "have no edge between them" in detail, detail

    def test_a_genuinely_unpriced_window_still_says_so(self) -> None:
        detail = self._detail([
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.10, 400),
        ])
        assert "close enough together" in detail, detail


class TestAQuarterLineIsDescribedAsWhatItIs:
    """The dashboard split the world into half-lines and whole ones, so a
    quarter line fell into the **whole** branch — the one that asserts a push.

    254 rows on the captured slate rendered as impossible events ("a 0.25-goal
    win refunds"), and worse, the sentence hid a payout: on away +0.25 a draw is
    a half-win, and the reader was told the only outcomes were an away win and a
    refund that cannot happen.  ``src/arb.py`` models this exactly, so the page
    and the detector described the same stored row differently — and the page is
    what a human reads.
    """

    def test_the_page_has_a_third_case(self) -> None:
        import pathlib

        text = pathlib.Path("src/report_assets.py").read_text()
        assert "isQuarter" in text, "quarter lines still fall through to whole"
        assert "pays half" in text

    def test_the_smoke_harness_covers_quarter_lines(self) -> None:
        """The gap that let it ship: 30 sentence cases, not one of them a
        quarter line."""
        import pathlib

        text = pathlib.Path("tests/dashboard_smoke.mjs").read_text()
        assert "-0.25" in text and "3.25" in text and "2.75" in text


class TestAnEmptyRunIsRefusedRatherThanReadAsQuiet:
    """``_resolve_run``'s docstring lists "a run that stored no rows" as one of
    the three failures indistinguishable from a quiet slate, and never checked
    it — ``run_row`` selects ``quote_count`` for exactly this and nothing read
    it.

    ``collect_once`` calls ``finish_run`` unconditionally, so a pass in which
    every source failed leaves a *finished* run holding nothing, and the read
    commands answered "0 rows", "0 opportunities" and "0 markets shown", each
    exiting 0 — the sentences a quiet slate produces.
    """

    def test_it_is_refused_with_the_real_reason(self, tmp_path) -> None:
        from src.collector import _resolve_run
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(FETCHED)
            store.finish_run(run, finished_at=FETCHED, report=ValidationReport())
            resolved, note = _resolve_run(store, None, what="to analyse")
        assert resolved is None
        assert "stored no prices" in note

    def test_a_run_that_stored_rows_is_still_read(self, tmp_path) -> None:
        from src.collector import _resolve_run
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes(run, [make_quote()])
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=1),
            )
            resolved, _ = _resolve_run(store, None, what="to analyse")
        assert resolved == run


class TestAnArgumentThatCannotBeHonouredIsRefused:
    """``_positive`` compared with ``<=``, and every comparison is False for
    NaN — so ``--stake nan`` and ``--stake inf`` walked past it, past the
    one-unit floor, and aborted inside the detector's ``int()``.

    That is verbatim the failure the type's own docstring exists to remove: a
    traceback on a slate that had a position, silence on one that did not.
    ``--interval`` and ``--max-runs`` were bare ``int`` and took values the
    watch loop cannot honour.
    """

    @pytest.mark.parametrize("text", ["nan", "inf", "-inf", "Infinity", "1e400"])
    def test_a_non_finite_stake_is_refused(self, text) -> None:
        import argparse

        from src.collector import _positive

        with pytest.raises(argparse.ArgumentTypeError, match="finite"):
            _positive("stake")(text)

    @pytest.mark.parametrize("text", ["-5", "0"])
    def test_an_unusable_interval_is_refused(self, text) -> None:
        import argparse

        from src.collector import _positive

        with pytest.raises(argparse.ArgumentTypeError):
            _positive("interval")(text)

    def test_a_negative_run_count_is_refused_but_zero_is_not(self) -> None:
        """``--max-runs 0`` means unlimited, so a negative read as unlimited and
        then delivered exactly one pass in silence."""
        import argparse

        from src.collector import _non_negative

        with pytest.raises(argparse.ArgumentTypeError, match="negative"):
            _non_negative()("-1")
        assert _non_negative()("0") == 0
        assert _non_negative()("3") == 3

    def test_the_ordinary_values_still_pass(self) -> None:
        from src.collector import _positive

        assert _positive("stake")("100") == 100.0
        assert _positive("interval")("300") == 300.0


# ── round twenty ─────────────────────────────────────────────────────────────


class TestBlockingOneLegRevealsTheNext:
    """The blocked-leg search computed its candidates **once**, from the legs of
    the unblocked assignment.

    When a blocked leg's replacement is *itself* a size-stating venue, that
    replacement was never blocked in turn, so the third-choice counterparty was
    unreachable — and the subset-minus-a-venue candidates could not rescue it,
    because their unblocked assignment reproduced a fingerprint already seen and
    their own blocked combinations were then skipped.

    26% of cross-source groups on the captured slate have two or more
    limit-stating sources on one selection; Pinnacle states a size on every one
    of its quotes.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, limit=None, **kwargs):
        row = make_quote(source=source, selection=selection, decimal_odds=odds,
                         source_market_id="m", **kwargs)
        return row if limit is None else row.model_copy(update={"limit_amount": limit})

    def _report(self, rows, **kwargs):
        from src.arb import find_opportunities

        return find_opportunities(rows, as_of=self.AS_OF, commissions={}, **kwargs)

    @pytest.mark.parametrize("second_limit", [0.60, 1.00, 5.00])
    def test_two_limited_venues_on_one_selection(self, second_limit) -> None:
        """Refused outright at 0.60, and published as +0.10 on a bankroll of 2
        at 1.00 — while +2.90 on 100 sat in the same rows."""
        rows = [
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("matchbook", Selection.AWAY, 2.20, 0.60),
            self._q("smarkets", Selection.AWAY, 2.15, second_limit),
            self._q("bovada", Selection.AWAY, 2.05),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        opportunity = report.opportunities[0]
        assert opportunity.guaranteed_profit > 2.0, opportunity.describe()
        assert {l.source for l in opportunity.legs} == {"pinnacle", "bovada"}

    def test_a_cheaper_unlimited_leg_can_raise_the_cap(self) -> None:
        """``max_total_stake`` is ``min(limit · S · d)``, so it rises when the
        *other* leg's price falls — the profit-maximising assignment can use a
        worse price on an unlimited leg, which a search that only moves
        size-stating legs never reaches."""
        rows = [
            self._q("sxbet", Selection.HOME, 1.944, 21.08),
            self._q("fanduel", Selection.AWAY, 2.184),
            self._q("pinnacle", Selection.AWAY, 2.134),
        ]
        report = self._report(rows)
        assert len(report.opportunities) == 1
        opportunity = report.opportunities[0]
        # Pinned as a *property*, not as a pairing.  When this was written the
        # answer was sxbet/pinnacle at +0.55 on 40 — pinnacle's cheaper unlimited
        # away leg raised the cap, which is the mechanism the search has to reach.
        # Once the position stopped having to spend the whole bankroll,
        # sxbet/fanduel at +0.94 on 34 overtook it.  Both are reached by the same
        # search; what has to hold is that the best of them is the one published.
        best = max(
            self._report([row for row in rows if row.source in pair]).opportunities[0]
            .guaranteed_profit
            for pair in ({"sxbet", "pinnacle"}, {"sxbet", "fanduel"})
        )
        assert opportunity.guaranteed_profit == pytest.approx(best, abs=1e-9)
        assert opportunity.guaranteed_profit > 0.9, opportunity.describe()

    def test_adding_a_venue_never_makes_the_answer_worse(self) -> None:
        """The invariant behind both: a market's reported profit must not fall
        when a source is added to it."""
        base = [
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.05),
        ]
        extras = [
            self._q("matchbook", Selection.AWAY, 2.20, 0.60),
            self._q("smarkets", Selection.AWAY, 2.15, 0.60),
        ]
        alone = max(
            (o.guaranteed_profit for o in self._report(base).opportunities), default=0.0
        )
        with_more = max(
            (o.guaranteed_profit for o in self._report(base + extras).opportunities),
            default=0.0,
        )
        assert with_more >= alone - 1e-9, (alone, with_more)

    def test_the_search_is_still_cheap(self) -> None:
        import random
        import time

        random.seed(5)
        rows = []
        for index in range(20):
            for selection in (Selection.HOME, Selection.AWAY):
                rows.append(self._q(
                    f"s{index}", selection, round(random.uniform(1.8, 2.3), 3),
                    round(random.uniform(2, 500), 2),
                    observed_at=FETCHED + timedelta(seconds=index * 20),
                ))
        started = time.perf_counter()
        for _ in range(10):
            self._report(rows)
        assert (time.perf_counter() - started) / 10 < 0.20


class TestAnUnusedStalePriceDoesNotHideItsSource:
    """The observation window required **every** one of a source's best quotes
    to be inside it, so a stale price the assignment never uses removed the
    source from every window.

    Widening the generator is safe because the spread of the *chosen* legs is
    checked on every candidate anyway; the window only decides what is worth
    trying.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    T = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, seconds=0):
        return make_quote(source=source, selection=selection, decimal_odds=odds,
                          observed_at=self.T + timedelta(seconds=seconds),
                          source_market_id="m")

    def _report(self, rows):
        from src.arb import find_opportunities

        return find_opportunities(rows, as_of=self.AS_OF, commissions={})

    def test_the_simultaneous_legs_are_still_paired(self) -> None:
        report = self._report([
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("pinnacle", Selection.AWAY, 1.50, 600),  # never used
            self._q("bovada", Selection.AWAY, 2.05),
        ])
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        assert {l.source for l in report.opportunities[0].legs} == {"pinnacle", "bovada"}

    def test_legs_that_actually_straddle_are_still_refused(self) -> None:
        report = self._report([
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.05, 600),
        ])
        assert report.opportunities == []
        assert "stale_leg" in [d.code for d in report.diagnostics]


class TestTheTruncationCaveatCoversEveryClaimBesideIt:
    """Round 19 qualified "impossible prices" for a truncated run and left the
    row directly beneath it — "crossed order books: none" — making the same
    positive claim about rows the page never loaded."""

    def test_both_claims_are_qualified(self) -> None:
        import pathlib

        text = pathlib.Path("src/report_assets.py").read_text()
        block = text[text.index("['impossible prices'"): text.index("['re-read from disk'")]
        assert block.count("partial ?") == 2, block


class TestTheAssignmentSearchIsExhaustiveNotBounded:
    """The assignment search was a walk that blocked chosen legs one at a time,
    bounded by a depth constant, and the bound truncated.

    On limit-heavy slates, raising it from 3 to 4 changed the published profit on
    1 trial in 8,000, and from 2 to 4 on 13 — the market still published, just for
    less money, with nothing on the page to say the search had stopped looking.
    Neither the suite nor the differential fuzz could tell 3 from 4, so the bound
    was untestable as well as wrong.

    The search now scores every assignment, so there is no depth to pin.  What is
    pinned instead is the property the bound was standing in for: the published
    profit is the best any assignment of these rows can pay.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, limit=None):
        row = make_quote(source=source, selection=selection, decimal_odds=odds,
                         source_market_id="m")
        return row if limit is None else row.model_copy(update={"limit_amount": limit})

    def _best(self, rows):
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={})
        return max((o.guaranteed_profit for o in report.opportunities), default=0.0)

    def _brute_force(self, rows):
        """The best profit reachable by *any* one-source-per-selection pairing,
        found by asking the engine about each pairing on its own."""
        import itertools

        by_selection = {}
        for row in rows:
            by_selection.setdefault(row.selection, []).append(row)
        best = 0.0
        for pairing in itertools.product(*by_selection.values()):
            if len({leg.source for leg in pairing}) < 2:
                continue
            best = max(best, self._best(list(pairing)))
        return best

    def test_three_limited_venues_competing_for_one_selection(self) -> None:
        """Refused outright — ``cannot_be_placed_at_the_stated_limits`` — while
        +2.90 on a bankroll of 100 sat in the same rows.  Two competing venues
        were reachable one leg deep; three were not."""
        rows = [
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("matchbook", Selection.AWAY, 2.30, 0.60),
            self._q("smarkets", Selection.AWAY, 2.20, 0.60),
            self._q("sxbet", Selection.AWAY, 2.15, 0.60),
            self._q("bovada", Selection.AWAY, 2.05),
        ]
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={})
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        assert report.opportunities[0].guaranteed_profit > 2.5
        assert {l.source for l in report.opportunities[0].legs} == {"pinnacle", "bovada"}

    def test_the_published_profit_is_the_best_any_pairing_can_pay(self) -> None:
        """Seven venues a side, nearly all stating a size.  The bounded walk
        published +0.35; the best pairing in these rows pays +0.41."""
        rows = [
            self._q("betrivers_kambi", Selection.AWAY, 3.797, 1.92),
            self._q("fanduel", Selection.AWAY, 3.606, 6.89),
            self._q("leovegas_kambi", Selection.AWAY, 2.517, 1.22),
            self._q("matchbook", Selection.AWAY, 2.499, 2.77),
            self._q("pinnacle", Selection.AWAY, 2.45),
            self._q("polymarket", Selection.AWAY, 2.378, 0.49),
            self._q("sxbet", Selection.AWAY, 2.712),
            self._q("betrivers_kambi", Selection.HOME, 1.291, 1.78),
            self._q("bovada", Selection.HOME, 1.427, 19.89),
            self._q("fanduel", Selection.HOME, 1.367, 2.8),
            self._q("leovegas_kambi", Selection.HOME, 1.596, 1.21),
            self._q("matchbook", Selection.HOME, 1.6, 4.96),
            self._q("pinnacle", Selection.HOME, 1.584, 2.57),
            self._q("polymarket", Selection.HOME, 1.763, 0.24),
            self._q("sxbet", Selection.HOME, 1.583, 0.54),
        ]
        published = self._best(rows)
        assert published == pytest.approx(self._brute_force(rows), abs=1e-9)
        assert published > 0.40, published


class TestARunRecordsTheRowsItStoredNotTheRowsItValidated:
    """``collect_once`` threw away ``save_quotes_by_source``'s stored count and
    ``finish_run`` wrote ``report.quote_count`` — the count that survived
    *validation*.

    Those are the same number only while every insert succeeds, and isolating
    inserts per source exists precisely because they do not.  With every insert
    failing, the run recorded ``quotes 7372 events 414`` against an empty table
    and ``arb``, ``show`` and ``lines`` each printed their zero-row sentence and
    exited 0 — which is verbatim what :func:`_resolve_run`'s guard was added to
    stop being mistaken for a thin slate.  The guard reads the recorded count, so
    the lie disarmed the check meant to catch it.
    """

    def _store(self, tmp_path):
        from src.store import Store

        return Store(tmp_path / "db.sqlite3")

    def test_a_run_whose_inserts_all_failed_is_refused(self, tmp_path) -> None:
        import sqlite3

        from src.collector import _resolve_run
        from src.validation import ValidationReport

        with self._store(tmp_path) as store:
            run = store.start_run(datetime.now(UTC))
            store._conn.execute(
                "CREATE TRIGGER no_inserts BEFORE INSERT ON quote "
                "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
            )
            stored, failures = store.save_quotes_by_source(run, [make_quote()])
            assert stored == 0 and failures
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                # what validation counted, which is what used to be recorded
                report=ValidationReport(quote_count=7372, event_count=414),
            )
            row = store._conn.execute(
                "SELECT quote_count, event_count FROM collection_run WHERE id = ?", (run,)
            ).fetchone()
            assert (row["quote_count"], row["event_count"]) == (0, 0)
            resolved, note = _resolve_run(store, None, what="to analyse")
        assert resolved is None
        assert "stored no prices" in note
        assert isinstance(sqlite3.Error, type)

    def test_a_partial_failure_records_only_what_landed(self, tmp_path) -> None:
        """One source's insert failing left the run claiming every source's rows."""
        from src.validation import ValidationReport

        rows = [make_quote(source="pinnacle", event_key="A", source_market_id="m"),
                make_quote(source="bovada", event_key="A", source_market_id="m")]
        with self._store(tmp_path) as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, rows[:1])
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
            )
            recorded = store._conn.execute(
                "SELECT quote_count FROM collection_run WHERE id = ?", (run,)
            ).fetchone()["quote_count"]
            assert recorded == 1, "recorded what validation saw, not what was stored"
            assert store.stored_quote_counts(run) == {"pinnacle": 1}

    def test_the_runs_listing_names_rows_that_never_reached_the_table(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        """``runs`` printed ``pinnacle  ok  2346 quotes`` for a source with
        nothing in the table, because ``source_health`` counts what the adapter
        produced rather than what was stored."""
        import argparse
        import contextlib

        from src import collector
        from src.sources.base import SourceHealth
        from src.validation import Severity, ValidationReport

        report = ValidationReport()
        report.add(Severity.ERROR, "quotes_not_persisted",
                   "OperationalError: disk I/O error", source="pinnacle")
        with self._store(tmp_path) as store:
            run = store.start_run(datetime.now(UTC))
            store.save_health(run, SourceHealth(
                source_key="pinnacle", ok=True, checked_at=datetime.now(UTC),
                quote_count=2346, event_count=100,
            ))
            store.save_findings(run, report.findings)
            store.finish_run(run, finished_at=datetime.now(UTC), report=report)
            monkeypatch.setattr(
                collector, "_open_store", lambda: contextlib.nullcontext(store)
            )
            collector._cmd_runs(argparse.Namespace(limit=5, sport=None, league=None))
        printed = capsys.readouterr().out
        assert "2346 quotes" in printed
        assert "0 of those 2346 rows are in the database" in printed

    def test_rows_the_scope_filter_dropped_are_not_called_a_lost_write(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        """``source_health`` counts what the adapter produced, *before*
        ``--sport``/``--league`` drops what was out of scope.  Reading the
        difference as a storage fault had ``collect --league EPL`` accusing the
        database of losing 351 FanDuel rows and naming an error the run does not
        contain, two lines above the run's own note saying they were excluded by
        filter."""
        import argparse
        import contextlib

        from src import collector
        from src.sources.base import SourceHealth
        from src.validation import ValidationReport

        with self._store(tmp_path) as store:
            run = store.start_run(datetime.now(UTC))
            store.save_health(run, SourceHealth(
                source_key="fanduel", ok=True, checked_at=datetime.now(UTC),
                quote_count=393, event_count=127,
            ))
            store.save_quotes_by_source(run, [make_quote(source="fanduel")])
            store.finish_run(run, finished_at=datetime.now(UTC), report=ValidationReport())
            monkeypatch.setattr(
                collector, "_open_store", lambda: contextlib.nullcontext(store)
            )
            collector._cmd_runs(argparse.Namespace(limit=5, sport=None, league=None))
        printed = capsys.readouterr().out
        assert "393 quotes" in printed
        assert "not stored" not in printed
        assert "quotes_not_persisted" not in printed


class TestAMarginBarBelowZeroIsRefused:
    """``--min-margin`` was a bare ``float`` where ``--stake`` and ``--limit``
    use a validated type.

    A negative bar admits markets with no edge at all, and the detector then
    files them under ``rounding_destroys_edge`` — "margin -3.54% is too thin for
    a 1-unit stake increment", 110 markets on the captured slate, when rounding
    destroyed nothing and there was nothing there to round.  No position is
    reported either way, so what is wrong is the accusation, not the money.
    """

    @pytest.mark.parametrize("text", ["-50", "-0.001", "nan", "-inf", "1e400"])
    def test_it_is_refused_up_front(self, text) -> None:
        import argparse

        from src.collector import _non_negative

        with pytest.raises(argparse.ArgumentTypeError):
            _non_negative(whole=False)(text)

    @pytest.mark.parametrize("text,expected", [("0", 0.0), ("2.5", 2.5), ("1e-9", 1e-9)])
    def test_a_bar_that_can_be_honoured_is_kept(self, text, expected) -> None:
        from src.collector import _non_negative

        assert _non_negative(whole=False)(text) == expected

    def test_the_argument_is_actually_wired_to_it(self, capsys) -> None:
        """The mechanism was already there for ``--stake``; what was missing was
        this flag using it."""
        from src.collector import main

        with pytest.raises(SystemExit) as exit_code:
            main(["arb", "--min-margin", "-50"])
        assert exit_code.value.code == 2
        assert "cannot be negative" in capsys.readouterr().err


class TestTheGatesOnEachAssignmentAreLoadBearing:
    """Replacing the subset search with exhaustive enumeration moved two checks
    from "restricts what is searched" to "the only thing standing between a
    non-position and the page", and the suite pinned neither: removing the
    counterparty check and loosening the margin floor each left all 2,120 tests
    passing.  Both are reachable, and both publish something that is not real.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, limit=None):
        row = make_quote(source=source, selection=selection, decimal_odds=odds,
                         source_market_id="m")
        return row if limit is None else row.model_copy(update={"limit_amount": limit})

    def test_two_keys_of_one_book_are_not_two_books(self) -> None:
        """The Kambi keys price themselves sanely, so
        ``source_prices_itself_to_lose`` never fires — the gap between them is one
        book against itself, and once the group is being examined at all only the
        counterparty check can see it.

        Reaching that check needs a group that a *genuine* cross-counterparty edge
        has already qualified, because a group whose best distinct assignment has
        no edge is dropped before any assignment is enumerated.  With the check
        removed, the mirrored pair outranks the real position and the engine
        publishes ``margin 23.08%, guaranteed +30.00`` on a position nobody can
        take, in place of the honest +14.40 beside it.
        """
        from src.arb import EVERY_LEAGUE

        mirrors = {EVERY_LEAGUE: [frozenset({"betrivers_kambi", "leovegas_kambi"})]}
        rows = [
            self._q("pinnacle", Selection.HOME, 2.05),
            self._q("pinnacle", Selection.AWAY, 1.80),
            self._q("bovada", Selection.HOME, 1.80),
            self._q("bovada", Selection.AWAY, 2.05),
            self._q("betrivers_kambi", Selection.HOME, 2.60),
            self._q("betrivers_kambi", Selection.AWAY, 1.50),
            self._q("leovegas_kambi", Selection.HOME, 1.50),
            self._q("leovegas_kambi", Selection.AWAY, 2.60),
        ]
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={},
                                    one_counterparty=mirrors)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        legs = {l.source for l in report.opportunities[0].legs}
        assert legs != {"betrivers_kambi", "leovegas_kambi"}, "one book against itself"
        assert len({mirrors[EVERY_LEAGUE][0] if s in mirrors[EVERY_LEAGUE][0] else s
                    for s in legs}) == 2

        # Without the mapping the same rows really are two books, and the bigger
        # edge is the right answer.
        unmapped = find_opportunities(rows, as_of=self.AS_OF, commissions={})
        assert {l.source for l in unmapped.opportunities[0].legs} == {
            "betrivers_kambi", "leovegas_kambi"
        }

    def test_a_margin_exactly_at_the_bar_does_not_clear_it(self) -> None:
        """``--min-margin`` is a bar the edge has to *clear*, which is the
        convention every retry in this module already uses.  The check only
        becomes reachable when the better-priced assignment is refused for its
        stated size and the tail falls through to this one, so nothing was
        exercising it: loosening the comparison published ``margin 2.00%,
        guaranteed +1.23`` on a market with exactly the asked-for edge and no
        more."""
        bar = 0.02
        away = 1.0 / (1.0 - bar - 1.0 / 2.10)
        rows = [
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("matchbook", Selection.AWAY, 2.40, 0.01),  # better, unplaceable
            self._q("bovada", Selection.AWAY, round(away, 10)),
        ]
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={}, min_margin=bar)
        assert report.opportunities == []
        assert "cannot_be_placed_at_the_stated_limits" in [
            d.code for d in report.diagnostics
        ]

    def test_a_margin_above_the_bar_still_clears_it(self) -> None:
        """The other side of the same boundary, so the fix cannot be "reject
        everything"."""
        bar = 0.02
        away = 1.0 / (1.0 - bar - 1.0 / 2.10)
        rows = [
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, round(away, 10) + 0.01),
        ]
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={}, min_margin=bar)
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        assert report.opportunities[0].margin > bar


class TestTheBankrollIsACeilingNotAQuota:
    """``stake_candidates`` handed the leftover units out one per leg, so the
    pure-floors allocation was unreachable whenever anything was left over.

    The spare unit lands on the leg that is *not* setting the floor, where it
    buys nothing and is subtracted from every outcome.  On an ordinary two-book
    moneyline — 1.88 against 2.35 — the engine staked 56/44 and reported ``+3.40
    on 100`` where 55/44 guarantees **+4.40**.  Worse than understating: in the
    thin band where cross-book edges actually live, hundreds of genuinely
    risk-free positions were refused outright as ``rounding_destroys_edge``,
    because every split that spent the whole bankroll was underwater and the one
    that did not was never offered.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds):
        return make_quote(source=source, selection=selection, decimal_odds=odds,
                          source_market_id="m")

    def _published(self, home_odds, away_odds, total_stake, increment=1.0):
        report = find_opportunities(
            [self._q("pinnacle", Selection.HOME, home_odds),
             self._q("fanduel", Selection.AWAY, away_odds)],
            total_stake=total_stake, stake_increment=increment,
            as_of=self.AS_OF, commissions={},
        )
        return report.opportunities[0].guaranteed_profit if report.opportunities else None

    def _exhaustive(self, home_odds, away_odds, total_stake, increment=1.0):
        """Every whole-unit allocation of at most the bankroll, scored directly."""
        units = int(total_stake / increment + 1e-9)
        return max(
            (min(home * increment * home_odds, away * increment * away_odds)
             - (home + away) * increment
             for home in range(1, units + 1)
             for away in range(1, units - home + 1)),
            default=0.0,
        )

    def test_the_measured_case(self) -> None:
        assert self._published(1.88, 2.35, 100.0) == pytest.approx(4.40, abs=0.01)

    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_the_published_profit_is_the_best_whole_unit_split(self, seed) -> None:
        """Differential against the whole allocation space, over the realistic
        band: a self-consistent edge under ``REFUSE_MARGIN``, which is where the
        engine is meant to be exact."""
        import random

        rng = random.Random(seed)
        checked = 0
        for _ in range(400):
            home = round(rng.uniform(1.30, 4.00), 3)
            away = round(rng.uniform(1.30, 4.00), 3)
            if not (0.95 <= 1.0 / home + 1.0 / away < 1.0):
                continue
            best = self._exhaustive(home, away, 100.0)
            if best <= 1e-9:
                continue
            checked += 1
            got = self._published(home, away, 100.0)
            assert got is not None, f"refused a real edge at {home}/{away}"
            assert got == pytest.approx(best, abs=1e-9), f"{home}/{away}: {got} vs {best}"
        assert checked > 20, f"only {checked} arb-bearing pairs — the band is wrong"

    @pytest.mark.parametrize("seed", [11, 12])
    def test_the_allocator_itself_reaches_the_optimum(self, seed) -> None:
        """Straight at ``stake_candidates``, which is a pure function, so this
        can cover three-way shapes and coarse increments without having to build
        a contract every book agrees on.

        A non-binding leg takes the *fewest* units that clear the guarantee — a
        ceiling.  Flooring it instead still produces allocations, just worse ones,
        and the caller picks the best of what it is given: 128 of 1,063 arb-bearing
        slates then fall short by up to 6.00.
        """
        import random

        from src.arb import stake_candidates, stake_split

        rng = random.Random(seed)
        checked = 0
        for _ in range(3000):
            count = rng.choice([2, 2, 3])
            odds = [round(rng.uniform(1.25, 8.0), 3) for _ in range(count)]
            if not (0.90 <= sum(1.0 / o for o in odds) < 1.0):
                continue
            total = rng.choice([100.0, 50.0, 200.0, 25.0])
            increment = rng.choice([1.0, 1.0, 5.0])
            units = int(total / increment + 1e-9)
            if count == 2:
                best = max(
                    (min(a * increment * odds[0], b * increment * odds[1])
                     - (a + b) * increment
                     for a in range(1, units + 1) for b in range(1, units - a + 1)),
                    default=0.0,
                )
            else:
                best = max(
                    (min(a * increment * odds[0], b * increment * odds[1],
                         c * increment * odds[2]) - (a + b + c) * increment
                     for a in range(1, units + 1) for b in range(1, units - a + 1)
                     for c in range(1, units - a - b + 1)),
                    default=0.0,
                )
            if best <= 1e-9:
                continue
            checked += 1
            offered = max(
                (min(stake * odd for stake, odd in zip(allocation, odds)) - sum(allocation)
                 for allocation in stake_candidates(
                     stake_split(odds, total), total, increment)),
                default=0.0,
            )
            assert offered == pytest.approx(best, abs=1e-6), (
                f"odds {odds} bankroll {total} increment {increment}: "
                f"offered {offered}, optimum {best}"
            )
        assert checked > 50, f"only {checked} arb-bearing slates"

    def test_a_position_never_exceeds_the_bankroll(self) -> None:
        """The other direction: leaving the bankroll unspent is allowed, going
        over it never is."""
        import random

        rng = random.Random(7)
        for _ in range(200):
            home = round(rng.uniform(1.30, 4.00), 3)
            away = round(rng.uniform(1.30, 4.00), 3)
            for total, increment in ((100.0, 1.0), (100.0, 6.0), (25.0, 5.0)):
                report = find_opportunities(
                    [self._q("pinnacle", Selection.HOME, home),
                     self._q("fanduel", Selection.AWAY, away)],
                    total_stake=total, stake_increment=increment,
                    as_of=self.AS_OF, commissions={},
                )
                for opportunity in report.opportunities:
                    assert opportunity.total_stake <= total + 1e-9
                    for leg in opportunity.legs:
                        assert leg.stake % increment == pytest.approx(0.0, abs=1e-9)


class TestTheSplitAimsAtTheFloorNotAtEqualReturns:
    """``stake_split`` equalises the two *full-win* returns, and the search for a
    whole-unit allocation explores along whatever ray it is handed.

    On a quarter (Asian split) line that ray is the wrong one.  Half the stake
    rides each neighbouring line, so the middle outcome pays half of the
    surviving side's profit and the floor is ``min(win_a, win_b, half)`` — a
    different function with a different maximum.  The optimum was therefore out
    of reach before any rounding happened: soccer total 2.75 priced 2.10 against
    2.10 reported ``+2.50 on 100`` where the same two prices guarantee **+3.00**.
    200 of the captured slate's lines are quarter lines.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    COMMENCE = datetime(2026, 7, 28, 22, 0, tzinfo=UTC)

    def _rows(self, market, line, first_odds, second_odds):
        selections = ((Selection.OVER, Selection.UNDER) if market is Market.TOTAL
                      else (Selection.HOME, Selection.AWAY))
        return [
            make_quote(
                source=source, selection=selection, decimal_odds=odds,
                source_market_id="m", sport=Sport.SOCCER, league="EPL", market=market,
                period=Period.FULL_GAME,
                line=line if market is Market.TOTAL else sign * line,
                event_key="SOCCER-a@SOCCER-b:2026-07-28",
                home_participant="SOCCER-b", away_participant="SOCCER-a",
                commence_time=self.COMMENCE,
            )
            for source, selection, odds, sign in (
                ("pinnacle", selections[0], first_odds, 1),
                ("fanduel", selections[1], second_odds, -1),
            )
        ]

    def _exhaustive(self, rows, total_stake):
        """Every whole-unit split, scored through the module's own settlement
        model — so this checks the *choice*, not the arithmetic."""
        from src.arb import (LOSE, _return_multiplier, contract_shape,
                             settlement_outcomes)

        shape = contract_shape(rows[0].sport, rows[0].market, rows[0].period, rows)
        outcomes = settlement_outcomes(rows[0].sport, rows[0].market, rows[0].period,
                                       rows[0].line, shape)
        odds = [row.decimal_odds for row in rows]
        multipliers = [
            [_return_multiplier(results.get(row.selection, LOSE), odd)
             for row, odd in zip(rows, odds)]
            for _, results in outcomes
        ]
        units = int(total_stake)
        return max(
            (min(first * row[0] + second * row[1] for row in multipliers) - first - second
             for first in range(1, units + 1) for second in range(1, units - first + 1)),
            default=0.0,
        )

    def test_the_measured_quarter_total(self) -> None:
        rows = self._rows(Market.TOTAL, 2.75, 2.10, 2.10)
        report = find_opportunities(rows, total_stake=100.0, as_of=self.AS_OF,
                                    commissions={})
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        opportunity = report.opportunities[0]
        assert opportunity.guaranteed_profit == pytest.approx(3.00, abs=1e-9)
        assert opportunity.total_stake == 99.0

    @pytest.mark.parametrize("seed", [23, 24])
    def test_every_line_granularity_reaches_its_own_optimum(self, seed) -> None:
        """Quarter, half and whole lines together, on both totals and spreads:
        the fix must not aim the half- and whole-line cases somewhere new."""
        import random

        rng = random.Random(seed)
        checked = 0
        for _ in range(600):
            market = rng.choice([Market.TOTAL, Market.SPREAD])
            line = rng.choice([2.25, 2.75, 3.25, 1.25, 0.25, 4.75, 2.5, 3.0, 1.5])
            if market is Market.TOTAL and line <= 0:
                continue
            first = round(rng.uniform(1.6, 3.2), 3)
            second = round(rng.uniform(1.6, 3.2), 3)
            if not (0.93 <= 1.0 / first + 1.0 / second < 1.0):
                continue
            rows = self._rows(market, line, first, second)
            best = self._exhaustive(rows, 100.0)
            if best <= 1e-9:
                continue
            checked += 1
            report = find_opportunities(rows, total_stake=100.0, as_of=self.AS_OF,
                                        commissions={})
            assert report.opportunities, (
                f"refused a real edge: {market.value} @ {line} {first}/{second}"
            )
            assert report.opportunities[0].guaranteed_profit == pytest.approx(
                best, abs=1e-6
            ), f"{market.value} @ {line} {first}/{second}"
        assert checked > 20, f"only {checked} arb-bearing lines"


class TestAScopedHealthCheckStillSeesATotalOutage:
    """``run_summaries`` keeps only runs holding a row in scope, which deletes
    from ``health`` exactly the runs it exists to surface.

    Measured on five runs with two total outages: ``health`` read 60% and exited
    1; ``health --sport baseball`` read 100% and exited 0 on the same database,
    with the outage absent from the rate, from the ``.``/``X`` strip, from any
    note, and from the exit code an unattended watch reads.
    """

    def _database(self, tmp_path, *, outages):
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        store = Store(tmp_path / "db.sqlite3")
        for index in range(2 + outages):
            failed = index >= 2
            run = store.start_run(datetime.now(UTC))
            store.save_health(run, SourceHealth(
                source_key="pinnacle", ok=not failed, checked_at=datetime.now(UTC),
                quote_count=0 if failed else 1, event_count=0 if failed else 1,
                error_kind="blocked" if failed else None,
            ))
            if not failed:
                store.save_quotes_by_source(run, [make_quote(source="pinnacle")])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport())
        return store

    def _health(self, store, monkeypatch, capsys, **scope):
        import argparse
        import contextlib

        from src import collector

        monkeypatch.setattr(collector, "_open_store",
                            lambda: contextlib.nullcontext(store))
        args = argparse.Namespace(
            limit=10, min_rate=0.8,
            sport=scope.get("sport"), league=scope.get("league"),
        )
        code = collector._cmd_health(args)
        return code, capsys.readouterr().out

    def test_the_outage_survives_a_sport_filter(self, tmp_path, monkeypatch, capsys) -> None:
        """Health is a property of a source, not of a sport, so the scoped view
        must show the *same rate* the unscoped one does.

        The first repair kept the scoped rate at 100% and printed a flag beside
        it — and the flag's sentence was "a source is below the success
        threshold", directly over a table showing every rate at 100%.  Now the
        rates themselves are computed over every run, the threshold sentence
        names the failing source, and no sentence can contradict the table."""
        with self._database(tmp_path, outages=2) as store:
            unscoped_code, unscoped = self._health(store, monkeypatch, capsys)
            scoped_code, scoped = self._health(store, monkeypatch, capsys,
                                               sport=["baseball"])
        assert unscoped_code == 1, unscoped
        # The scoped view used to read 100% and exit 0 on this same database.
        assert scoped_code == 1, scoped
        def rate_line(printed):
            return next(line for line in printed.splitlines()
                        if line.startswith("pinnacle"))
        assert rate_line(scoped) == rate_line(unscoped)
        assert "50%" in rate_line(scoped)
        assert "pinnacle below the 80% success threshold" in scoped
        assert "a source is below" not in scoped

    def test_a_failure_on_an_out_of_scope_run_still_counts(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The subtler hole: a source fails on a run that stored *another*
        sport's rows.  The run is out of scope but the failure is the source's,
        and the scoped rate was reading 100% over it."""
        from src.schema import Sport
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            for failed in (False, False, True):
                run = store.start_run(datetime.now(UTC))
                store.save_health(run, SourceHealth(
                    source_key="pinnacle", ok=not failed,
                    checked_at=datetime.now(UTC),
                    quote_count=1, event_count=1,
                    error_kind="blocked" if failed else None,
                ))
                # the failing run still stores a row — for a different sport
                store.save_quotes_by_source(run, [make_quote(
                    source="pinnacle",
                    **({"sport": Sport.HOCKEY, "league": "NHL",
                        "event_key": "NHL-a@NHL-b:2026-07-28",
                        "home_participant": "NHL-b", "away_participant": "NHL-a"}
                       if failed else {}),
                )])
                store.finish_run(run, finished_at=datetime.now(UTC),
                                 report=ValidationReport(quote_count=1, event_count=1))
            code, printed = self._health(store, monkeypatch, capsys,
                                         sport=["baseball"])
        assert code == 1, printed
        assert "67%" in printed
        assert "pinnacle below the 80% success threshold" in printed

    def test_a_clean_database_is_still_clean_under_a_filter(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The fix must not invent an outage where there is none."""
        with self._database(tmp_path, outages=0) as store:
            code, printed = self._health(store, monkeypatch, capsys, sport=["baseball"])
        assert code == 0, printed
        assert "stored no prices at all" not in printed


class TestARateIsAProportionNotAPercentage:
    """``--min-rate`` was a bare ``float`` while ``--stake`` and ``--min-margin``
    are validated.

    ``nan`` made the threshold unreachable, so the command exited **0** on a
    database it exits 1 on by default — a silent all-clear from the check an
    unattended watch reads.  ``80``, which the all-percentages output invites,
    printed "below the 8000% success threshold" and failed every source.
    """

    @pytest.mark.parametrize("text", ["nan", "1e400", "80", "-0.1", "1.5", "inf"])
    def test_a_rate_that_cannot_be_honoured_is_refused(self, text) -> None:
        import argparse

        from src.collector import _a_rate

        with pytest.raises(argparse.ArgumentTypeError):
            _a_rate(text)

    @pytest.mark.parametrize("text,expected", [("0", 0.0), ("0.8", 0.8), ("1", 1.0)])
    def test_a_real_proportion_is_kept(self, text, expected) -> None:
        from src.collector import _a_rate

        assert _a_rate(text) == expected

    def test_the_argument_is_wired_to_it(self, capsys) -> None:
        from src.collector import main

        with pytest.raises(SystemExit) as exit_code:
            main(["health", "--min-rate", "nan"])
        assert exit_code.value.code == 2
        assert "finite" in capsys.readouterr().err


class TestThePageDoesNotClaimNothingHappenedInTablesItNeverLoaded:
    """Skips, rejections, raw responses and findings are fetched only for the
    runs whose prices are embedded, while the run picker lists every run.

    For the others the page rendered "This collection saved nothing" and
    "Everything this collection saw was in scope" about tables it had never
    queried — with the true numbers printed in the flow diagram on the same
    screen.  Findings had a second way to go missing: the 500-row cap was ordered
    by ``id`` ascending, so the *oldest* runs filled it and the newest lost, and
    the page then said "Nothing was flagged in this collection" while its own
    stat strip pointed the reader at Checks.
    """

    def test_the_payload_records_which_runs_the_detail_tables_cover(self) -> None:
        text = pathlib.Path("src/report.py").read_text()
        assert '"detail_runs": list(detail_ids),' in text

    def test_the_newest_run_is_never_the_one_the_cap_cuts(self) -> None:
        text = pathlib.Path("src/report.py").read_text()
        for table in ("FROM finding", "FROM rejection"):
            block = text[text.index(table):]
            block = block[: block.index("LIMIT 500") + len("LIMIT 500")]
            assert "ORDER BY run_id DESC" in block, table

    def test_every_empty_state_over_those_tables_is_qualified(self) -> None:
        """The three empty states that make a positive claim must all be behind
        the ``detailLoaded`` guard, and the findings one behind the cap check."""
        text = pathlib.Path("src/report_assets.py").read_text()
        for claim in ("This collection saved nothing.",
                      "Everything this collection saw was in scope."):
            index = text.index(claim)
            preceding = text[max(0, index - 260): index]
            assert "detailLoaded(currentRunId)" in preceding, claim
        index = text.index("Nothing was flagged in this collection")
        assert "findingsShort" in text[max(0, index - 400): index]

    def test_render_raw_can_reach_the_run_it_names(self) -> None:
        """The qualified message quotes ``run.raw_count``; ``renderRaw`` did not
        bind ``run``, so the branch would have thrown where it was meant to
        explain."""
        text = pathlib.Path("src/report_assets.py").read_text()
        body = text[text.index("function renderRaw() {"):]
        body = body[: body.index("\n}\n")]
        assert "const run = runById.get(currentRunId);" in body
        assert "run.raw_count" in body

    def test_the_truncation_share_is_measured_before_the_sport_filter(self) -> None:
        """``partial`` compared the whole run's stored count against the
        *sport-filtered* rows, so selecting Hockey on a fully embedded run read
        "none in the 5% of this collection embedded here" — 5% being the hockey
        share, with nothing left out at all."""
        text = pathlib.Path("src/report_assets.py").read_text()
        assert "const embedded = runRows().length;" in text
        assert "const partial = stored > embedded;" in text


class TestTheAnswerDoesNotDependOnRowOrder:
    """``_best_assignment`` and ``_pick_switch`` break ties on the *index into
    ``eligible``*, and ``eligible`` was built in the order the rows arrived.

    Two venues quoting the identical net price — which mirrored tenants do by
    construction — therefore made the chosen assignment a function of row order.
    Demonstrated below at ``_best_assignment``: the same four rows give two
    different answers across their orderings.

    I could **not** reproduce a change in any *published* report from this on the
    current engine, because scoring every assignment and ranking by margin
    decides the answer before the tie-break can, and ``_best_assignment`` only
    gates the group.  That makes the sort defence in depth rather than a
    reported-bug fix — but an incidental tie-break behind a deliberate one is
    still a thing that will bite the next time the order of those two changes.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, limit=None):
        row = make_quote(source=source, selection=selection, decimal_odds=odds,
                         source_market_id="m")
        return row if limit is None else row.model_copy(update={"limit_amount": limit})

    def test_the_underlying_tie_break_really_is_positional(self) -> None:
        """The mechanism, at the function that has it."""
        import itertools

        from src.arb import _best_assignment

        rows = [
            self._q("pinnacle", Selection.HOME, 2.30),
            self._q("pinnacle", Selection.AWAY, 2.30),
            self._q("fanduel", Selection.AWAY, 2.05, 30.0),
            self._q("bovada", Selection.AWAY, 2.05),
        ]
        answers = set()
        for order in itertools.permutations(rows):
            best: dict[str, dict] = {}
            for row in order:
                best.setdefault(row.source, {})[row.selection] = row
            chosen = _best_assignment(
                needed=frozenset({Selection.HOME, Selection.AWAY}),
                eligible=list(best), best=best, require_distinct_sources=True,
                commissions={}, counterparties={},
            )
            answers.add(tuple(sorted(
                (selection.value, quote.source) for selection, quote in chosen[0].items()
            )))
        assert len(answers) == 2, "the positional tie-break no longer bites here"

        # Handed the same sources in a canonical order, it settles on one answer —
        # which is what ``_examine_group`` now does before calling it.
        settled = set()
        for order in itertools.permutations(rows):
            best = {}
            for row in order:
                best.setdefault(row.source, {})[row.selection] = row
            chosen = _best_assignment(
                needed=frozenset({Selection.HOME, Selection.AWAY}),
                eligible=sorted(best), best=best, require_distinct_sources=True,
                commissions={}, counterparties={},
            )
            settled.add(tuple(sorted(
                (selection.value, quote.source) for selection, quote in chosen[0].items()
            )))
        assert len(settled) == 1, settled

    def test_examine_group_hands_it_a_canonical_order(self) -> None:
        text = pathlib.Path("src/arb.py").read_text()
        assert "        eligible = sorted(" in text

    @pytest.mark.parametrize("seed", [77, 78])
    def test_the_published_report_is_stable_under_shuffling(self, seed) -> None:
        """Tie-heavy slates — a short price ladder so exact ties across venues are
        common — each read four times in different row orders."""
        import random

        rng = random.Random(seed)
        books = ["pinnacle", "fanduel", "bovada", "matchbook", "smarkets", "sxbet",
                 "betrivers_kambi", "leovegas_kambi"]
        ladder = [round(1.80 + 0.05 * step, 2) for step in range(4)]
        for _ in range(300):
            rows = []
            for book in rng.sample(books, rng.randint(3, 6)):
                for selection in (Selection.HOME, Selection.AWAY):
                    if rng.random() < 0.15:
                        continue
                    limit = (round(rng.uniform(1.0, 60.0), 2)
                             if rng.random() < 0.4 else None)
                    rows.append(self._q(book, selection, rng.choice(ladder), limit))
            if len(rows) < 2:
                continue
            answers = set()
            for _ in range(4):
                shuffled = rows[:]
                rng.shuffle(shuffled)
                report = find_opportunities(shuffled, as_of=self.AS_OF, commissions={})
                answers.add((
                    tuple(sorted((leg.source, leg.quote.selection.value, leg.stake)
                                 for opportunity in report.opportunities
                                 for leg in opportunity.legs)),
                    tuple(sorted((d.code, d.detail) for d in report.diagnostics)),
                ))
            assert len(answers) == 1, f"{len(answers)} different answers from one slate"


class TestARefusalNamesWhatActuallyBlockedIt:
    """``priced_together`` was set before the settlement gate, so a market whose
    only simultaneous assignment was blocked by *settlement* was refused with
    "the books that were priced together have no edge between them".

    Two books observed at the same instant carrying 3.6% is not "no edge"; what
    stopped them is that they do not void together, which the sentence never
    said — sending the operator after collection timing instead of the
    constraint that actually bound.

    The same chain ended in a third branch, ``no_reportable_position``, that
    cannot arise: when nothing survives and the named assignment settles
    consistently, the window is the only gate left, because that assignment is
    one of the ones the enumeration scored and it has already cleared the margin
    bar and the counterparty rule.
    """

    T0 = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, minutes=0):
        return make_quote(source=source, selection=selection, decimal_odds=odds,
                          source_market_id="m",
                          observed_at=self.T0 + timedelta(minutes=minutes))

    def _stale_leg(self, rows):
        report = find_opportunities(rows, as_of=self.AS_OF, commissions={})
        assert report.opportunities == []
        detail = [d.detail for d in report.diagnostics if d.code == "stale_leg"]
        assert detail, [d.code for d in report.diagnostics]
        return detail[0]

    def test_a_settlement_clash_is_not_reported_as_no_edge(self) -> None:
        """The best pair settles the same way and straddles the window; the
        simultaneous pair has a 6.93% edge and does not void together."""
        detail = self._stale_leg([
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.20, minutes=10),
            self._q("kalshi", Selection.AWAY, 2.05),
        ])
        assert "do not settle the same way" in detail
        assert "have no edge between them" not in detail

    def test_a_flat_simultaneous_pair_still_says_so(self) -> None:
        detail = self._stale_leg([
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.20, minutes=10),
            self._q("fanduel", Selection.AWAY, 1.80),
        ])
        assert "have no edge between them" in detail

    def test_nothing_simultaneous_at_all_still_says_so(self) -> None:
        detail = self._stale_leg([
            self._q("pinnacle", Selection.HOME, 2.10),
            self._q("bovada", Selection.AWAY, 2.20, minutes=10),
        ])
        assert "close enough together to replace them" in detail

    def test_the_unreachable_third_reason_is_gone(self) -> None:
        assert "no_reportable_position" not in pathlib.Path("src/arb.py").read_text()


class TestTheDashboardOnlyAccusesStorageWhenStorageFailed:
    """``build_report`` attached ``stored_count`` to every source from a bare
    produced-vs-stored comparison, while the ``runs`` listing gated the same
    number on the run's ``quotes_not_persisted`` finding.

    Health is written before the ``--sport``/``--league`` filter drops what is
    out of scope, so on any scoped collection the two counts differ for every
    source and the page said "reached the database 153 / the rest were not
    stored — see Checks" about rows the operator asked it to drop — above a
    Checks panel with no storage finding in it.
    """

    def _report_for(self, tmp_path, *, lost):
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import Severity, ValidationReport

        report = ValidationReport(quote_count=1, event_count=1)
        if lost:
            report.add(Severity.ERROR, "quotes_not_persisted",
                       "OperationalError: disk I/O error", source="fanduel")
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_health(run, SourceHealth(
                source_key="fanduel", ok=True, checked_at=datetime.now(UTC),
                quote_count=393, event_count=127,
            ))
            # one row stored of 393 produced — a scope filter, or a lost write
            store.save_quotes_by_source(run, [make_quote(source="fanduel")])
            store.save_findings(run, report.findings)
            store.finish_run(run, finished_at=datetime.now(UTC), report=report)
            return build_report(store)

    def test_a_scoped_collection_is_not_a_storage_fault(self, tmp_path) -> None:
        data = self._report_for(tmp_path, lost=False)
        health = data["runs"][0]["sources"][0]
        assert health["quote_count"] == 393
        assert "stored_count" not in health

    def test_a_lost_write_still_is(self, tmp_path) -> None:
        data = self._report_for(tmp_path, lost=True)
        health = data["runs"][0]["sources"][0]
        assert health["stored_count"] == 1


class TestTheBookPanelDoesNotAssertOverTablesItNeverLoaded:
    """The run-level empty states were qualified last round and the per-book
    panel repeats every one of them: ``book-mix``, ``book-skip-count``,
    ``book-skips`` and ``book-raws`` filter run-scoped tables that are only
    loaded for embedded runs — including the very sentence, "This book stored no
    prices in this collection.", that the run-level fix names as the bug."""

    def test_all_four_regions_are_behind_the_guard(self) -> None:
        text = pathlib.Path("src/report_assets.py").read_text()
        for claim in ("This book stored no prices in this collection.",
                      "Everything this book offered was in scope.",
                      "No pages were saved from this book in this collection.",
                      "'nothing skipped'"):
            index = text.index(claim)
            assert "detailLoaded(currentRunId)" in text[max(0, index - 400): index], claim


class TestRejectionsGetTheSameHonestyAsFindings:
    """The findings table gained newest-run-first capping and a ``findingsShort``
    guard; the rejections table got the ordering and neither guard.  With the
    cap filled by another run it said "Nothing had to be thrown away in this
    collection." about 60 recorded rejections, and a run showing 500 of 520 had
    no notice at all."""

    def test_the_empty_state_is_cap_aware(self) -> None:
        text = pathlib.Path("src/report_assets.py").read_text()
        index = text.index("'Nothing had to be thrown away in this collection.'")
        assert "rejectionsShort" in text[max(0, index - 600): index]

    def test_a_partially_shown_table_says_how_partial(self) -> None:
        text = pathlib.Path("src/report_assets.py").read_text()
        assert "rejections-note" in text
        assert "rejected rows shown" in text

    def test_the_recorded_total_comes_from_health(self) -> None:
        text = pathlib.Path("src/report_assets.py").read_text()
        assert "h.rejection_count" in text or "(h.rejection_count || 0)" in text


class TestTheChecksPanelJudgesTheRunNotTheFilter:
    """Two defects six lines apart: ``loaded`` read the *sport-filtered* rows,
    so picking a sport the row cap cut blanked the whole panel with the wrong
    remedy while the run's findings sat embedded; and ``run.ok === 0`` compared
    a JSON boolean with a number, so "recorded this run as failed" could never
    render and a failed run read "recorded it as unknown"."""

    def test_loaded_is_measured_before_the_sport_filter(self) -> None:
        text = pathlib.Path("src/report_assets.py").read_text()
        assert "const loaded = runRows().length > 0;" in text

    def test_the_verdict_compares_booleans_with_booleans(self) -> None:
        text = pathlib.Path("src/report_assets.py").read_text()
        assert "run.ok === false" in text
        assert "run.ok === 0" not in text
        # there is no third verdict for a finished run
        assert "(run.ok ? 'passing' : 'unknown')" not in text

    def test_a_zero_row_embedded_run_shows_its_findings(self) -> None:
        """A run that genuinely stored nothing has its findings embedded, so the
        panel must render them rather than blank itself behind a message whose
        remedy — a larger ``--quote-runs`` — changes nothing."""
        text = pathlib.Path("src/report_assets.py").read_text()
        assert "!loaded && !detail" in text


class TestTheReplayVerdictNamesTheRunItChecked:
    """The replay check runs once, against the newest collection's captures, and
    the PASS was rendered inside the run-scoped Checks strip — so selecting an
    older run showed a PASS for bytes the check never re-read, on a run whose
    own ``replay --run`` says FAIL."""

    def test_the_payload_carries_the_checked_run(self, tmp_path) -> None:
        from src.report import build_report
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
            data = build_report(store, replay_note="PASS", replay_run_id=run)
        assert data["meta"]["replay_run_id"] == run

    def test_the_page_scopes_the_verdict_to_that_run(self) -> None:
        """The *value* must be conditional, not just the caption — a first
        version of this test matched the conditional in the caption line and let
        a mutation reinstate the unscoped verdict."""
        text = pathlib.Path("src/report_assets.py").read_text()
        tile = text[text.index("['re-read from disk',"):]
        tile = tile[: tile.index("]")]
        value_line = tile.splitlines()[1]
        assert "currentRunId === DATA.meta.replay_run_id" in value_line, tile
        assert "DATA.meta.replay_note : '—'" in value_line, tile


class TestReportFlagsThatCannotBeHonouredAreRefused:
    """``--runs``, ``--quote-runs``, ``--max-quote-rows`` and ``--serve`` were
    bare ``int``: ``--runs 0`` printed "no finished collection runs recorded" on
    a database holding four, ``--serve 0`` was falsy and silently did not serve,
    and negatives reached SQLite as ``LIMIT -1`` — unlimited."""

    @pytest.mark.parametrize("text", ["0", "-3", "x"])
    def test_a_count_below_one_is_refused(self, text) -> None:
        import argparse

        from src.report import _a_count

        with pytest.raises(argparse.ArgumentTypeError):
            _a_count(text)

    def test_the_flags_are_wired_to_it(self, capsys) -> None:
        from src.report import main

        with pytest.raises(SystemExit) as exit_code:
            main(["--runs", "0"])
        assert exit_code.value.code == 2
        assert "at least 1" in capsys.readouterr().err


class TestTheStatedSizeCeilingCoversOffRaySplits:
    """The step-down's ceiling was ``min(limit_i · S · d_i)`` — the bound for
    stakes on the equal-return ray.  Once the allocator stopped walking that ray
    the bound stopped being one: an off-ray split can total more with every leg
    still inside its stated size, and the walk starting below it never tried it.

    Measured: pinnacle 1.423 sized 5.03 against fanduel 3.862 sized 60.94 was
    refused outright — "no whole-unit split at or below the 6 the venues state",
    a number no venue stated — while staking 5 and 2 keeps both legs inside
    their sizes and guarantees +0.115.  The exact ceiling is
    ``min(limit_i · d_i)``: risk-free means the outcome where leg *i* alone wins
    covers the whole total, so ``T <= stake_i · d_i <= limit_i · d_i``.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, limit=None):
        row = make_quote(source=source, selection=selection, decimal_odds=odds,
                         source_market_id="m")
        return row if limit is None else row.model_copy(update={"limit_amount": limit})

    def test_the_measured_refusal_is_now_a_position(self) -> None:
        report = find_opportunities(
            [self._q("pinnacle", Selection.AWAY, 1.423, 5.03),
             self._q("fanduel", Selection.HOME, 3.862, 60.94)],
            total_stake=100.0, as_of=self.AS_OF, commissions={},
        )
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]
        opportunity = report.opportunities[0]
        assert opportunity.guaranteed_profit == pytest.approx(0.115, abs=1e-6)
        assert opportunity.total_stake == 7.0
        for leg in opportunity.legs:
            assert leg.stake <= leg.quote.limit_amount + 1e-9

    @pytest.mark.parametrize("seed", [41, 42])
    def test_the_engine_matches_exhaustive_search_under_limits(self, seed) -> None:
        import random

        rng = random.Random(seed)
        checked = 0
        for _ in range(1200):
            home = round(rng.uniform(1.2, 4.5), 3)
            away = round(rng.uniform(1.2, 4.5), 3)
            if not (0.90 <= 1.0 / home + 1.0 / away < 1.0):
                continue
            home_cap = round(rng.uniform(0.3, 30.0), 2) if rng.random() < 0.8 else None
            away_cap = round(rng.uniform(0.3, 30.0), 2) if rng.random() < 0.8 else None
            if home_cap is None and away_cap is None:
                continue
            best = 0.0
            for x in range(1, 101):
                if home_cap is not None and x > home_cap + 1e-9:
                    break
                for y in range(1, 101 - x):
                    if away_cap is not None and y > away_cap + 1e-9:
                        break
                    best = max(best, min(x * home, y * away) - x - y)
            report = find_opportunities(
                [self._q("pinnacle", Selection.HOME, home, home_cap),
                 self._q("fanduel", Selection.AWAY, away, away_cap)],
                total_stake=100.0, as_of=self.AS_OF, commissions={},
            )
            got = max((o.guaranteed_profit for o in report.opportunities), default=None)
            if best <= 1e-9:
                continue
            checked += 1
            assert got is not None, f"refused {home}/{away} caps {home_cap}/{away_cap}"
            assert got == pytest.approx(best, abs=1e-6), (
                f"{home}/{away} caps {home_cap}/{away_cap}: {got} vs {best}"
            )
        assert checked > 50, checked


class TestANonDyadicIncrementCannotMintAZeroStakePosition:
    """``trial -= step`` accumulated float error for any step that is not a
    dyadic fraction: at 0.2 the walk ended on ``2.78e-17 > 0``, which reached the
    allocator as a zero-unit bankroll, whose fallback returned an all-zero split,
    whose floor of exactly 0 passed ``is_risk_free`` — and the engine published
    ``guaranteed +0.00 on 0`` with both legs at stake 0.00 *in place of* the
    refusal, on roughly 60% of the slates the refusal was for."""

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _q(self, source, selection, odds, limit=None):
        row = make_quote(source=source, selection=selection, decimal_odds=odds,
                         source_market_id="m")
        return row if limit is None else row.model_copy(update={"limit_amount": limit})

    @pytest.mark.parametrize("increment", [0.2, 0.1, 0.05, 0.25, 1.0])
    def test_the_refusal_survives_every_increment(self, increment) -> None:
        """The stated size is far too small for any placeable split, whatever
        the increment — so the only honest answers are a refusal, or a real
        position with money on every leg."""
        report = find_opportunities(
            [self._q("pinnacle", Selection.AWAY, 1.2160, 0.55),
             self._q("fanduel", Selection.HOME, 7.7010, 100.0)],
            total_stake=100.0, stake_increment=increment,
            as_of=self.AS_OF, commissions={},
        )
        for opportunity in report.opportunities:
            assert opportunity.total_stake > 0, opportunity.describe()
            assert all(leg.stake > 0 for leg in opportunity.legs)
        if not report.opportunities:
            assert "cannot_be_placed_at_the_stated_limits" in [
                d.code for d in report.diagnostics
            ]

    @pytest.mark.parametrize("seed", [51, 52])
    def test_no_increment_publishes_stakeless_money(self, seed) -> None:
        import random

        rng = random.Random(seed)
        for _ in range(400):
            home = round(rng.uniform(1.2, 6.0), 3)
            away = round(rng.uniform(1.2, 6.0), 3)
            if 1.0 / home + 1.0 / away >= 1.0:
                continue
            report = find_opportunities(
                [self._q("pinnacle", Selection.HOME, home,
                         round(rng.uniform(0.2, 5.0), 2)),
                 self._q("fanduel", Selection.AWAY, away)],
                total_stake=100.0, stake_increment=rng.choice([0.2, 0.1, 0.05]),
                as_of=self.AS_OF, commissions={},
            )
            for opportunity in report.opportunities:
                assert opportunity.total_stake > 0, opportunity.describe()
                assert all(leg.stake > 0 for leg in opportunity.legs), (
                    opportunity.describe()
                )


class TestAGenerationalSuffixDoesNotSplitATennisPlayer:
    """Books mix "Martin Damm" and "Martin Damm Jr" for the same ATP player
    (whose father also toured), so one real match sat under two event keys —
    and bovada's rows never joined the other three books' for that fixture,
    excluding the leg from every cross-book comparison with nothing anywhere to
    say so: every cross-source check groups by event key or participant pair,
    and both differed.  The committed fixtures contain the split and the whole
    suite was green over it.
    """

    def _slug(self, name):
        from src.participants import _open_slug
        from src.vocab import Sport

        return _open_slug(name, sport=Sport.TENNIS)

    @pytest.mark.parametrize("spelling", ["Martin Damm Jr", "Martin Damm Jr.",
                                          "Damm Jr Martin", "Martin Damm"])
    def test_every_spelling_lands_on_one_key(self, spelling) -> None:
        assert self._slug(spelling) == "dammmartin"

    def test_a_suffix_only_name_is_not_erased_to_nothing(self) -> None:
        """The filter must fall back to the raw tokens rather than empty the
        name entirely."""
        assert self._slug("Jr") == "jr"

    def test_clubs_keep_their_junior_teams_apart(self) -> None:
        """The club branch is untouched: there ``junior`` separates a real
        junior side from the senior one, which is a merge that would pair a
        youth fixture with the first team's."""
        from src.participants import _open_slug
        from src.vocab import Sport

        junior = _open_slug("Palmeiras Jr", sport=Sport.SOCCER)
        senior = _open_slug("Palmeiras", sport=Sport.SOCCER)
        assert junior != senior

    def test_the_fixture_slate_no_longer_splits_the_match(self) -> None:
        """Replay the committed captures end to end: no tennis date may hold two
        event keys that share one participant and the same start time — the
        near-identity shape that only a spelling split produces."""
        import collections

        from src.events import reconcile_event_keys
        from src.raw_store import RawStore
        from src.sources import registry
        from src.vocab import Sport

        store = RawStore(pathlib.Path("tests/fixtures/raw"))
        quotes = []
        for key in registry.keys():
            raws = [store.read(path) for path in sorted(
                pathlib.Path("tests/fixtures/raw").glob(f"{key}__*.json"))]
            adapter = registry.descriptor(key).replay_instance()
            try:
                quotes.extend(adapter.parse(raws).quotes)
            finally:
                adapter.close()
        quotes, _ = reconcile_event_keys(quotes)
        fixtures = collections.defaultdict(set)
        for quote in quotes:
            if quote.sport is not Sport.TENNIS:
                continue
            fixtures[quote.event_key].add(quote.commence_time)
        sides = collections.defaultdict(set)
        for event_key in fixtures:
            body = event_key.split(":")[0]
            away, home = body.split("@")
            for participant in (away, home):
                sides[participant].add(event_key)
        splits = []
        for participant, keys in sides.items():
            if len(keys) < 2:
                continue
            for first in keys:
                for second in keys:
                    if first < second and fixtures[first] & fixtures[second]:
                        splits.append((participant, first, second))
        assert not splits, splits


class TestANarrowedRunStillFilesTheMirrorFinding:
    """``counterparty_groups`` is measured on the unfiltered rows — the comment
    beside it says the evidence "does not stop existing because this command was
    asked about one league" — and ``_check_distinctness`` was run on the
    *filtered* rows six hundred lines later.

    The gate still blocked positions, so no money was misreported; what was
    hidden is the finding that tells a person to remove one of the two keys from
    the registry, on exactly the narrowed runs an operator uses day to day: two
    sources mirrored on 24 MLB selections were reported on an unscoped run and
    silent under ``--sport tennis``.
    """

    def test_the_filing_reads_the_unfiltered_rows(self) -> None:
        text = pathlib.Path("src/collector.py").read_text()
        # Shares the slate's one ``find_mirrors`` pass with the gate — the call
        # still names ``unfiltered_quotes``, which is the invariant this locks.
        assert "_check_distinctness(\n        unfiltered_quotes,\n        report,\n        mirrors=measured_mirrors," in text
        measured = text.index(
            "measured_counterparties = counterparty_groups(all_quotes, mirrors=measured_mirrors)"
        )
        kept = text.index("unfiltered_quotes = all_quotes")
        filtered = text.index("kept = [q for q in all_quotes if in_scope(q, sports, leagues)]")
        assert measured < kept < filtered, "the unfiltered binding must precede the filter"


class TestReplayJudgesAScopedRunByItsOwnScope:
    """``replay_run`` narrowed the stored side by the *command's* scope and
    re-parsed everything the raws contain — so a run collected under
    ``--sport baseball`` was reported as corrupt for the very rows its own
    filter dropped: "row count differs: stored 2476, replayed 5429 / replay
    invented row", exit 1, and "replay FAIL" in the dashboard masthead.  The
    README's own quick start (``collect --sport hockey`` then ``replay``)
    walked into it.  The scope was recoverable only as prose in the run's note,
    which no code could safely read back, so it is now recorded structurally.
    """

    def test_a_scoped_run_replays_clean(self, tmp_path, monkeypatch) -> None:
        from src.collector import collect_once, replay_run
        from src.raw_store import RawStore
        from src.sources import registry
        from src.store import Store
        from tests.conftest import CapturedSource

        raw_store = RawStore(tmp_path / "raw")
        fixture_dir = pathlib.Path("tests/fixtures/raw")
        sources = []
        for key in ("pinnacle", "fanduel"):
            descriptor = registry.descriptor(key)
            source = descriptor.replay_instance()
            raws = [
                RawStore(fixture_dir).read(path)
                for path in sorted(fixture_dir.glob(f"{key}__*.json"))
            ]
            sources.append(CapturedSource(source, raws))
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                sources, raw_store=raw_store, store=store, sports=["baseball"],
            )
            assert result.excluded_by_filter > 0, (
                "the fixtures must hold non-baseball rows for this to test anything"
            )
            ok, problems = replay_run(
                result.run_id, store=store, raw_store=raw_store,
            )
        assert ok, problems

    def test_the_scope_is_recorded_structurally(self, tmp_path) -> None:
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), sports=["baseball"],
                                  leagues=["MLB"])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(), excluded=2953)
            assert store.run_scope(run) == (["baseball"], ["MLB"], 2953)

    def test_a_command_scope_still_composes_with_the_runs(self, tmp_path, monkeypatch) -> None:
        """Both filters apply — asking ``replay --sport hockey`` about a
        baseball-scoped run must compare empty against empty, not resurrect the
        dropped rows (a union of the two scopes would)."""
        from src.collector import collect_once, replay_run
        from src.raw_store import RawStore
        from src.sources import registry
        from src.store import Store
        from tests.conftest import CapturedSource

        raw_store = RawStore(tmp_path / "raw")
        fixture_dir = pathlib.Path("tests/fixtures/raw")
        descriptor = registry.descriptor("pinnacle")
        source = descriptor.replay_instance()
        raws = [
            RawStore(fixture_dir).read(path)
            for path in sorted(fixture_dir.glob("pinnacle__*.json"))
        ]
        captured = CapturedSource(source, raws)
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [captured], raw_store=raw_store, store=store, sports=["baseball"],
            )
            ok, problems = replay_run(
                result.run_id, store=store, raw_store=raw_store, sports=["hockey"],
            )
        assert ok, problems


class TestAMigratedRunsReplayNamesTheParserNotCorruption:
    """After migrating a v3 history the current parser can legitimately disagree
    with what a v3-era run stored — different endpoint labels, evolved field
    values — and ``replay`` reported every difference as corruption, with the
    dashboard masthead reading "replay FAIL (21)" straight after a successful
    migrate.  Migration now stamps each pre-existing run with the version it was
    collected under, and replay's verdict says which kind of claim it makes."""

    def test_migration_stamps_the_runs(self, tmp_path) -> None:
        import sqlite3 as sqlite

        from src.store import Store, migrate_database
        from tests.test_pipeline import write_v3_database

        path = tmp_path / "old.sqlite3"
        write_v3_database(path)
        migrate_database(path, backup=False)
        with Store(path) as store:
            rows = store.query("SELECT id, migrated_from FROM collection_run")
            assert rows, "the v3 fixture must hold at least one run"
            assert all(row["migrated_from"] == 3 for row in rows)
            # a run collected natively afterwards carries no stamp
            fresh = store.start_run(datetime.now(UTC))
            row = store.run_row(fresh)
            assert row["migrated_from"] is None
        assert isinstance(sqlite.Row, type)

    def test_replay_prefixes_the_stamp_when_differences_appear(self, tmp_path) -> None:
        """Synthetic: a stamped run whose stored rows cannot be reproduced —
        the problems list must open with the parser-evolution context."""
        from src.collector import replay_run
        from src.raw_store import RawStore
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
            store._conn.execute(
                "UPDATE collection_run SET migrated_from = 3 WHERE id = ?", (run,)
            )
            store._conn.commit()
            ok, problems = replay_run(
                run, store=store, raw_store=RawStore(tmp_path / "raw"),
            )
        assert not ok
        assert "collected under schema v3" in problems[0], problems

    def test_a_migrated_runs_read_failure_is_not_vouched_for(self, tmp_path) -> None:
        """The stamp explains comparisons, not byte integrity.

        The first wording asserted "the stored rows are still what was
        collected, and a sha mismatch is the only sign of changed bytes"
        on EVERY failing migrated return.  Once read failures became
        problems for every source (round 5), a migrated run with a deleted
        archive file printed that vouching clause directly above its own
        FileNotFoundError — the oldest runs, whose archives are the
        likeliest to have rotted, wore the most confident preamble.
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        rigged = TestReplayComparesTheWholeRow._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            store._conn.execute(
                "DELETE FROM quote WHERE run_id = ?", (result.run_id,)
            )
            store._conn.execute(
                "UPDATE collection_run SET migrated_from = 3 WHERE id = ?",
                (result.run_id,),
            )
            store._conn.commit()
            next((tmp_path / "raw").rglob("*.json")).unlink()
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert "collected under schema v3" in problems[0], problems
        assert "nothing here vouches" in problems[0], problems
        assert "verified against" not in problems[0], problems
        assert any("stored bytes unreadable" in p for p in problems), problems

    def test_a_migrated_all_comparison_fail_names_verified_bytes(self, tmp_path) -> None:
        """When every problem IS a comparison, the migrated preamble may —
        and should — say the bytes verified, with the same two-sided framing
        the non-migrated preamble carries (an edited stored row looks the
        same, and no sha256 covers the quote table).
        """
        from src.collector import SOURCE_FACTORIES, collect_once, replay_run
        from src.raw_store import RawStore
        from src.store import Store

        rigged = TestReplayComparesTheWholeRow._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            store._conn.execute(
                "DELETE FROM quote WHERE rowid = ("
                "SELECT rowid FROM quote WHERE run_id = ? LIMIT 1)",
                (result.run_id,),
            )
            store._conn.execute(
                "UPDATE collection_run SET migrated_from = 3 WHERE id = ?",
                (result.run_id,),
            )
            store._conn.commit()
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert not ok
        assert "collected under schema v3" in problems[0], problems
        assert "verified against its recorded sha256" in problems[0], problems
        assert "would look the same" in problems[0], problems


class TestTheFlowStripAccountsForTheScopeFilter:
    """For a scoped run the flow strip rendered ``read 5,429 -> checked 5,429 ->
    stored 2,476``: 2,953 rows vanish between adjacent boxes, and "checked
    5,429" is false — validation runs on what the filter kept.  The explaining
    sentence (the run's note) was serialized into the payload with nothing
    rendering it."""

    def test_the_payload_carries_the_excluded_count(self, tmp_path) -> None:
        from src.report import build_report
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), sports=["baseball"])
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1),
                             excluded=2953)
            data = build_report(store)
        assert data["runs"][0]["excluded_count"] == 2953

    def test_the_strip_has_the_scope_step(self) -> None:
        text = pathlib.Path("src/report_assets.py").read_text()
        assert "dropped by the run's --sport/--league scope" in text
        assert "parsed - excluded" in text


class TestOperatorFacingSentencesAreTrueOnANewBook:
    """Round 23's operator-week sweep: three sentences that were wrong.

    ``migrate`` printed "markets renamed: run_line->3348" — the dict maps old
    name to *row count*, so the operator read their markets renamed to numbers.
    The lede kept a private three-book names dict and printed raw registry keys
    for everything added since ("…FanDuel, leovegas_kambi and Pinnacle").  And
    ``collect --watch --max-runs 3`` exited 0 when pass 2 of 3 died, because one
    good pass overwrote the failure.
    """

    def test_the_migrate_summary_counts_rows(self) -> None:
        from src.store import Migration

        migration = Migration(
            path=pathlib.Path("x.sqlite3"), from_version=3, to_version=4,
            quotes_migrated=9936, event_keys_rewritten=100,
            markets_renamed={"run_line": 3348, "total_runs": 4272},
        )
        text = migration.summary()
        assert "run_line (3,348 rows)" in text
        assert "->3348" not in text and "-> 3348" not in text

    def test_the_lede_names_every_book_from_source_notes(self) -> None:
        from src.report import SOURCE_NOTES, _lede

        latest = {
            "quote_count": 4,
            "event_count": 2,
            "sources": [
                {"key": key, "quote_count": 1}
                for key in ("leovegas_kambi", "smarkets", "sxbet", "kalshi")
            ],
            "sports": [{"sport": "baseball", "comparable": True}],
        }
        lede = _lede(latest)
        for key in ("leovegas_kambi", "smarkets", "sxbet", "kalshi"):
            assert key not in lede, lede
            assert SOURCE_NOTES[key]["label"] in lede

    def test_source_notes_names_every_registered_source(self) -> None:
        """The lede's own comment claims "SOURCE_NOTES … names every registered
        source", and the sources page falls back to an unknown-venue card
        (raw key, hardcoded sportsbook kind) for any it misses.  Six registered
        keys were missing when this pin landed — draftkings, hardrock, caesars
        and the three vi_ book columns — so the claim was false and every one
        of them rendered as "No description recorded"."""
        from src.report import SOURCE_NOTES
        from src.sources import registry

        missing = sorted(set(registry.keys()) - set(SOURCE_NOTES))
        assert missing == [], missing

    def test_a_failed_middle_pass_fails_the_batch(self) -> None:
        """The sticky property, pinned structurally: once any pass fails the
        exit code must never be reassigned back to zero."""
        text = pathlib.Path("src/collector.py").read_text()
        # anchored as a code line — the explanatory comment quotes the old text
        assert "\n            exit_code = 0 if result.ok else 1\n" not in text
        index = text.index("if not batch.ok:\n                exit_code = 1")
        assert "result.print_summary()" in text[max(0, index - 800): index]


class TestAThinFeeProtectedMarketIsNotAMispairing:
    """Kalshi resting at 49¢/49¢ sums to 0.98 gross — below the flat 0.99 floor
    — and ~1.015 net of its contract fee, so makers legitimately sit there:
    nobody can take both sides at a profit.  The flat floor filed it as "a book
    does not price itself to lose" (a sportsbook sentence about a venue that is
    not a book), failed the live run, and ``arb`` deleted the venue from the
    market.  The boundary is now the **net** sum; the bid-read-as-ask parser
    trap the flat floor was calibrated against is caught at the source level by
    rate, where a systematic fault actually lives.
    """

    AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def _kalshi_pair(self, price=0.49, market_id="m1", event_key=None):
        kwargs = {"event_key": event_key} if event_key else {}
        return [
            make_quote(source="kalshi", selection=Selection.HOME,
                       decimal_odds=round(1.0 / price, 6),
                       source_market_id=market_id, **kwargs),
            make_quote(source="kalshi", selection=Selection.AWAY,
                       decimal_odds=round(1.0 / price, 6),
                       source_market_id=market_id, **kwargs),
        ]

    def test_the_live_case_is_a_warning_not_an_error(self) -> None:
        from src.validation import Severity, validate

        report = validate(self._kalshi_pair(), order_book_sources={"kalshi"})
        found = [f for f in report.findings if f.code == "negative_overround"]
        assert found and found[0].severity is Severity.WARNING, report.findings
        assert "not a mispairing" in found[0].message
        assert report.ok

    def test_arb_keeps_the_venue(self) -> None:
        """The same shape must not delete Kalshi from a cross-book market."""
        rows = self._kalshi_pair() + [
            make_quote(source="pinnacle", selection=Selection.HOME,
                       decimal_odds=1.80, source_market_id="m1"),
            make_quote(source="pinnacle", selection=Selection.AWAY,
                       decimal_odds=1.95, source_market_id="m1"),
        ]
        report = find_opportunities(rows, as_of=self.AS_OF)
        assert "source_prices_itself_to_lose" not in [
            d.code for d in report.diagnostics
        ], [d.detail for d in report.diagnostics]

    def test_the_systematic_trap_is_still_loud(self) -> None:
        """Bid read as ask sub-1.0s *most* of a source's markets, executably —
        deep enough that no fee explains it.  Six markets at 45¢/45¢ must file
        the source-level error the flat floor existed to keep."""
        from src.validation import validate

        rows = []
        for index in range(22):  # past MIN_SUB_UNITY_MARKETS
            rows.extend(self._kalshi_pair(
                price=0.45, market_id=f"m{index}",
                event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
            ))
        report = validate(rows, order_book_sources={"kalshi"})
        assert "systematic_sub_unity_pricing" in [f.code for f in report.findings]
        assert not report.ok

    def test_a_live_shaped_exchange_does_not_trip_the_trap_gate(self) -> None:
        """Shaped like the real thing: this session's live slates put Matchbook's
        median gross overround at 1.032-1.035 with a 16% tail below 0.99, so a
        healthy venue has a spread on most markets and crosses on a few.  A
        gate that convicts this shape fails the run and tells the operator every
        Matchbook price is suspect."""
        from src.validation import validate

        rows = []
        for index in range(25):
            # 21 markets holding a ~3% spread, 4 thin/crossed — the live ratio
            price = 0.515 if index < 21 else 0.4925
            rows.extend([
                make_quote(source="matchbook", selection=selection,
                           decimal_odds=round(1.0 / price, 6),
                           source_market_id=f"m{index}",
                           event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28")
                for selection in (Selection.HOME, Selection.AWAY)
            ])
        report = validate(rows, order_book_sources={"matchbook"})
        assert "systematic_sub_unity_pricing" not in [
            f.code for f in report.findings
        ], [f.message for f in report.findings]


class TestTheMirrorGateSurvivesAScopedRunsStore:
    """``collect_once`` measures the counterparty gate on the full slate before
    the ``--sport`` filter, and the store receives only the kept rows — so
    re-analysing a scoped run re-measured the gate from narrowed evidence.

    Two licences of one operator, established as one counterparty on 22 shared
    MLB selections the filter then dropped, were re-judged independent, and
    ``arb --run N`` published ``margin 2.91%, guaranteed +3.00`` with both legs
    at one book — seconds after the live pass had refused the identical rows.
    The collector's measurement is now persisted on the run and unioned into
    every re-measurement, because evidence only ever accumulates.
    """

    def test_the_gate_is_recorded_and_read_back(self, tmp_path) -> None:
        from src.store import Store
        from src.validation import ValidationReport

        groups = {"*": [frozenset({"betrivers_kambi", "leovegas_kambi"})]}
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), sports=["tennis"])
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1),
                             counterparties=groups)
            assert store.recorded_counterparty_groups(run) == groups
            # a run that recorded nothing reads back empty, not broken
            bare = store.start_run(datetime.now(UTC))
            store.finish_run(bare, finished_at=datetime.now(UTC),
                             report=ValidationReport())
            assert store.recorded_counterparty_groups(bare) == {}

    def test_merge_unions_and_never_forgets(self) -> None:
        from src.arb import merge_counterparty_groups

        recorded = {"*": [frozenset({"a", "b"})]}
        remeasured = {"*": [frozenset({"c", "d"})], "MLB": [frozenset({"e", "f"})]}
        merged = merge_counterparty_groups(remeasured, recorded)
        assert frozenset({"a", "b"}) in merged["*"]
        assert frozenset({"c", "d"}) in merged["*"]
        assert merged["MLB"] == [frozenset({"e", "f"})]
        # duplicates collapse; None tables are tolerated
        again = merge_counterparty_groups(merged, merged, None)
        assert again == merged

    def test_the_re_analysis_sites_union_the_record(self) -> None:
        text = pathlib.Path("src/collector.py").read_text()
        assert text.count("store.recorded_counterparty_groups(run_id)") >= 2
        assert "merge_counterparty_groups(" in text


class TestRunResolutionHonoursTheScopeAsked:
    """``_resolve_run`` was scope-blind: ``arb --sport baseball`` resolved a
    ``--sport tennis`` run — which kept no baseball by the operator's own flag —
    printed the quiet-slate sentence the resolver's docstring exists to prevent,
    and exited 0, while the previous run's real baseball edge sat one ``--run``
    away and ``health --sport baseball`` described that earlier run.  Two
    commands, seconds apart, describing different collections."""

    def _database(self, tmp_path):
        from src.store import Store
        from src.validation import ValidationReport

        store = Store(tmp_path / "db.sqlite3")
        first = store.start_run(datetime.now(UTC))
        store.save_quotes_by_source(first, [make_quote()])  # baseball
        store.finish_run(first, finished_at=datetime.now(UTC),
                         report=ValidationReport(quote_count=1, event_count=1))
        second = store.start_run(datetime.now(UTC), sports=["tennis"])
        store.save_quotes_by_source(second, [make_quote(
            source="pinnacle", sport=Sport.TENNIS, league="ATP",
            event_key="TENNIS-a@TENNIS-b:2026-07-28",
            home_participant="TENNIS-b", away_participant="TENNIS-a",
            market=Market.MONEYLINE,
        )])
        store.finish_run(second, finished_at=datetime.now(UTC),
                         report=ValidationReport(quote_count=1, event_count=1))
        return store, first, second

    def test_the_scoped_ask_resolves_the_run_that_holds_it(self, tmp_path) -> None:
        from src.collector import _resolve_run

        store, first, second = self._database(tmp_path)
        with store:
            resolved, note = _resolve_run(store, None, what="to analyse",
                                          sports=["baseball"], leagues=None)
            assert resolved == first
            assert f"the newer run {second} holds none of it" in note
            # named once — the caller appends the scope label itself
            assert note.count("sport=") == 0
            unscoped, _ = _resolve_run(store, None, what="to analyse")
            assert unscoped == second

    def test_nothing_in_scope_names_the_newest_runs_own_scope(self, tmp_path) -> None:
        from src.collector import _resolve_run
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), sports=["tennis"])
            store.save_quotes_by_source(run, [make_quote(
                source="pinnacle", sport=Sport.TENNIS, league="ATP",
                event_key="TENNIS-a@TENNIS-b:2026-07-28",
                home_participant="TENNIS-b", away_participant="TENNIS-a",
                market=Market.MONEYLINE,
            )])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
            resolved, note = _resolve_run(store, None, what="to analyse",
                                          sports=["hockey"], leagues=None)
        assert resolved is None
        assert "--sport tennis" in note
        assert "excludes what was asked for" in note


class TestTheLedeDoesNotBlameTheOperatorsOwnScope:
    """League coverage is recorded before the scope filter, so the dashboard's
    coverage derivation blank-filled every configured sport — and the lede of a
    ``--sport tennis`` run claimed "Baseball did not clear that bar … listed
    rather than hidden": both books priced baseball, the operator excluded it,
    and none of it is listed.  The run's own note, in the same payload, said so
    correctly."""

    def test_a_scope_excluded_sport_is_not_a_coverage_gap(self, tmp_path) -> None:
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), sports=["tennis"])
            store.save_health(run, SourceHealth(
                source_key="pinnacle", ok=True, checked_at=datetime.now(UTC),
                quote_count=1, event_count=1,
            ))
            store.save_league_coverage(run, "pinnacle", [
                ("MLB", "baseball", 8, 1),
                ("ATP", "tennis", 1, 1),
            ])
            store.save_quotes_by_source(run, [make_quote(
                source="pinnacle", sport=Sport.TENNIS, league="ATP",
                event_key="TENNIS-a@TENNIS-b:2026-07-28",
                home_participant="TENNIS-b", away_participant="TENNIS-a",
                market=Market.MONEYLINE,
            )])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1),
                             excluded=8)
            data = build_report(store)
        sports_listed = [entry["sport"] for entry in data["runs"][0]["sports"]]
        assert "baseball" not in sports_listed, sports_listed
        assert "baseball" not in data["meta"]["lede"].lower()


class TestTheTrapGateCountsGrossNotNet:
    """The lay-read-as-back parser trap produces mispricings *smaller than the
    venue's own commission* on every realistic bid-ask spread — ~2¢ on
    Matchbook, ~4.5¢ on Kalshi — so each trapped market nets above 1.0 and the
    per-market check files it benign.  Counting only net-executable markets in
    the rate gate's numerator therefore read 21 of 295 (7%) on a slate where
    the trap had been set and 290 of 295 were gross-sub-1.0 — and the gate
    calibrated against exactly that trap never fired, while ``arb`` published
    ``guaranteed +1.68`` on a lay price nobody can take, through a GREEN run.

    The numerator is now the gross sub-0.99 markets: honest fixtures measure 0%
    for all five order-driven sources, tight 2.02/2.02 books (0.9901) stay out,
    and the trap measures 33-98%.
    """

    def _matchbook_market(self, price, index):
        return [
            make_quote(source="matchbook", selection=selection,
                       decimal_odds=round(1.0 / price, 6),
                       source_market_id=f"m{index}",
                       event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28")
            for selection in (Selection.HOME, Selection.AWAY)
        ]

    def test_a_shallow_systematic_trap_fails_the_run(self) -> None:
        """Gross 0.985 per market — benign per-market once Matchbook's 2%
        winnings commission nets it above 0.99 — on six of six markets, so the
        source's median market prices below fair."""
        from src.commission import net_decimal_odds
        from src.validation import validate

        gross_each = 0.4925
        net = 2.0 / net_decimal_odds("matchbook", 1.0 / gross_each)
        assert net >= 0.99, "the construction must be per-market benign"
        rows = []
        for index in range(22):  # past MIN_SUB_UNITY_MARKETS
            rows.extend(self._matchbook_market(gross_each, index))
        report = validate(rows, order_book_sources={"matchbook"})
        assert "systematic_sub_unity_pricing" in [f.code for f in report.findings], [
            (f.code, f.severity) for f in report.findings
        ]
        assert not report.ok

    def test_a_minority_of_thin_markets_is_still_sporadic(self) -> None:
        """Genuine thin books among ordinary ones must not convict the source:
        the median is what decides, so a tail — even a large one — cannot."""
        from src.validation import validate

        rows = []
        for index in range(4):
            rows.extend(self._matchbook_market(0.4925, index))   # crossed
        for index in range(4, 12):
            rows.extend(self._matchbook_market(0.515, index))    # a real spread
        report = validate(rows, order_book_sources={"matchbook"})
        assert "systematic_sub_unity_pricing" not in [f.code for f in report.findings]


class TestARefusalCaptureCannotAbortThePass:
    """The refusal-payload ``_persist_raw`` in the ``SourceError`` handler sat
    outside the per-source raw-write isolation, and the two failures it joins
    are correlated: a venue blocking you is exactly when a refusal payload
    exists, and an unwritable raw directory makes every source take some write
    path.  A blocked source plus a bad directory aborted the whole pass — no
    health rows, an unfinished run wearing the Ctrl-C signature, the healthy
    sources' fetches discarded."""

    def test_a_failing_refusal_write_degrades_to_a_recorded_refusal(
        self, tmp_path, monkeypatch
    ) -> None:
        from src import collector
        from src.collector import _collect_source
        from src.raw_store import RawResponse, RawStore
        from src.sources.base import ParseOutcome
        from src.sources.guards import BlockedError

        class _Blocked:
            source_key = "pinnacle"

            def fetch(self, tier=None):
                error = BlockedError("access denied")
                error.raw = RawResponse(
                    source="pinnacle", endpoint="block", url="https://x",
                    status_code=403, content_type="text/html",
                    fetched_at=datetime.now(UTC), body="blocked",
                )
                raise error

            def close(self):
                pass

        def _explodes(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(collector, "_persist_raw", _explodes)
        monkeypatch.setattr(collector, "_fetch",
                            lambda source, tier: source.fetch(tier))
        health, outcome = _collect_source(
            _Blocked(), raw_store=RawStore(tmp_path / "raw"), store=None,
            run_id=None,
        )
        assert health.ok is False
        assert health.error_kind == "blocked"
        assert isinstance(outcome, ParseOutcome)

    def test_a_parse_failure_still_carries_the_scopes_the_adapter_lost(
        self, tmp_path, monkeypatch
    ) -> None:
        """The tally is filled during fetch.  Dropping it on the parse-error
        path left a source that had already recorded which leagues were refused
        looking like one that had never been asked for anything."""
        from src import collector
        from src.collector import _collect_source
        from src.raw_store import RawResponse, RawStore
        from src.sources._common import ScopeTally
        from src.sources.base import ParseOutcome
        from src.sources.guards import CoverageCappedError

        tally = ScopeTally("book_a")
        tally.requested("mlb")
        tally.failed("epl", CoverageCappedError("blocked"))
        tally.truncated("mlb", CoverageCappedError("beyond the page cap"))

        class _Partial:
            source_key = "book_a"
            last_fetch = tally

            def fetch_raw(self):
                return [
                    RawResponse(
                        source="book_a", endpoint="mlb", url="https://x",
                        status_code=200, content_type="application/json",
                        fetched_at=datetime.now(UTC), body="[]",
                    )
                ]

            def parse(self, raws):
                raise RuntimeError("boom")

            def close(self):
                pass

        monkeypatch.setattr(
            collector, "_fetch", lambda source, tier: source.fetch_raw()
        )
        health, outcome = _collect_source(
            _Partial(), raw_store=RawStore(tmp_path / "raw"), store=None,
            run_id=None,
        )
        assert health.ok is False
        assert health.error_kind == "parse_error"
        assert health.scopes_requested == 2
        assert health.scopes_failed == 1
        assert health.failed_scopes == ("epl: blocked",)
        assert health.truncated_scopes == ("mlb: beyond the page cap",)
        assert isinstance(outcome, ParseOutcome)

    def test_a_raw_write_failure_still_carries_the_scopes_the_adapter_lost(
        self, tmp_path, monkeypatch
    ) -> None:
        """Same guarantee as the parse-error path, on the other early return.

        Dropping the four telemetry fields from only the ``raw_write_failed``
        branch left that path unpinned: the parse-error test stayed green.
        """
        from src import collector
        from src.collector import _collect_source
        from src.raw_store import RawResponse, RawStore
        from src.sources._common import ScopeTally
        from src.sources.base import ParseOutcome
        from src.sources.guards import CoverageCappedError

        tally = ScopeTally("book_a")
        tally.requested("mlb")
        tally.failed("epl", CoverageCappedError("blocked"))
        tally.truncated("mlb", CoverageCappedError("beyond the page cap"))

        class _Partial:
            source_key = "book_a"
            last_fetch = tally

            def fetch_raw(self):
                return [
                    RawResponse(
                        source="book_a", endpoint="mlb", url="https://x",
                        status_code=200, content_type="application/json",
                        fetched_at=datetime.now(UTC), body="[]",
                    )
                ]

            def parse(self, raws):
                raise AssertionError("parse must not run after a raw-write failure")

            def close(self):
                pass

        def _explodes(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(collector, "_persist_raw", _explodes)
        monkeypatch.setattr(
            collector, "_fetch", lambda source, tier: source.fetch_raw()
        )
        health, outcome = _collect_source(
            _Partial(), raw_store=RawStore(tmp_path / "raw"), store=None,
            run_id=None,
        )
        assert health.ok is False
        assert health.error_kind == "raw_write_failed"
        assert health.scopes_requested == 2
        assert health.scopes_failed == 1
        assert health.failed_scopes == ("epl: blocked",)
        assert health.truncated_scopes == ("mlb: beyond the page cap",)
        assert isinstance(outcome, ParseOutcome)


class TestAPreUpgradeScopedRunKeepsItsScope:
    """The scope columns arrived by additive upgrade, which backfills ``''`` —
    read as *unscoped* — so every scoped run collected before the upgrade
    replayed against an unscoped re-parse and was reported corrupt all over
    again.  The note's machine-written ``--sport``/``--league`` tokens are the
    fallback; the masthead now renders a migrated run's differences as DIFFERS
    with the parser-evolution context instead of counting the explanation as a
    problem."""

    def test_the_note_fallback_recovers_the_scope(self, tmp_path) -> None:
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=1, event_count=1),
                note="tier=full; comparable sports (2+ books on one fixture): "
                     "none; --sport hockey; --league NHL; 4237 rows excluded by filter",
            )
            # simulate the pre-upgrade backfill: blank structural columns
            store._conn.execute(
                "UPDATE collection_run SET scope_sports='', scope_leagues='' "
                "WHERE id = ?", (run,)
            )
            store._conn.commit()
            assert store.run_scope(run) == (["hockey"], ["NHL"], 4237)

    def test_a_structurally_scoped_run_ignores_the_note(self, tmp_path) -> None:
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), sports=["tennis"])
            store.finish_run(
                run, finished_at=datetime.now(UTC), report=ValidationReport(),
                note="--sport hockey",  # stale prose must not override structure
            )
            assert store.run_scope(run)[0] == ["tennis"]

    def test_the_masthead_renders_evolution_as_differs(self) -> None:
        text = pathlib.Path("src/report.py").read_text()
        assert "MIGRATED_EVOLUTION_NOTE in problems[0]" in text
        assert "DIFFERS ({len(problems) - 1})" in text

    def test_the_masthead_verdicts_follow_the_archive_not_the_wording(
        self, tmp_path, monkeypatch
    ) -> None:
        """Behavioral, because the source grep above cannot see which
        collector wording carries the matched phrase: round 6 reworded the
        preambles, moved the phrase from the benign migrated wording into
        the non-vouching one, and the masthead inverted with the grep still
        green — DIFFERS for a migrated run with a rotted archive, hard FAIL
        for benign migrated evolution.  The phrase is now a shared constant
        (``collector.MIGRATED_EVOLUTION_NOTE``); this drives both cases
        through the real ``_replay_note``.
        """
        from src import settings
        from src.collector import SOURCE_FACTORIES, collect_once
        from src.raw_store import RawStore
        from src.report import _replay_note
        from src.store import Store

        rigged = TestReplayComparesTheWholeRow._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            store._conn.execute(
                "DELETE FROM quote WHERE rowid = ("
                "SELECT rowid FROM quote WHERE run_id = ? LIMIT 1)",
                (result.run_id,),
            )
            store._conn.execute(
                "UPDATE collection_run SET migrated_from = 3 WHERE id = ?",
                (result.run_id,),
            )
            store._conn.commit()
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                benign = _replay_note(store, result.run_id)
                next((tmp_path / "raw").rglob("*.json")).unlink()
                rotted = _replay_note(store, result.run_id)
                from src.collector import replay_run

                _, rotted_problems = replay_run(
                    result.run_id, store=store, raw_store=raw_store
                )
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert benign.startswith("DIFFERS"), benign
        assert rotted.startswith("FAIL"), rotted
        # The FAIL count names real problems, not the code-authored preamble
        # a stamped run always opens with — the DIFFERS branch subtracted its
        # explanation while the FAIL branch counted it, so the number meant
        # different things depending on the verdict.
        assert rotted == f"FAIL ({len(rotted_problems) - 1})", (rotted, rotted_problems)

    def test_a_native_runs_tamper_cannot_buy_the_differs_verdict(
        self, tmp_path, monkeypatch
    ) -> None:
        """DIFFERS is gated on the run's migration stamp, not on text.

        On an UNSTAMPED run no preamble is inserted, so ``problems[0]`` is
        the first appended problem — and the unreadable-bytes message
        interpolates file-controlled text: a tampered ``envelope_version``
        carrying the constant's words bought "DIFFERS — run predates the
        current parser" on a native run, a fabricated provenance claim
        rendered directly over the tamper.  The stamp is structural; no
        problem string can reach the DIFFERS branch without it.
        """
        import json

        from src import settings
        from src.collector import MIGRATED_EVOLUTION_NOTE, SOURCE_FACTORIES, collect_once
        from src.raw_store import RawStore
        from src.report import _replay_note
        from src.store import Store

        rigged = TestReplayComparesTheWholeRow._make_rigged()
        raw_store = RawStore(tmp_path / "raw")
        monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                [rigged()], raw_store=raw_store, store=store, as_of=FETCHED
            )
            assert result.quotes
            raw_path = next((tmp_path / "raw").rglob("*.json"))
            envelope = json.loads(raw_path.read_text())
            envelope["envelope_version"] = f"9 {MIGRATED_EVOLUTION_NOTE}"
            raw_path.write_text(json.dumps(envelope))
            saved = SOURCE_FACTORIES.get("book_a")
            SOURCE_FACTORIES["book_a"] = rigged
            try:
                note = _replay_note(store, result.run_id)
            finally:
                if saved is None:
                    SOURCE_FACTORIES.pop("book_a", None)
                else:
                    SOURCE_FACTORIES["book_a"] = saved
        assert note.startswith("FAIL"), note


class TestAFailedRunsPricesCarryTheVerdict:
    """``_resolve_run`` checked existence, finished, quote_count and age — never
    the run's own verdict — so ``arb`` printed "guaranteed" positions from a run
    validation had recorded FAILED, whose stored findings can include "every
    price from this source is suspect", with exit 0 and no caveat."""

    def test_the_note_carries_the_failed_verdict(self, tmp_path) -> None:
        from src.collector import _resolve_run
        from src.store import Store
        from src.validation import Severity, ValidationReport

        report = ValidationReport(quote_count=1, event_count=1)
        report.add(Severity.ERROR, "systematic_sub_unity_pricing", "suspect")
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC), report=report)
            resolved, note = _resolve_run(store, None, what="to analyse")
        assert resolved == run
        assert "recorded FAILED with 1 validation error" in note
        assert "may be mis-parsed" in note

    def test_a_passing_run_carries_no_caveat(self, tmp_path) -> None:
        from src.collector import _resolve_run
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
            _, note = _resolve_run(store, None, what="to analyse")
        assert "FAILED" not in note


class TestTheTrapGateCannotConvictAHealthyVenue:
    """The rate-based trap gate was calibrated on the committed fixtures, where
    honest order-driven sources measure 0% gross-sub-0.99.  **Live** slates
    measure 15-20% (Polymarket touched 20.2%) against a 25% threshold, and a
    realistic 1.5% misread moves Matchbook from 16% to 18% — the honest band and
    the trap band overlapped, so the gate could fail a run and tell the operator
    every price from a healthy venue is suspect, while missing the trap it was
    for.

    It is a median now: a venue holds a spread, and one whose typical market
    prices below fair is a parser reading the wrong side of the book.  Honest
    live medians are 1.0100-1.1121; the separation is real.
    """

    def _slate(self, source, spread_markets, crossed_markets):
        rows = []
        for index in range(spread_markets + crossed_markets):
            price = 0.515 if index < spread_markets else 0.4925
            rows.extend([
                make_quote(source=source, selection=selection,
                           decimal_odds=round(1.0 / price, 6),
                           source_market_id=f"m{index}",
                           event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28")
                for selection in (Selection.HOME, Selection.AWAY)
            ])
        return rows

    @pytest.mark.parametrize("crossed", [0, 4, 9])
    def test_a_venue_that_holds_a_spread_is_never_convicted(self, crossed) -> None:
        """21 markets with a spread against up to 9 crossed — a heavier tail
        than any live slate showed — must stay clean, because the median is
        what decides."""
        from src.validation import validate

        report = validate(self._slate("matchbook", 21, crossed),
                          order_book_sources={"matchbook"})
        assert "systematic_sub_unity_pricing" not in [
            f.code for f in report.findings
        ], [f.message for f in report.findings]

    def test_a_venue_that_holds_no_spread_is_convicted(self) -> None:
        from src.validation import validate

        report = validate(self._slate("matchbook", 4, 21),
                          order_book_sources={"matchbook"})
        found = [f for f in report.findings if f.code == "systematic_sub_unity_pricing"]
        assert found, [f.code for f in report.findings]
        assert "typical market prices below fair" in found[0].message
        assert not report.ok

    def test_the_gate_is_a_median_not_a_rate(self) -> None:
        """Pinned structurally: a rate cannot separate these bands, and the
        measurement that proves it is recorded beside the constant."""
        text = pathlib.Path("src/validation.py").read_text()
        assert "MAX_SUB_UNITY_RATE" not in text
        assert "median(overrounds)" in text
        assert "no honest gate can be built from this data" in text


class TestTheDocsSayWhatTheCodeDoes:
    """Round 25 audited README.md and docs/INPUT_CONTRACT.md against behaviour
    for the first time in ~370 fixes.  Every claim below was false when checked.
    They are pinned against the code rather than against a copy of the prose, so
    the next change to the code fails the test instead of quietly outdating the
    document."""

    def test_the_settlement_tables_list_every_collectable_window(self) -> None:
        """Both tables listed 8 of the 14 ``(sport, period)`` pairs, and the 5
        missing from README were *every* one with ``draw_is_priced=True`` —
        while the README claimed an absent pair is "refused outright", which it
        is not: a ``soccer/first_half`` draw constructs fine."""
        from src.vocab import PERIOD_RULES

        for path in ("README.md", "docs/INPUT_CONTRACT.md"):
            text = pathlib.Path(path).read_text()
            for (sport, period) in PERIOD_RULES:
                window = f"| {sport.value} {period.value.replace('_', ' ')} |"
                assert window in text, f"{path} omits {sport.value}/{period.value}"

    def test_the_settlement_tables_state_the_right_facts(self) -> None:
        """Not just present — correct.  A row that says a window cannot end
        level when it can is worse than a missing row."""
        from src.vocab import PERIOD_RULES

        for path in ("README.md", "docs/INPUT_CONTRACT.md"):
            text = pathlib.Path(path).read_text()
            for (sport, period), rule in PERIOD_RULES.items():
                prefix = f"| {sport.value} {period.value.replace('_', ' ')} |"
                row = next(line for line in text.splitlines() if line.startswith(prefix))
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                says_tie = "yes" in cells[1]
                says_draw = "yes" in cells[2]
                assert says_tie == rule.tie_possible, (path, prefix, row)
                assert says_draw == rule.draw_is_priced, (path, prefix, row)

    def test_the_ok_bar_is_described_as_venues_not_sportsbooks(self) -> None:
        """``_check_source_health`` counts *venues*: two exchanges clear the
        floor with zero sportsbooks, so every sportsbook could be geo-blocked
        and an unattended loop still reported ok and exited 0."""
        for path in ("README.md", "src/collector.py"):
            text = pathlib.Path(path).read_text()
            assert "at least two sportsbooks produced data" not in text
            assert "at least two\nsportsbooks actually produced data" not in text

    def test_one_measurement_has_one_value(self) -> None:
        """The same Smarkets-ordering incident was quoted as 691 in the README
        and 847 in the registry."""
        readme = pathlib.Path("README.md").read_text()
        registry_text = pathlib.Path("src/sources/registry.py").read_text()
        assert "691 of their shared markets" not in readme
        for text in (readme, registry_text):
            assert "847 of their shared markets" in text

    def test_the_glossary_does_not_contradict_the_slate(self) -> None:
        """It said a draw is backable "in soccer — and in hockey over regulation
        time only", while the captured slate holds 38 baseball first-inning draw
        rows and ``PERIOD_RULES`` prices a draw in eight windows."""
        from src.report import GLOSSARY

        entry = next(e for e in GLOSSARY if e["term"] == "Who wins")
        assert "in hockey over regulation time only" not in entry["plain"]

    def test_the_glossary_gets_prediction_market_fees_right(self) -> None:
        """It called every commission something "taken out of a winning bet",
        which is the opposite of a contract fee charged on entry — it makes the
        losing leg of a Kalshi hedge look free."""
        from src.commission import ContractFeeCommission
        from src.report import GLOSSARY

        entry = next(e for e in GLOSSARY if e["term"] == "Commission")
        assert "when you enter" in entry["plain"]
        assert "whether or not it settles your way" in entry["plain"]
        # and the model really does charge on entry, win or lose
        fee = ContractFeeCommission(label="kalshi", rate=0.07, shape="p_times_q")
        assert fee.net_decimal(2.0) < 2.0


class TestAnEnvironmentValueThatCannotBeHonouredIsRefused:
    """``--interval`` got a validator with a comment explaining that ``-5``
    "reached ``time.sleep`` and killed the loop with a traceback after the first
    successful pass".  argparse validators never see the *default*, which comes
    from ``ODDS_INTERVAL_SECONDS`` — so the environment reintroduced the exact
    bug the flag had fixed, and ``ODDS_HTTP_TIMEOUT=abc`` aborted every command
    including ``--help`` with a traceback that never named the variable."""

    def _settings(self, monkeypatch, **env):
        import importlib

        import src.settings as settings

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(settings)

    @pytest.mark.parametrize("value", ["-9", "0", "abc", "nan"])
    def test_an_unusable_interval_is_refused_by_name(self, monkeypatch, value) -> None:
        settings = self._settings(monkeypatch, ODDS_INTERVAL_SECONDS=value)
        try:
            assert settings.BAD_SETTINGS, value
            assert "ODDS_INTERVAL_SECONDS" in settings.BAD_SETTINGS[0]
            assert settings.refuse_bad_settings() == 2
            # importable anyway: the refusal happens at the entry point
            assert settings.DEFAULT_INTERVAL_SECONDS == 300
        finally:
            monkeypatch.delenv("ODDS_INTERVAL_SECONDS", raising=False)
            self._settings(monkeypatch)

    def test_an_unparseable_timeout_is_refused_by_name(self, monkeypatch) -> None:
        settings = self._settings(monkeypatch, ODDS_HTTP_TIMEOUT="abc")
        try:
            assert any("ODDS_HTTP_TIMEOUT" in line for line in settings.BAD_SETTINGS)
        finally:
            monkeypatch.delenv("ODDS_HTTP_TIMEOUT", raising=False)
            self._settings(monkeypatch)

    def test_a_good_environment_is_silent(self, monkeypatch) -> None:
        settings = self._settings(monkeypatch, ODDS_INTERVAL_SECONDS="600")
        try:
            assert settings.BAD_SETTINGS == []
            assert settings.refuse_bad_settings() is None
            assert settings.DEFAULT_INTERVAL_SECONDS == 600
        finally:
            monkeypatch.delenv("ODDS_INTERVAL_SECONDS", raising=False)
            self._settings(monkeypatch)

    def test_both_entry_points_refuse_before_acting(self) -> None:
        for path in ("src/collector.py", "src/report.py"):
            text = pathlib.Path(path).read_text()
            body = text[text.index("def main("):]
            head = body[: body.index("\n\n")]
            assert "refuse_bad_settings()" in head, path


class TestShowStatesTheAgeLikeEveryOtherReadCommand:
    """``_resolve_run``'s note carries the run's age and any caveat on it — a
    FAILED verdict, a newer out-of-scope run skipped.  ``show`` built its own
    header and dropped all of it, so it alone printed thirty-six-hour-old prices
    with no age, against this module's own promise that the age is stated on
    every run and not only a stale one."""

    def test_show_prints_the_resolved_note(self, tmp_path, monkeypatch, capsys) -> None:
        """Asserted on the output, not on the source text.

        This used to match the literal string ``print(f"{note}``, which broke the
        moment the call was wrapped across lines to add the jurisdiction — a test
        that fails on reformatting is not testing the promise. The promise is that
        the resolved note reaches the reader.
        """
        import src.collector
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")

        rows = [make_quote(source="fanduel", source_market_id="m")]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(
                datetime.now(UTC), jurisdiction="PA", route_scope="state",
            )
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
                counterparties={},
            )
        assert src.collector.main(["show", "--run", str(run)]) == 0
        out = capsys.readouterr().out
        # The age, which is the whole point of reusing ``_resolve_run``'s note...
        assert "collected" in out, out
        assert f"run {run}" in out, out
        # ...and the jurisdiction, which is the other thing a reader needs.
        assert "[PA]" in out, out


class TestThePageOnlySumsAMarketItPricedCompletely:
    """``src/validation.py`` will only sum a book's own market when nothing is
    missing and every row is active, and says why: "two legs of a three-way
    market sum to less than 1.0 on perfectly good prices, and reporting that as
    'the book prices itself to lose' blames the prices for a missing row."

    The page re-implemented that sum with a bare ``>= 2`` row count.  A
    suspended draw leg — or an exchange with no resting draw offer — turned a
    healthy 3-way into a "2-way" summing to 0.757: rendered as "impossible
    prices: 1, a book pricing itself to lose" in the same strip whose
    "problems found" said 0, and as "smarkets keeps -23.3%" on the bet panel.
    The committed capture holds 221 three-way moneylines.
    """

    def _js(self):
        return pathlib.Path("src/report_assets.py").read_text()

    def test_both_summing_sites_use_the_settlement_rule(self) -> None:
        text = self._js()
        assert text.count("sumsToAMargin(") >= 3, "definition plus both call sites"
        # the quality strip
        strip = text[text.index("const overrounds = [];"):]
        strip = strip[: strip.index("const sums =")]
        assert "sumsToAMargin(" in strip, strip
        assert "if (bySelection.size < 2) continue;" not in strip
        # the bet panel
        panel = text[text.index("const totals = sideSources.map"):]
        panel = panel[: panel.index("}).filter(Boolean);")]
        assert "sumsToAMargin(" in panel, panel
        assert "if (sides.length < 2) return null;" not in panel

    def test_the_rule_reads_the_settlement_table(self) -> None:
        text = self._js()
        rule = text[text.index("function moneylineSides"):]
        rule = rule[: rule.index("\n}")]
        assert "draw_is_priced" in rule
        assert "? 3 : 2" in rule

    def test_the_shipped_page_carries_the_guard(self) -> None:
        """Not just the source of the JS module — the assembled page, which is
        what actually runs in front of the operator."""
        from src.report_assets import JS

        assert "function sumsToAMargin" in JS
        assert "function moneylineSides" in JS


class TestASuspendedRowIsNotAPriceInScope:
    """``latest_run_id``'s scope subquery did not filter on status, so a run
    whose only in-scope rows are suspended counted as holding prices in scope:
    ``arb --league MLB`` resolved it and printed "0 opportunities from 0
    cross-book markets" — the quiet-slate sentence ``_resolve_run`` exists to
    prevent — while the takeable edge sat one ``--run`` away."""

    def test_a_run_of_suspended_rows_does_not_win_resolution(self, tmp_path) -> None:
        from src.schema import QuoteStatus
        from src.store import Store
        from src.validation import ValidationReport

        with Store(tmp_path / "db.sqlite3") as store:
            live = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(live, [make_quote(league="MLB")])
            store.finish_run(live, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
            later = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(later, [make_quote(
                league="MLB", status=QuoteStatus.SUSPENDED,
                event_key="MLB-PHI@MLB-ARI:2026-07-28", away_participant="MLB-PHI",
                home_participant="MLB-ARI",
            )])
            store.finish_run(later, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
            assert store.latest_run_id() == later
            assert store.latest_run_id(leagues=["MLB"]) == live

    def test_the_subquery_filters_on_status(self) -> None:
        text = pathlib.Path("src/store.py").read_text()
        block = text[text.index("def latest_run_id"):]
        block = block[: block.index("\n    def ")]
        assert "q.status = 'active'" in block


class TestASmallSampleCannotConvictAVenue:
    """At a five-market floor, a venue with three markets crossed by half a cent
    — every one *benign* after commission, and printed as such two lines above —
    had its median drag under 1.0 and the run failed with "every price from this
    source is suspect".  A quiet scoped hour is not evidence about a parser."""

    def _slate(self, count, crossed):
        rows = []
        for index in range(count):
            price = 0.4975 if index < crossed else 0.4902
            rows.extend([
                make_quote(source="smarkets", selection=selection,
                           decimal_odds=round(1.0 / price, 6),
                           source_market_id=f"m{index}",
                           event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28")
                for selection in (Selection.HOME, Selection.AWAY)
            ])
        return rows

    def test_five_markets_cannot_convict(self) -> None:
        from src.validation import validate

        report = validate(self._slate(5, 3), order_book_sources={"smarkets"})
        assert "systematic_sub_unity_pricing" not in [f.code for f in report.findings]
        assert report.ok

    def test_the_floor_is_high_enough_to_mean_something(self) -> None:
        from src.validation import MIN_SUB_UNITY_MARKETS

        assert MIN_SUB_UNITY_MARKETS >= 20


class TestTheCommandLineIsTestedAgainstARealPosition:
    """Every CLI-level test was a smoke test, because the committed slate holds
    no arbitrage: 7,372 quotes, 2,087 groups, **0 opportunities**.  So
    ``main(["arb"])`` printed "0 opportunities from N cross-book markets" no
    matter what its arguments were, and a suite-wide mutation audit found the two
    money-path arguments it passes were pinned by nothing at all:

    * ``as_of=None if args.include_started else datetime.now(UTC)`` could be
      replaced by ``as_of=None`` — every game treated as still open — and all
      2,248 tests passed.  ``include_started`` appeared nowhere in the suite.
    * ``one_counterparty=measured_counterparties`` could be replaced by ``None``
      and all 2,248 passed, even though the comment above it records the cost:
      "published a guaranteed +3.00 with both legs at one operator".  The
      identical wiring in ``collect_once`` *is* pinned; only the re-analysis path
      was not.

    So this class stores a run that does contain a position — one leg per side at
    two genuinely distinct books, one fixture in the future and one already
    started — and asserts the numbers the CLI prints about it.
    """

    # Relative to the wall clock, not to a fixed date: the started-game gate is
    # a comparison against ``now``, so a hardcoded 2026-07-28 kickoff is "future"
    # only until the day it is not — which is precisely the trap that makes this
    # gate hard to test and is why it went untested.
    def _kickoff(self):
        return datetime.now(UTC) + timedelta(hours=6)

    def _started(self):
        return datetime.now(UTC) - timedelta(hours=6)

    def _rows(self):
        """A future arb and a started arb, at two genuinely distinct books."""
        rows = []
        for tag, commence in (("FUT", self._kickoff()), ("OLD", self._started())):
            key = f"MLB-{tag}A@MLB-{tag}B:2026-07-28"
            for source, selection, odds in (
                ("pinnacle", Selection.HOME, 2.10),
                ("bovada", Selection.AWAY, 2.10),
            ):
                rows.append(make_quote(
                    source=source, selection=selection, decimal_odds=odds,
                    source_market_id=f"m{tag}", event_key=key,
                    home_participant=f"MLB-{tag}B", away_participant=f"MLB-{tag}A",
                    commence_time=commence,
                ))
        return rows

    def _cli(self, tmp_path, monkeypatch, argv):
        import src.collector
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        rows = self._rows()
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=2),
                counterparties={},
            )
        return src.collector.main(argv)

    def test_a_started_fixture_is_excluded_by_default(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The started-game clock, on the *caller* that supplies it.  Both
        fixtures carry the same 4.76% edge; only the one that has not kicked off
        is a position anybody can take."""
        assert self._cli(tmp_path, monkeypatch, ["arb"]) == 0
        out = capsys.readouterr().out
        assert "1 opportunities" in out or "1 opportunity" in out, out
        assert "MLB-FUTA@MLB-FUTB" in out
        assert "MLB-OLDA@MLB-OLDB" not in out, "a started game is not takeable"

    def test_include_started_says_so_and_reports_both(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        assert self._cli(tmp_path, monkeypatch, ["arb", "--include-started"]) == 0
        out = capsys.readouterr().out
        assert "2 opportunities" in out, out
        assert "MLB-OLDA@MLB-OLDB" in out
        assert "already started" in out, "the caveat has to be on the page"

    def test_the_bankroll_asked_for_is_the_one_used(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``total_stake=args.stake`` was pinned by nothing: dropped, ``arb
        --stake 5000`` still printed ``stake 50.00`` and ``guaranteed +5.00 on
        100``, and all 2,262 tests passed."""
        assert self._cli(tmp_path, monkeypatch, ["arb", "--stake", "5000"]) == 0
        out = capsys.readouterr().out
        assert "on 5000" in out, out
        assert "on 100" not in out

    def test_the_margin_bar_is_read_as_a_percentage(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``min_margin=args.min_margin / 100.0`` was pinned by nothing either.
        Without the conversion ``--min-margin 3`` means 300%, and the two real
        positions on this slate vanish — while the flag's own help says "in
        percent"."""
        assert self._cli(tmp_path, monkeypatch,
                         ["arb", "--include-started", "--min-margin", "3"]) == 0
        kept = capsys.readouterr().out
        assert "2 opportunities" in kept, kept

        # ...and a bar above the real edge does filter them out, so the number is
        # genuinely being compared rather than ignored.
        assert self._cli(tmp_path, monkeypatch,
                         ["arb", "--include-started", "--min-margin", "6"]) == 0
        gone = capsys.readouterr().out
        assert "0 opportunities" in gone, gone

    def test_the_scope_narrows_what_is_reported(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``find_opportunities(quotes, ...)`` reads the *narrowed* rows; passing
        the unfiltered set instead passed all 2,262 tests while printing an ATP
        tennis position under a ``[sport=baseball]`` header."""
        import src.collector
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        rows = self._rows()  # baseball
        rows += [
            make_quote(source=source, selection=selection, decimal_odds=2.10,
                       source_market_id="mT", sport=Sport.TENNIS, league="ATP",
                       event_key="TENNIS-a@TENNIS-b:2026-07-28",
                       home_participant="TENNIS-b", away_participant="TENNIS-a",
                       commence_time=self._kickoff())
            for source, selection in (("pinnacle", Selection.HOME),
                                      ("bovada", Selection.AWAY))
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=3),
                counterparties={},
            )
        assert src.collector.main(["arb", "--sport", "baseball"]) == 0
        out = capsys.readouterr().out
        assert "sport=baseball" in out
        assert "TENNIS" not in out, out

    def test_the_counterparty_gate_reaches_the_re_analysis_path(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``_cmd_arb`` must apply the mirror gate, not just ``collect_once``.
        Two Kambi tenants are one counterparty, so the edge between them is a
        position nobody can hold — and this is the path that reads stored rows,
        where the live pass's measurement is no longer in memory."""
        import src.collector
        import src.settings
        from src.arb import EVERY_LEAGUE
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        rows = [
            make_quote(source=source, selection=selection, decimal_odds=2.10,
                       source_market_id="m", commence_time=self._kickoff())
            for source, selection in (("betrivers_kambi", Selection.HOME),
                                      ("leovegas_kambi", Selection.AWAY))
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
                counterparties={
                    EVERY_LEAGUE: [frozenset({"betrivers_kambi", "leovegas_kambi"})]
                },
            )
        assert src.collector.main(["arb"]) == 0
        out = capsys.readouterr().out
        assert "0 opportunities" in out, out


class TestClusteringSplitsAtTheWidestGap:
    """``cluster_start_times`` walked ascending and broke the moment the span
    bound was exceeded, so the split landed wherever the scan happened to be
    rather than where the listings actually disagree.

    Pinnacle routinely lists a soccer fixture a day early — the tolerance table
    records 18 to 26 hour gaps — so with one early listing and four books
    agreeing to within a minute 30 hours later, the early listing was grouped
    with the *first* of the four and the other three were broken off.  Bovada was
    split from FanDuel, Matchbook and Smarkets, whom it agreed with to the
    minute, and joined to a listing 30 hours away; the halves fell on different
    scheduling dates so no ``#2`` marker appeared either, and a four-book join
    silently became 2+3.
    """

    TOL = timedelta(hours=30)

    def _at(self, day, hour, minute=0):
        return datetime(2026, 8, day, hour, minute, tzinfo=UTC)

    def _clusters(self, times, tolerance=None):
        from src.events import cluster_start_times

        return [
            [t.strftime("%d %H:%M") for t in group]
            for group in cluster_start_times(times, tolerance or self.TOL)
        ]

    def test_the_early_listing_is_the_one_that_splits_off(self) -> None:
        clusters = self._clusters([
            self._at(15, 12), self._at(16, 18), self._at(16, 18, 1),
            self._at(16, 18, 2), self._at(16, 18, 3),
        ])
        assert clusters == [
            ["15 12:00"],
            ["16 18:00", "16 18:01", "16 18:02", "16 18:03"],
        ], clusters

    def test_a_chain_is_still_bounded(self) -> None:
        """The span guarantee the widest-gap rule must not weaken: three books
        stepping along 29 hours at a time are not one fixture."""
        clusters = self._clusters([self._at(15, 0), self._at(16, 5), self._at(17, 10)])
        assert len(clusters) > 1, clusters
        for group in self._clusters([self._at(15, 0), self._at(16, 5), self._at(17, 10)]):
            first, last = group[0], group[-1]
            assert first == last or len(group) <= 2, group

    def test_the_widest_gap_is_found_wherever_it_sits(self) -> None:
        """Not merely "split after the first moment" — which happens to be right
        when the early listing comes first, and wrong the moment it does not.
        Three books agreeing within two minutes, and a fourth 40 hours later:
        breaking at the first gap fragments the three."""
        clusters = self._clusters([
            self._at(16, 18), self._at(16, 18, 1), self._at(16, 18, 2),
            self._at(18, 10),
        ])
        assert clusters == [
            ["16 18:00", "16 18:01", "16 18:02"],
            ["18 10:00"],
        ], clusters

    def test_moments_inside_the_bound_stay_together(self) -> None:
        assert self._clusters([self._at(16, 18), self._at(16, 19), self._at(16, 20)]) == [
            ["16 18:00", "16 19:00", "16 20:00"]
        ]


class TestTheMirrorGateMeasuresOnlyTradeableRows:
    """``src.arb`` keeps only active prices, and the distinctness tables did not
    filter on status — so the gate compared rows no position can be taken at, and
    the suspensions did not merely add noise, they *diluted* the agreement rate.

    Two BetRivers tenants agreeing 20 of 20 on their live moneylines read
    "20/40 identical (50.0%) — DISTINCT" once each tenant's suspensions were
    counted; the gate stayed open and the detector published ``margin 4.76%,
    guaranteed +5.00`` with both legs at BetRivers and no diagnostic.  It is the
    very case ``MIRROR_AGREEMENT_RATE``'s comment cites as the reason its cut is
    0.95 rather than 1.0.
    """

    def _pair(self, live, suspended):
        from src.schema import QuoteStatus

        rows = []
        for index in range(live):
            for source in ("rsiusil", "rsiusnj"):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=2.10,
                    source_market_id=f"m{index}",
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                ))
        for index in range(live, live + suspended):
            for source, odds in (("rsiusil", 2.10), ("rsiusnj", 1.80)):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=odds,
                    source_market_id=f"m{index}", status=QuoteStatus.SUSPENDED,
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                ))
        return rows

    def test_suspended_disagreement_does_not_hide_a_mirror(self) -> None:
        from src.distinctness import compare_sources

        agreement = compare_sources(self._pair(live=20, suspended=20),
                                    "rsiusil", "rsiusnj")
        assert agreement.compared == 20, agreement.summary()
        assert agreement.identical == 20, agreement.summary()
        assert agreement.verdict.blocks_registration, agreement.summary()

    def test_the_gate_closes_on_the_same_rows(self) -> None:
        from src.arb import counterparty_groups

        groups = counterparty_groups(self._pair(live=20, suspended=20))
        assert groups, "the mirror must reach the gate"
        assert any(
            {"rsiusil", "rsiusnj"} == set(group)
            for entries in groups.values()
            for group in entries
        ), groups


class TestASameTimeDoubleheaderDoesNotFuse:
    """Clustering reads the *set* of start times, so a book listing both games of
    a doubleheader at one nominal time collapsed them onto one key.  Their rows
    then collided on ``dedup_key`` — a UNIQUE constraint over that source's whole
    transaction — so the fusion cost the book every unrelated fixture it had
    collected, and every later read command saw the book as absent while real
    money sat in it, under a finding blaming "two prices for one selection".

    A source presenting two different event ids is the only witness that there
    are two fixtures there, so they are kept apart.
    """

    def _rows(self):
        when = datetime(2026, 7, 28, 19, 10, tzinfo=UTC)
        rows = []
        for event_id in ("g1", "g2"):
            for selection in (Selection.HOME, Selection.AWAY):
                rows.append(make_quote(
                    source="bookX", source_event_id=event_id, selection=selection,
                    commence_time=when, source_market_id="m",
                ))
        rows.append(make_quote(
            source="bookY", source_event_id="e9", selection=Selection.HOME,
            commence_time=when, source_market_id="m",
        ))
        return rows

    def test_the_two_games_keep_separate_keys(self) -> None:
        from src.events import reconcile_event_keys

        out, _ = reconcile_event_keys(self._rows())
        by_id = {
            (quote.source, quote.source_event_id): quote.event_key for quote in out
        }
        assert by_id[("bookX", "g1")] != by_id[("bookX", "g2")], by_id

    def test_no_row_collides_on_its_dedup_key(self) -> None:
        """The consequence that made this expensive."""
        from src.events import reconcile_event_keys

        out, _ = reconcile_event_keys(self._rows())
        keys = [quote.dedup_key for quote in out]
        assert len(set(keys)) == len(keys), "a collision costs the source its run"

    def test_no_book_is_joined_to_a_coin_flip(self) -> None:
        """This test previously asserted the opposite, and was wrong to.

        Suffixing only the *extra* ids left the first-sorting id holding the bare
        key, so every other book in the cluster was joined to whichever of the two
        games happened to sort first at that source.  Nothing in the data supports
        that pairing.  Measured on the shape below with the other book pricing
        only the later game: ``margin 13.92%, guaranteed +15.50`` with zero
        diagnostics, pairing game 2 against game 1, while the true comparison of
        the matching games has no edge at all.  Strictly worse than the collision
        this branch replaced — that was expensive but loud; this was invented
        money.

        So a cluster nobody can resolve produces no cross-source comparison, and
        the test that once demanded one now forbids it.
        """
        from src.arb import find_opportunities
        from src.events import reconcile_event_keys

        when = datetime(2026, 7, 28, 19, 10, tzinfo=UTC)
        rows = []
        for event_id, home, away in (("111", 2.10, 1.80), ("222", 1.50, 2.70)):
            for selection, odds in ((Selection.HOME, home), (Selection.AWAY, away)):
                rows.append(make_quote(
                    source="bookX", source_event_id=event_id, selection=selection,
                    decimal_odds=odds, commence_time=when, source_market_id="m",
                ))
        for selection, odds in ((Selection.HOME, 1.55), (Selection.AWAY, 2.60)):
            rows.append(make_quote(
                source="bookY", source_event_id="z9", selection=selection,
                decimal_odds=odds, commence_time=when, source_market_id="m",
            ))
        out, _ = reconcile_event_keys(rows)
        by_key: dict[str, set[str]] = {}
        for quote in out:
            by_key.setdefault(quote.event_key, set()).add(quote.source)
        assert all(len(sources) == 1 for sources in by_key.values()), by_key
        report = find_opportunities(
            out, as_of=datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        )
        assert report.opportunities == [], [
            o.describe() for o in report.opportunities
        ]


class TestMirrorsAndLinesReadTheWholeRun:
    """Third and fourth instances of a defect closed twice already: a gate
    measured on narrowed evidence.  ``mirrors --sport tennis`` answered "only 4
    shared selection(s) — no verdict", exit 0, on the same run whose unscoped
    answer was "24/28 identical (85.7%) — MIRROR", exit 1 — and that exit code is
    the gate a person screens a candidate source with.  ``lines`` reconciled
    *after* filtering, so a scope could merge two clusters or renumber an ordinal
    and key a fixture differently from ``arb`` on the same stored rows."""

    def test_mirrors_compares_the_unfiltered_run(self) -> None:
        text = pathlib.Path("src/collector.py").read_text()
        body = text[text.index("def _cmd_mirrors"):]
        body = body[: body.index("\ndef ")]
        assert "reconcile_event_keys(store.load_quotes(run_id))" in body, body
        assert "load_quotes(run_id, sports=sports, leagues=leagues)" not in body

    def test_lines_reconciles_before_it_narrows(self) -> None:
        text = pathlib.Path("src/collector.py").read_text()
        body = text[text.index("def _cmd_lines"):]
        body = body[: body.index("\ndef ")]
        reconcile = body.index("reconcile_event_keys(store.load_quotes(run_id))")
        narrow = body.index("in_scope(quote, sports, leagues)")
        assert reconcile < narrow, "reconciliation is global; the scope comes after"


class TestFilteringTheMirrorSampleOnlyStrengthensTheCase:
    """Restricting the distinctness sample to tradeable rows fixed a real
    dilution — and introduced the opposite failure, in the costly direction.

    A shrunken sample can fall under ``MIN_SHARED_SELECTIONS``, where the verdict
    becomes UNDECIDED and ``blocks_registration`` is False.  Five selections
    suspended at one tenant took two BetRivers feeds agreeing 24 of 24 from
    "MIRROR" to "only 19 shared selection(s) — no verdict", and the detector then
    published ``margin 4.76%, guaranteed +5.00`` with both legs at BetRivers —
    refused by the measurement this change replaced.  A suspension is not
    evidence of independence.
    """

    def _tenants(self, live, suspended_one_side):
        from src.schema import QuoteStatus

        rows = []
        for index in range(live):
            for source in ("rsiusil", "rsiusnj"):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=2.10,
                    source_market_id=f"m{index}",
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                ))
        for index in range(live, live + suspended_one_side):
            rows.append(make_quote(
                source="rsiusil", selection=Selection.HOME, decimal_odds=2.10,
                source_market_id=f"m{index}",
                event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
            ))
            rows.append(make_quote(
                source="rsiusnj", selection=Selection.HOME, decimal_odds=2.10,
                source_market_id=f"m{index}", status=QuoteStatus.SUSPENDED,
                event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
            ))
        return rows

    def test_a_starved_sample_falls_back_to_the_whole_one(self) -> None:
        from src.distinctness import compare_sources

        # 19 tradeable shared selections is one under the floor
        agreement = compare_sources(
            self._tenants(live=19, suspended_one_side=5), "rsiusil", "rsiusnj"
        )
        assert agreement.verdict.blocks_registration, agreement.summary()
        assert agreement.compared == 24, agreement.summary()

    def test_the_gate_closes_and_no_phantom_is_published(self) -> None:
        from src.arb import counterparty_groups, find_opportunities

        rows = self._tenants(live=19, suspended_one_side=5)
        groups = counterparty_groups(rows)
        assert groups, "the mirror must reach the gate"
        # and the position between the two tenants is refused
        pair = [
            make_quote(source="rsiusil", selection=Selection.OVER, decimal_odds=2.10,
                       market=Market.TOTAL, line=8.5, source_market_id="t"),
            make_quote(source="rsiusnj", selection=Selection.UNDER, decimal_odds=2.10,
                       market=Market.TOTAL, line=8.5, source_market_id="t"),
        ]
        report = find_opportunities(
            rows + pair, as_of=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
            one_counterparty=groups,
        )
        assert report.opportunities == [], [
            o.describe() for o in report.opportunities
        ]

    def test_dilution_is_still_fixed(self) -> None:
        """The direction the filter was added for must still hold: suspended
        *disagreement* cannot hide a mirror either."""
        from src.distinctness import compare_sources
        from src.schema import QuoteStatus

        rows = self._tenants(live=20, suspended_one_side=0)
        for index in range(20, 40):
            for source, odds in (("rsiusil", 2.10), ("rsiusnj", 1.80)):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=odds,
                    source_market_id=f"m{index}", status=QuoteStatus.SUSPENDED,
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                ))
        agreement = compare_sources(rows, "rsiusil", "rsiusnj")
        assert agreement.compared == 20, agreement.summary()
        assert agreement.verdict.blocks_registration, agreement.summary()

    def test_a_genuinely_distinct_pair_is_not_convicted_by_the_fallback(self) -> None:
        """The fallback must not resurrect disagreement into agreement: two books
        that differ on their live prices stay distinct however many suspended
        rows they share."""
        from src.distinctness import compare_sources
        from src.schema import QuoteStatus

        rows = []
        for index in range(25):
            for source, odds in (("bovada", 2.10), ("fanduel", 1.95)):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=odds,
                    source_market_id=f"m{index}",
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                ))
        for index in range(25, 30):
            for source in ("bovada", "fanduel"):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=2.10,
                    source_market_id=f"m{index}", status=QuoteStatus.SUSPENDED,
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                ))
        agreement = compare_sources(rows, "bovada", "fanduel")
        assert not agreement.verdict.blocks_registration, agreement.summary()


class TestTheSplitSuffixDoesNotLeakIntoTheDate:
    """``_slate_dates`` read the scheduling date off the tail of an event key and
    stripped ``#n`` but not the same-time split suffix, so the page's own header
    read "2026-07-28 … 2026-07-28~bookx2"."""

    def test_the_header_holds_dates_only(self) -> None:
        text = pathlib.Path("src/report.py").read_text()
        block = text[text.index("def _slate_dates"):]
        block = block[: block.index("\ndef ")]
        assert 'split("~")[0]' in block, block


class TestEachVenuesKindIsPinnedBecauseItPicksTheRule:
    """A venue's ``kind`` decides which overround rule it is judged by, and it was
    pinned by nothing at all.

    Flipping Matchbook from ``EXCHANGE`` to ``SPORTSBOOK`` passed all 2,262
    tests; so did deleting ``order_book_sources=_order_book_sources(sources)``
    from ``collect_once``.  No test file mentioned ``SourceKind`` or
    ``has_stated_liquidity``, every ``validate(...)`` call passed
    ``order_book_sources`` as a hand-written literal, and the end-to-end fixture
    holds three sportsbooks — so ``_order_book_sources()`` returned an empty set
    in every integration test and the mapping was never read from the registry.

    What the flag decides is money: on an exchange resting 2.02/2.02 against a
    book at 1.60/2.30 the correct answer is one opportunity at 6.51% with no
    diagnostics, and under the sportsbook rule the exchange is deleted from the
    market as pricing itself to lose.  It also flips ``draw_leg_missing`` and
    ``core_market_absent`` from WARNING to ERROR, failing the whole run.
    """

    #: What each venue is.  Written out rather than derived, because deriving it
    #: from the registry is what left it unpinned in the first place.
    EXPECTED = {
        "an_bet365": False,
        "an_betmgm": False,
        "an_betrivers": False,
        "an_caesars": False,
        "an_bovada": False,
        "an_draftkings": False,
        "an_fanduel": False,
        "an_onexbet": False,
        "an_open": False,
        "an_hardrock": False,
        "an_fanatics": False,
        "an_bally": False,
        "an_parx": False,
        "an_unibet": False,
        "an_thescore": False,
        "vi_draftkings": False,
        "vi_caesars": False,
        "vi_hardrock": False,
        "vi_fanatics": False,
        "vi_bet365": False,
        "vi_betmgm": False,
        "vi_fanduel": False,
        "vi_betrivers": False,
        "vsin_circa": False,
        "betmgm": False,
        "betrivers_kambi": False,
        "bovada": False,
        "caesars": False,
        "cloudbet": False,
        "draftkings": False,
        "fanduel": False,
        "hardrock": False,
        "kalshi": True,
        "leovegas_kambi": False,
        "matchbook": True,
        "onexbet": False,
        "pinnacle": False,
        "polymarket": True,
        "smarkets": True,
        "sxbet": True,
    }

    def test_every_registered_venue_is_the_kind_it_is(self) -> None:
        from src.sources import registry

        assert set(registry.keys()) == set(self.EXPECTED), (
            "a venue was added or removed without saying which kind it is — and "
            "the kind decides which overround rule judges its prices"
        )
        for key, stated_liquidity in sorted(self.EXPECTED.items()):
            entry = registry.BY_KEY[key]
            assert entry.kind.has_stated_liquidity is stated_liquidity, (
                f"{key} is registered as {entry.kind.value}"
            )

    def test_the_collector_reads_the_mapping_off_the_registry(self) -> None:
        """Not from a literal: the derivation ``collect_once`` uses must return
        exactly the order-driven venues, for a mixed set of real adapters."""
        from src.collector import _order_book_sources

        class _Named:
            def __init__(self, key):
                self.source_key = key

        mixed = [_Named(key) for key in ("pinnacle", "matchbook", "kalshi", "fanduel")]
        assert _order_book_sources(mixed) == {"matchbook", "kalshi"}
        # an unregistered source is treated as a sportsbook, the stricter reading
        assert _order_book_sources([_Named("some_fake")]) == set()

    def test_an_exchange_is_judged_by_the_exchange_rule_end_to_end(self) -> None:
        """Driven through ``validate`` with the registry's own mapping rather than
        a literal, so a wrong ``kind`` shows up here."""
        from src.collector import _order_book_sources
        from src.validation import Severity, validate

        class _Named:
            def __init__(self, key):
                self.source_key = key

        rows = [
            make_quote(source="matchbook", selection=selection, decimal_odds=2.02,
                       source_market_id="m")
            for selection in (Selection.HOME, Selection.AWAY)
        ]
        order_driven = _order_book_sources([_Named("matchbook")])
        report = validate(rows, order_book_sources=order_driven)
        found = [f for f in report.findings if f.code == "negative_overround"]
        assert found, [f.code for f in report.findings]
        assert found[0].severity is Severity.WARNING, (
            "an exchange resting a tight book is not pricing itself to lose"
        )

    def test_collect_once_actually_wires_the_mapping_through(
        self, tmp_path, monkeypatch
    ) -> None:
        """The *wiring*, not the derivation.  Deleting
        ``order_book_sources=_order_book_sources(sources)`` from ``collect_once``
        passed all 2,262 tests — and my first attempt at pinning this tested the
        helper and the validator separately, so the mutation survived that too.
        This drives the real ``collect_once``."""
        import src.collector
        from src.raw_store import RawStore
        from src.sources.base import ParseOutcome, SourceHealth
        from src.validation import Severity

        rows = [
            make_quote(source="matchbook", selection=selection, decimal_odds=2.02,
                       source_market_id="m")
            for selection in (Selection.HOME, Selection.AWAY)
        ]

        class _Named:
            source_key = "matchbook"
            leagues = ("MLB",)

            def close(self):
                pass

        def _collected(source, **kwargs):
            return (
                SourceHealth(source_key="matchbook", ok=True,
                             checked_at=datetime.now(UTC), quote_count=len(rows),
                             event_count=1),
                ParseOutcome(quotes=rows),
            )

        monkeypatch.setattr(src.collector, "_collect_source", _collected)
        result = src.collector.collect_once(
            [_Named()], raw_store=RawStore(tmp_path / "raw"), store=None,
        )
        found = [
            f for f in result.report.findings if f.code == "negative_overround"
        ]
        assert found, [f.code for f in result.report.findings]
        assert found[0].severity is Severity.WARNING, (
            "the exchange rule has to reach validation through collect_once"
        )

    def test_the_detector_keeps_an_exchange_the_sportsbook_rule_would_delete(self) -> None:
        from src.arb import find_opportunities

        rows = [
            make_quote(source="matchbook", selection=Selection.HOME,
                       decimal_odds=2.02, source_market_id="m"),
            make_quote(source="matchbook", selection=Selection.AWAY,
                       decimal_odds=2.02, source_market_id="m"),
            make_quote(source="fanduel", selection=Selection.HOME,
                       decimal_odds=1.60, source_market_id="m"),
            make_quote(source="fanduel", selection=Selection.AWAY,
                       decimal_odds=2.30, source_market_id="m"),
        ]
        report = find_opportunities(
            rows, as_of=datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        )
        assert "source_prices_itself_to_lose" not in [
            d.code for d in report.diagnostics
        ], [d.detail for d in report.diagnostics]
        assert len(report.opportunities) == 1, [d.code for d in report.diagnostics]


class TestTheReadCommandsArgumentsAreObservedNotAsserted:
    """A caller-mutation sweep found several command arguments that no test
    observes, each guarded — if at all — only by a source-text assertion the
    mutation slips straight past.

    ``_cmd_mirrors`` was the costly one: narrowing the sample it compares turned
    "24/28 identical (85.7%) — MIRROR", exit 1, into "only 4 shared selection(s)
    — no verdict", exit 0, on the same run.  The existing guard asserts that the
    string ``reconcile_event_keys(store.load_quotes(run_id))`` appears and the
    scoped ``load_quotes`` does not; both stay true when the narrowing is applied
    one line later.  And that exit code is the gate a person screens a candidate
    source with.
    """

    def _database(self, tmp_path, monkeypatch, rows):
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
            )
        return run

    def _tenants(self):
        """Two Kambi tenants identical on 24 baseball selections, differing on
        four tennis ones — so a tennis-scoped sample cannot reach the floor."""
        rows = []
        for index in range(24):
            for source in ("betrivers_kambi", "leovegas_kambi"):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=2.10,
                    source_market_id=f"m{index}",
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                    # Participants per fixture: reconciliation rebuilds the event
                    # key from these, so leaving the defaults collapses every row
                    # onto one key and the sample never reaches the floor.
                    away_participant="MLB-PHI", home_participant=f"MLB-MI{index}",
                ))
        for index in range(4):
            for source, odds in (("betrivers_kambi", 2.10), ("leovegas_kambi", 1.80)):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=odds,
                    source_market_id=f"t{index}", sport=Sport.TENNIS, league="ATP",
                    event_key=f"TENNIS-a{index}@TENNIS-b{index}:2026-07-28",
                    home_participant=f"TENNIS-b{index}",
                    away_participant=f"TENNIS-a{index}",
                ))
        return rows

    def test_mirrors_gives_the_same_verdict_under_a_scope(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        import src.collector

        self._database(tmp_path, monkeypatch, self._tenants())
        assert src.collector.main(["mirrors"]) == 1
        unscoped = capsys.readouterr().out
        assert src.collector.main(["mirrors", "--sport", "tennis"]) == 1, (
            "the verdict, and the exit code that gates a registry decision, must "
            "not depend on what the operator asked to see"
        )
        scoped = capsys.readouterr().out
        for text in (unscoped, scoped):
            assert "MIRROR" in text, text
        assert "no verdict" not in scoped, scoped

    def test_show_honours_its_row_limit(self, tmp_path, monkeypatch, capsys) -> None:
        """``--limit`` was ignored and nothing noticed."""
        import src.collector

        rows = [
            make_quote(source="pinnacle", selection=Selection.HOME, decimal_odds=2.10,
                       source_market_id=f"m{index}",
                       event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28")
            for index in range(8)
        ]
        self._database(tmp_path, monkeypatch, rows)
        assert src.collector.main(["show", "--limit", "3"]) == 0
        out = capsys.readouterr().out
        assert "showing 3 rows" in out, out

    def test_an_interrupted_run_is_marked_apart_from_a_failed_one(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``state = "?" if finished_at is None else str(bool(ok))`` — collapsing
        it to the verdict reinstates verbatim the regression its own comment says
        was fixed: an interrupted pass reads as "the checks ran and found
        problems"."""
        import src.collector
        import src.settings
        from src.store import Store

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        with Store(tmp_path / "db.sqlite3") as store:
            store.start_run(datetime.now(UTC))  # never finished
        src.collector.main(["runs"])
        out = capsys.readouterr().out
        assert "never finished" in out, out
        assert "?" in out.split("\n")[1], out

    def test_the_coverage_bar_needs_two_books_on_one_fixture(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``min_books=MIN_HEALTHY_SOURCES`` → 1 made two books on completely
        disjoint slates read "usable".  The guard was an OR over three mutually
        exclusive labels, which cannot fail while any row prints."""
        import src.collector

        rows = []
        for index, source in enumerate(("pinnacle", "bovada")):
            rows.append(make_quote(
                source=source, selection=Selection.HOME, decimal_odds=2.10,
                source_market_id="m", sport=Sport.HOCKEY, league="NHL",
                event_key=f"NHL-a{index}@NHL-b{index}:2026-07-28",
                home_participant=f"NHL-b{index}", away_participant=f"NHL-a{index}",
            ))
        self._database(tmp_path, monkeypatch, rows)
        src.collector.main(["runs"])
        out = capsys.readouterr().out
        hockey = next(line for line in out.splitlines() if "hockey" in line)
        assert "NO OVERLAP" in hockey, hockey
        assert "usable" not in hockey, hockey


class TestTheDefaultSubcommandAndTheReportEntryPointAreExecuted:
    """``_cmd_collect`` — the default subcommand, the one an operator runs — was
    executed by **no test**: a bare ``raise AssertionError`` as its first
    statement left all 2,262 passing.  So ``build_sources``, ``resolve_leagues``
    and the slow-source ordering were named by no test at all, and inverting that
    ordering — which the registry records as having cost Kalshi and Polymarket
    847 comparable markets between them — also passed.

    ``src.report.main``'s render path was in the same state: reached only on two
    failure paths, so the ``--runs``/``--quote-runs``/``--max-quote-rows``
    wiring, ``_replay_note``, ``replay_run_id`` and ``--fragment`` were all
    unobserved and a swapped keyword was invisible.
    """

    def test_slow_sources_are_ordered_last(self) -> None:
        """Where a rate-limited source sits decides how many others it pushes out
        of the comparison window."""
        from src.collector import build_sources
        from src.sources import registry

        assert registry.SLOW_SOURCES, "the ordering means nothing with none named"
        built = build_sources(None)
        try:
            keys = [source.source_key for source in built]
        finally:
            for source in built:
                source.close()
        slow_positions = [
            index for index, key in enumerate(keys) if key in registry.SLOW_SOURCES
        ]
        fast_positions = [
            index for index, key in enumerate(keys)
            if key not in registry.SLOW_SOURCES
        ]
        assert slow_positions and fast_positions, keys
        assert min(slow_positions) > max(fast_positions), keys

    def test_collect_runs_end_to_end_without_the_network(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``_cmd_collect`` itself, driven with ``--no-store`` and every fetch
        replaced, so the command body — argument wiring, source construction,
        summary — is executed rather than merely imported."""
        import src.collector
        import src.settings
        import src.state_selection
        from src.egress import detection_from_payload
        from src.state_selection import StateSelection
        from src.sources.base import ParseOutcome, SourceHealth

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        detected = detection_from_payload({"ip": "203.0.113.55", "region_code": "IL"})
        monkeypatch.setattr(
            src.state_selection,
            "detect_and_select",
            lambda states: StateSelection(detected, ("IL",), "test"),
        )

        rows = [
            make_quote(source="pinnacle", selection=Selection.HOME,
                       decimal_odds=2.10, source_market_id="m"),
            make_quote(source="bovada", selection=Selection.AWAY,
                       decimal_odds=2.10, source_market_id="m"),
        ]

        def _collected(source, **kwargs):
            key = source.source_key
            mine = [row for row in rows if row.source == key]
            return (
                SourceHealth(source_key=key, ok=True, checked_at=datetime.now(UTC),
                             quote_count=len(mine), event_count=1 if mine else 0),
                ParseOutcome(quotes=mine),
            )

        monkeypatch.setattr(src.collector, "_collect_source", _collected)
        code = src.collector.main(["collect", "--no-store", "--sport", "baseball"])
        out = capsys.readouterr().out
        assert code in (0, 1), out
        assert "pinnacle" in out or "arbitrage" in out or "validation" in out, out

    def test_the_report_entry_point_writes_a_page(self, tmp_path, monkeypatch) -> None:
        """``src.report.main`` with its real argument wiring, on a stored run."""
        import src.report
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
        out = tmp_path / "dash.html"
        code = src.report.main([
            "--out", str(out), "--no-replay-check",
            "--runs", "5", "--quote-runs", "1", "--max-quote-rows", "50",
        ])
        assert code == 0
        page = out.read_text()
        assert "report-data" in page
        assert page.lstrip().startswith("<!"), "a whole document, not a fragment"

    def test_the_fragment_flag_writes_a_fragment(self, tmp_path, monkeypatch) -> None:
        import src.report
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
        out = tmp_path / "fragment.html"
        assert src.report.main(
            ["--out", str(out), "--no-replay-check", "--fragment"]
        ) == 0
        assert not out.read_text().lstrip().startswith("<!")


class TestTheRunTimingsOnThePageAreTheDatabasesOwn:
    """``total_latency_ms`` and ``duration_ms`` both render on the page and
    neither string appeared anywhere in the suite: ``sum(...)`` → ``max(...)``
    passed all 2,262 tests.  A wrong total reads as a fast collection."""

    def test_the_latency_total_is_a_sum_of_every_source(self, tmp_path) -> None:
        from src.report import build_report
        from src.sources.base import SourceHealth
        from src.store import Store
        from src.validation import ValidationReport

        latencies = [120.0, 400.0, 35.5]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC))
            for index, latency in enumerate(latencies):
                store.save_health(run, SourceHealth(
                    source_key=f"book{index}", ok=True,
                    checked_at=datetime.now(UTC), quote_count=1, event_count=1,
                    latency_ms=latency,
                ))
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=datetime.now(UTC),
                             report=ValidationReport(quote_count=1, event_count=1))
            data = build_report(store)
        assert data["runs"][0]["total_latency_ms"] == pytest.approx(sum(latencies))

    def test_the_duration_is_the_stored_span(self, tmp_path) -> None:
        from src.report import build_report
        from src.store import Store
        from src.validation import ValidationReport

        started = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(started)
            store.save_quotes_by_source(run, [make_quote()])
            store.finish_run(run, finished_at=started + timedelta(seconds=42),
                             report=ValidationReport(quote_count=1, event_count=1))
            data = build_report(store)
        assert data["runs"][0]["duration_ms"] == pytest.approx(42_000, abs=1)


class TestATransientGatewayErrorKeepsItsRetry:
    """``_BLOCK_MARKERS`` mixed real refusals with CDN *branding* —
    ``cloudflare``, ``reference #``, ``akamai reference`` — and the marker scan
    runs before the status check.  Those strings sit in the footer of every page
    a CDN generates, including its 502/503/504/429 interstitials.

    So every transient gateway error from a Cloudflare- or Akamai-fronted host was
    classified ``blocked``: one request where three were budgeted, a
    ``Retry-After`` ignored on 429s, and a health row saying the venue *denied
    access* — the reading that stops the source being asked at all — when it had
    merely had a bad minute or we were going too fast.  It is the most common
    failure against public endpoints, and it contradicted the non-finite-JSON
    branch, which already argues that "the status still decides first".
    """

    CF = ("<html><body><h1>502 Bad Gateway</h1><p>Reference #1.abc</p>"
          "<footer>Cloudflare</footer></body></html>")
    CF_RATE = ("<html><body>Error 1015 You are being rate limited"
               "<footer>Cloudflare</footer></body></html>")
    AKAMAI = ("<html><body>Service Unavailable<p>Akamai Reference #18.x</p>"
              "</body></html>")
    REFUSED = ("<html><body>You have been blocked<footer>Cloudflare</footer>"
               "</body></html>")

    def _raise(self, body, status, retry_after=None):
        from src.sources.guards import check_http_response

        try:
            check_http_response(
                source="x", endpoint="e", status_code=status, body=body,
                content_type="text/html", retry_after=retry_after,
            )
        except Exception as exc:  # noqa: BLE001
            return exc
        return None

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_cdn_fronted_server_error_is_retryable(self, status) -> None:
        error = self._raise(self.CF, status)
        assert error is not None
        assert error.kind == "server_error", f"{type(error).__name__}: {error}"
        assert error.retryable is True

    def test_an_akamai_server_error_is_retryable(self) -> None:
        error = self._raise(self.AKAMAI, 503)
        assert error.kind == "server_error"
        assert error.retryable is True

    def test_a_cdn_fronted_rate_limit_keeps_its_retry_after(self) -> None:
        error = self._raise(self.CF_RATE, 429, retry_after="2")
        assert error.kind == "rate_limited", f"{type(error).__name__}: {error}"
        assert error.retryable is True
        assert error.retry_after == pytest.approx(2.0)

    def test_a_cdn_fronted_geo_block_is_still_geo(self) -> None:
        assert self._raise(self.CF, 451).kind == "geo_restricted"

    @pytest.mark.parametrize("status", [200, 400, 404, 410])
    def test_branding_alone_is_a_refusal_when_the_status_is_silent(
        self, status
    ) -> None:
        """The direction that must not regress.  ``_refusal_for_status`` names
        only the statuses that mean something specific — 401/403 blocked, 451
        geo, 429 rate-limited — so a 200, 400, 404 or 410 carrying nothing but a
        CDN footer has the footer as its only evidence.

        My first version of this test used 200 and 403, and 403 is decided by
        status alone: deleting the 4xx branding consult entirely left it green.
        These are the statuses that actually depend on it.
        """
        assert self._raise(self.CF, status).kind == "blocked"

    def test_a_body_that_says_it_was_refused_is_still_blocked(self) -> None:
        assert self._raise(self.REFUSED, 403).kind == "blocked"
        assert self._raise(self.REFUSED, 200).kind == "blocked"

    def test_the_client_still_retries_a_cdn_fronted_five_hundred(self) -> None:
        """End to end through the real client, not just the classifier."""
        import httpx

        from src.sources._common import SourceClient
        from src.sources.guards import SourceError

        seen = []

        def _handler(request):
            seen.append(request.url)
            return httpx.Response(502, text=self.CF,
                                  headers={"content-type": "text/html"})

        client = SourceClient(
            "x",
            client=httpx.Client(transport=httpx.MockTransport(_handler)),
            host_interval=0.0,
            sleep=lambda _seconds: None,
        )
        try:
            with pytest.raises(SourceError) as raised:
                client.get("https://example.invalid/a", endpoint="e")
        finally:
            client.close()
        assert raised.value.kind == "server_error"
        assert len(seen) > 1, "a transient gateway error must be retried"


def _grade_scope_refusals(source):
    """Run the collector's own scope grading over one adapter's tally.

    Built the way ``_collect_one`` builds it — ``failed_scopes`` and
    ``scopes_requested`` read straight off ``last_fetch`` — so the arithmetic
    under test is the arithmetic that runs.  ``event_count`` is deliberately
    non-zero: the grade has a second clause for a source that went entirely
    quiet, and leaving it at zero would make every one of these ERROR without
    the share ever being consulted.
    """
    from src.collector import _check_source_health
    from src.sources.base import SourceHealth
    from src.validation import ValidationReport

    tally = source.last_fetch
    health = SourceHealth(
        source_key=source.source_key,
        ok=True,
        checked_at=datetime.now(UTC),
        event_count=1,
        quote_count=1,
        scopes_requested=int(tally.scopes_requested),
        scopes_failed=int(tally.scopes_failed),
        truncated_scopes=tuple(tally.truncated_scopes),
        failed_scopes=tuple(tally.failed_scopes),
    )
    report = ValidationReport()
    _check_source_health(
        [health], report, configured=[source.source_key], expected=None
    )
    return report


class TestASelfImposedPageCapIsReported:
    """Five adapters ended their paging loop by falling off ``range(1, CAP + 1)``
    with the venue's cursor still live and reported the scope as fully produced.
    ``CoverageCappedError`` exists for exactly this — "a bound on request volume
    is politeness and stays; reporting the run as complete afterwards is not" —
    and only Pinnacle's league-index fallback used it.

    Not hypothetical: the committed Polymarket fixtures were captured *at* the
    cap, page 2 holding 20 events against ``limit: 20``, so page 3 was never
    asked for.  Every unfetched page is an arb nobody can see, on a run that
    says OK.

    Round 28 fixed that and reported it with a scope name it made up:
    ``failed("mlb: stopped at the 2-page cap", …)``.  ``ScopeTally.failed``
    *registers* the name it is handed — deliberately, so a scope that fails
    before it is requested still counts — so every cap invented a scope the book
    was never asked for and put it in the numerator and the denominator at once.
    Polymarket with both of its scopes truncated then computed ``share_lost =
    2/4 = 0.5`` and graded a WARNING reading "was refused 2 of the scopes it
    asked for **and returned the rest**".  It returned none of the rest; the
    share lost was 1.0 and the grade is ERROR.

    Asserted by *running* the adapters against a transport that never stops
    handing back pages.  The two tests these replace read the source text for
    ``"CoverageCappedError"`` and for ``last_fetch.failed(`` within 600
    characters of it — both of which the broken code matched.
    """

    PAGING_ADAPTERS = ("polymarket", "smarkets", "sxbet", "kalshi", "matchbook")

    @staticmethod
    def _unpaced(source):
        """Drop the politeness delay for one adapter instance.

        These adapters share a process-wide :class:`HostPacer`, and Kalshi and
        Smarkets raise it further still (0.6 s and 3.1 s per request), so a cap
        of ten pages is half a minute of real sleeping for a test that asserts
        accounting.  The pacing itself is asserted in ``test_source_client``,
        against the pacer rather than through an adapter.
        """
        from src.sources._common import HostPacer

        source._http.host_interval = 0.0
        source._http._pacer = HostPacer(0.0)
        return source

    @staticmethod
    def _endless(module: str):
        """One adapter wired to a transport whose pages never run out, plus the
        scope names the truncation must be filed under.

        Page sizes are set to the smallest thing that still pages, so the caps
        are reached in a few requests rather than a few hundred; the pacing
        interval is dropped for the same reason — this asserts accounting, and
        the politeness delay is asserted where it belongs, in
        ``test_source_client``.
        """
        import httpx

        future = int(
            (datetime.now(UTC) + timedelta(days=2)).timestamp()
        )

        if module == "polymarket":
            from src.sources.polymarket import PolymarketAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                tag = request.url.params.get("tag_slug", "")
                limit = int(request.url.params.get("limit", "2"))
                # Always exactly full — including the one-row probe past the
                # cap, which is what makes this a truncation and not a slate
                # that happened to end on the boundary.
                return httpx.Response(200, json=[
                    {"id": f"{tag}{index}", "slug": f"{tag}-{index}", "title": "x",
                     "startTime": "2026-08-01T18:00:00Z", "markets": []}
                    for index in range(limit)
                ])

            source = PolymarketAdapter(
                ["MLB", "NFL"], page_size=2,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return TestASelfImposedPageCapIsReported._unpaced(source), {"mlb", "nfl"}

        if module == "smarkets":
            from src.sources.smarkets import SmarketsAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                if not request.url.path.rstrip("/").endswith("events"):
                    return httpx.Response(200, json={"markets": []})
                return httpx.Response(200, json={
                    "events": [
                        {"id": str(1000 + index), "name": f"A{index} vs B{index}",
                         "state": "upcoming", "bettable": True,
                         "start_datetime": "2026-08-01T18:00:00Z",
                         "full_slug": f"/sport/football/epl/2026/08/01/18-00/a{index}"}
                        for index in range(2)
                    ],
                    "pagination": {"next_page": "?state=upcoming&pagination_last_id=9"},
                })

            source = SmarketsAdapter(
                ["EPL"], page_size=2,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return TestASelfImposedPageCapIsReported._unpaced(source), {"soccer"}

        if module == "sxbet":
            from src.sources.sxbet import SxBetAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/orders"):
                    return httpx.Response(200, json={"data": []})
                return httpx.Response(200, json={"data": {
                    "markets": [{
                        "marketHash": f"0x{request.url.params.get('paginationKey', '0')}",
                        "sportId": 3, "type": 226, "leagueLabel": "MLB",
                        "status": "ACTIVE", "gameTime": future,
                    }],
                    "nextKey": "more",
                }})

            source = SxBetAdapter(
                ["MLB"], client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return TestASelfImposedPageCapIsReported._unpaced(source), {"baseball"}

        if module == "kalshi":
            from src.sources.kalshi import KalshiAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    "markets": [{"ticker": f"T{index}"} for index in range(2)],
                    "cursor": "more",
                })

            source = KalshiAdapter(
                ["MLB"], page_size=2,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return (
                TestASelfImposedPageCapIsReported._unpaced(source),
                {entry.ticker for entry in source.series},
            )

        from src.sources.matchbook import MatchbookAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/navigation"):
                return httpx.Response(200, json=[{"id": 1, "name": "x"}])
            return httpx.Response(200, json={
                "events": [{"id": index, "name": f"A{index} vs B{index}"}
                           for index in range(2)],
                "total": 10_000,
            })

        source = MatchbookAdapter(
            ["MLB"], per_page=2,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        return TestASelfImposedPageCapIsReported._unpaced(source), {"baseball"}

    @pytest.mark.parametrize("module", PAGING_ADAPTERS)
    def test_the_cap_is_filed_under_the_scope_that_was_actually_truncated(
        self, module
    ) -> None:
        """The scope name, not a sentence about the scope.

        Two assertions, and the second is the one that was wrong: the names
        filed must be the *configured* scopes, and filing them must not have
        changed how many scopes the book was asked for.
        """
        source, expected = self._endless(module)
        try:
            source.fetch_raw()
        finally:
            source.close()

        tally = source.last_fetch
        filed = {entry.split(":", 1)[0] for entry in tally.truncated_scopes}
        assert filed == expected, tally.truncated_scopes

    @pytest.mark.parametrize("module", PAGING_ADAPTERS)
    def test_a_truncated_scope_is_not_reported_as_a_refused_one(self, module) -> None:
        """A cap is a third state, and it must not be graded as the second.

        Round 29 fixed the invented scope name by filing the cap under the real
        one — through ``failed()``, which is what grades ``scopes_refused``.  But
        every one of these adapters calls ``produced()`` on the same name a
        moment later, so the scope landed in ``scopes_with_data`` *and* in
        ``failed_scopes``, and only the second was graded.  Polymarket handing
        over 41 MLB events and stopping at its own two-page cap was reported as

            was refused 1 of the 1 scope(s) it asked for and returned none of
            the rest … which is a broken feed rather than a quiet league

        at ERROR, with ``report.ok = False`` and a non-zero exit code, on a run
        that had collected 2,393 rows from ten venues.  Nothing was refused.

        A cap leaves behind a fraction of *one* scope, of unknown size.  The
        count of scopes cannot express that, so it does not try: the truncation
        is reported on its own line and stays out of the share arithmetic.
        """
        from src.validation import Severity

        source, expected = self._endless(module)
        try:
            source.fetch_raw()
        finally:
            source.close()

        tally = source.last_fetch
        assert tally.failed_scopes == [], "a cap is not a refusal"
        assert tally.scopes_failed == 0

        report = _grade_scope_refusals(source)
        codes = [f.code for f in report.findings]
        assert "scopes_refused" not in codes, codes
        finding = next(f for f in report.findings if f.code == "scopes_truncated")
        assert finding.severity is Severity.WARNING, finding.message
        assert "refused" not in finding.message

    @pytest.mark.parametrize("module", PAGING_ADAPTERS)
    def test_a_cap_does_not_move_the_denominator(self, module) -> None:
        """The round-28 defect this class was opened for, still fixed.

        ``failed()`` registers the name it is handed, so a decorated one — "mlb:
        stopped at the 2-page cap" — was a scope the book was never asked for,
        in the numerator and the denominator at once.  ``truncated()`` registers
        nothing at all, which is the same guarantee by a shorter route.
        """
        source, expected = self._endless(module)
        try:
            source.fetch_raw()
        finally:
            source.close()

        tally = source.last_fetch
        filed = {entry.split(":", 1)[0] for entry in tally.truncated_scopes}
        assert filed == expected, tally.truncated_scopes
        assert tally.scopes_requested == len(expected), (
            f"{module} invented {tally.scopes_requested - len(expected)} scope(s) "
            f"by reporting its cap"
        )


class TestPolymarketAsksWhetherItTruncatedRatherThanAssuming:
    """The four other paging adapters can *know* they stopped early, because the
    venue hands back a cursor and it is still live at the cap.  Gamma's
    ``/events`` is offset-paged: no cursor, no total, no more-pages flag.  A last
    page that comes back exactly full is the same bytes whether ten events follow
    it or none.

    Round 28 reported truncation there as fact anyway — "so more events existed
    upstream and were not collected" — from evidence that cannot support it, and
    the committed captures are exactly the ambiguous shape (20 events against
    ``limit: 20``), so the claim fired on essentially every run.  A finding that
    is wrong most of the time is a finding an operator learns to scroll past,
    which costs the runs where it was right.

    One row settles it: ask for a single event past the cap.  That is what these
    three cases are — it is there, it is not there, and the question could not be
    asked.
    """

    @staticmethod
    def _adapter(handler):
        import httpx

        from src.sources.polymarket import PolymarketAdapter

        source = PolymarketAdapter(
            ["MLB"], page_size=2,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        return TestASelfImposedPageCapIsReported._unpaced(source)

    @staticmethod
    def _events(count: int):
        return [
            {"id": f"e{index}", "slug": f"mlb-{index}", "title": "x",
             "startTime": "2026-08-01T18:00:00Z", "markets": []}
            for index in range(count)
        ]

    def test_a_slate_that_ends_on_the_cap_boundary_is_not_a_truncation(self) -> None:
        """Every permitted page exactly full and nothing after them.  Nothing was
        lost, so nothing is reported — this is the run the old code faulted."""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            limit = int(request.url.params.get("limit", "2"))
            offset = int(request.url.params.get("offset", "0"))
            # Four events, in two pages of two, and the slate stops there.
            return httpx.Response(200, json=self._events(max(0, min(limit, 4 - offset))))

        source = self._adapter(handler)
        try:
            source.fetch_raw()
        finally:
            source.close()
        assert source.last_fetch.truncated_scopes == []

    def test_a_slate_with_one_more_event_past_the_cap_is_a_truncation(self) -> None:
        """The same bytes for pages one and two, one event different past them,
        and the opposite verdict.  Nothing except the probe distinguishes these
        two tests, which is the point."""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            limit = int(request.url.params.get("limit", "2"))
            return httpx.Response(200, json=self._events(limit))

        source = self._adapter(handler)
        try:
            source.fetch_raw()
        finally:
            source.close()
        cut = source.last_fetch.truncated_scopes
        assert [s.split(":", 1)[0] for s in cut] == ["mlb"]
        assert "still had events past offset 4" in cut[0]
        assert source.last_fetch.failed_scopes == []

    def test_the_probe_is_kept_rather_than_thrown_away(self) -> None:
        """It is a page of the slate that was paid for.  Discarding it because it
        was fetched for a different reason is the "gathered locally, handed over
        only on success" mistake ``into=`` exists to prevent."""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            limit = int(request.url.params.get("limit", "2"))
            return httpx.Response(200, json=self._events(limit))

        source = self._adapter(handler)
        try:
            raws = source.fetch_raw()
        finally:
            source.close()
        assert "events:mlb:03" in {raw.endpoint for raw in raws}

    def test_a_refused_probe_leaves_the_question_open_and_says_so(self) -> None:
        """The one thing that could settle it was refused, so the slate cannot be
        called complete — and cannot be called truncated either.  Reported in
        those words rather than as "more events existed", which is not what was
        established."""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            limit = int(request.url.params.get("limit", "2"))
            if limit == 1:
                return httpx.Response(429, json={"error": "slow down"})
            return httpx.Response(200, json=self._events(limit))

        source = self._adapter(handler)
        try:
            source.fetch_raw()
        finally:
            source.close()
        unresolved = source.last_fetch.truncated_scopes
        assert [s.split(":", 1)[0] for s in unresolved] == ["mlb"]
        assert "cannot be called complete" in unresolved[0]
        assert "more events existed" not in unresolved[0]

    def test_a_non_list_probe_is_not_handed_to_parse(self) -> None:
        """The probe answered 200 with an error envelope.  That settles nothing
        about whether more events follow, so the run is truncated — and the
        envelope must not be kept among the pages ``parse_polymarket`` will
        ``require_list`` over, or the two full pages already collected are
        thrown away with the whole source.
        """
        import httpx

        from src.sources.polymarket import parse_polymarket

        def handler(request: httpx.Request) -> httpx.Response:
            limit = int(request.url.params.get("limit", "2"))
            if limit == 1:
                return httpx.Response(200, json={"error": "temporary upstream response"})
            return httpx.Response(200, json=self._events(limit))

        source = self._adapter(handler)
        try:
            raws = source.fetch_raw()
            cut = source.last_fetch.truncated_scopes
        finally:
            source.close()
        assert [s.split(":", 1)[0] for s in cut] == ["mlb"]
        assert "not a list of events" in cut[0]
        assert "events:mlb:03" not in {raw.endpoint for raw in raws}
        # FormatChangeError is the pre-fix failure: require_list on the
        # retained envelope.  Getting past parse at all is the pin.
        parse_polymarket(raws)


class TestARefusedSubRequestIsRecordedNotSwallowed:
    """FanDuel's per-event soccer hop caught **every** ``SourceError`` and only
    logged it.  The comment justified that for one benign case — a fixture
    postponed between the slate call and the detail call answers without an
    ``events`` attachment, a ``FormatChangeError`` — but the clause caught 403s,
    rate limits and timeouts too, and this hop is 126 of the 133 requests a full
    pass makes.

    So a refusal across the whole loop left ``ok=True``, ``failed_scopes``
    empty and ``scopes_refused`` silent, while ``capabilities(FULL)`` still
    declared spread and total.  The total loss then surfaced only as
    ``core_market_absent``, whose message blames a renamed source label and sends
    an operator after a parser change; the partial loss surfaced as two warnings
    the code documents as firing on every normal run.

    Round 28 recorded them, one scope per refused hop —
    ``failed(f"{page_key}:event:{event_id}", …)`` — and ``ScopeTally.failed``
    registers the name it is handed, so the denominator grew in step with the
    numerator: 12 refusals among 126 optional hops became 12 of 18 scopes lost,
    graded ERROR, ``report.ok = False``, on a pass that collected 99% of what it
    asked for.  A check that cries wolf on a good run is not a stricter check.

    ``page_key`` in that name was also a leaked loop variable from the slate
    loop above, right only because ``"soccer"`` is last in ``PAGES``.

    The hop is one scope, in the same unit as the slate pages beside it, and how
    much of it was lost is stated in the message.  The two tests this replaces
    were greps — one for ``"tally.failed("`` appearing somewhere before a
    ``return``, one for the buggy f-string *verbatim*, which pinned the defect
    rather than the behaviour.
    """

    @staticmethod
    def _run(*, refuse: int = 0, postpone: int = 0, fixtures: int = 20):
        """A whole FanDuel pass in which *refuse* of the soccer detail hops are
        answered 403 and *postpone* of them come back without an ``events``
        attachment, which is the shape of a fixture postponed between the slate
        call and the detail call."""
        import httpx

        from src.sources._common import HostPacer, RetryPolicy
        from src.sources.fanduel import FanDuelAdapter

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("event-page"):
                event_id = request.url.params.get("eventId", "")
                seen.append(event_id)
                index = int(event_id.removeprefix("e"))
                if index < refuse:
                    return httpx.Response(403, json={"error": "blocked"})
                if index < refuse + postpone:
                    # No ``events`` attachment: a FormatChangeError, and the one
                    # shape this loop is allowed to swallow.
                    return httpx.Response(200, json={"attachments": {"markets": {}}})
                return httpx.Response(200, json={"attachments": {"events": {
                    event_id: {"name": "A v B", "openDate": "2026-08-01T18:00:00Z"}
                }}})
            if request.url.params.get("page") != "SPORT":
                # A US league page: enough of a slate to pass _require_slate.
                return httpx.Response(200, json={"attachments": {
                    "events": {"u1": {"name": "Away @ Home"}},
                    "markets": {"m1": {"eventId": "u1", "marketType": "MONEY_LINE"}},
                }})
            return httpx.Response(200, json={"attachments": {
                "events": {
                    f"e{index}": {
                        "name": f"A{index} v B{index}",
                        "competitionId": "c1",
                        "openDate": f"2026-08-01T{index % 24:02d}:00:00Z",
                    }
                    for index in range(fixtures)
                },
                "competitions": {"c1": {"name": "English Premier League"}},
                "markets": {
                    f"m{index}": {"eventId": f"e{index}", "marketType": "WIN-DRAW-WIN"}
                    for index in range(fixtures)
                },
            }})

        source = FanDuelAdapter(
            client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        source._http._pacer = HostPacer(0.0)
        source._http.retry = RetryPolicy(attempts=1)
        try:
            source.fetch_raw()
        finally:
            source.close()
        return source, seen

    def test_a_refused_hop_is_still_recorded(self) -> None:
        """The guarantee round 28 added, kept: a 403 across this loop must not
        leave the instrumentation empty.  Partial loss is a truncation — some
        detail pages landed — not a refusal of the whole ``soccer:detail``
        scope."""
        source, seen = self._run(refuse=3)
        assert seen, "no detail hop was attempted"
        assert source.last_fetch.failed_scopes == []
        assert len(source.last_fetch.truncated_scopes) == 1
        assert "3 of 20" in source.last_fetch.truncated_scopes[0]

    def test_losing_every_detail_hop_is_a_refusal(self) -> None:
        """When nothing lands, the enrichment scope itself was refused."""
        source, seen = self._run(refuse=20)
        assert len(seen) == 20
        assert len(source.last_fetch.failed_scopes) == 1
        assert "all 20" in source.last_fetch.failed_scopes[0]
        assert source.last_fetch.truncated_scopes == []

    def test_a_postponed_fixture_is_still_not_a_refusal(self) -> None:
        """The one benign shape the loop is allowed to swallow.  It is an
        *addition* to a slate that already succeeded, so it costs that fixture's
        handicaps and nothing else."""
        source, _ = self._run(postpone=4)
        assert source.last_fetch.failed_scopes == []
        assert source.last_fetch.truncated_scopes == []

    def test_every_detail_hop_unavailable_is_still_reported(self) -> None:
        """Four postponed fixtures stay quiet; all twenty of them used to as
        well, and FanDuel soccer then carried no handicaps with a clean health
        row."""
        source, seen = self._run(postpone=20)
        assert len(seen) == 20
        assert source.last_fetch.failed_scopes == []
        assert len(source.last_fetch.truncated_scopes) == 1
        assert "all 20" in source.last_fetch.truncated_scopes[0]
        assert "without events" in source.last_fetch.truncated_scopes[0]

    def test_a_mixed_unavailable_detail_slate_is_not_called_all_refused(self) -> None:
        """One 403 and nineteen postponed pages is not ``all 20 were refused``.

        ``refusals and not got`` treated any non-empty refusal list with zero
        landings as a total refusal, so a single blocked hop among postponed
        fixtures put ``soccer:detail`` in the share-lost numerator.
        """
        source, seen = self._run(refuse=1, postpone=19)
        assert len(seen) == 20
        assert source.last_fetch.failed_scopes == []
        assert len(source.last_fetch.truncated_scopes) == 1
        cut = source.last_fetch.truncated_scopes[0]
        assert "1 of 20" in cut
        assert "all 20" not in cut
        assert "without events" in cut
    def test_losing_a_tenth_of_the_hops_does_not_read_as_a_broken_feed(self) -> None:
        """The finding, stated as the arithmetic it broke.

        Two of twenty hops refused is 90% of the enrichment collected and all of
        the slate.  Under one-scope-per-refusal that was 2 of 8 scopes — 25%,
        which is a WARNING; twelve of a real 126-hop pass was 67%, which is an
        ERROR and a failed run.  The denominator must count what was *asked
        for*, and this loop asks for one thing.

        Filing the partial loss as a refusal then put that one scope in the
        share-lost numerator; it is a truncation, and must not grade
        ``scopes_refused`` at all.
        """
        from src.validation import Severity

        source, _ = self._run(refuse=2)
        assert source.last_fetch.scopes_requested == len(source.page_keys) + 1
        report = _grade_scope_refusals(source)
        assert not [f for f in report.findings if f.code == "scopes_refused"]
        finding = next(f for f in report.findings if f.code == "scopes_truncated")
        assert finding.severity is Severity.WARNING, finding.message

    def test_the_scope_is_named_for_the_page_and_not_for_a_loop_variable(self) -> None:
        """``page_key`` there was the slate loop's variable, left over from the
        iteration that happened to run last."""
        source, _ = self._run(refuse=1)
        assert source.last_fetch.truncated_scopes[0].startswith("soccer:detail:")
        assert source.last_fetch.failed_scopes == []

    def test_a_clean_pass_counts_the_hop_as_produced(self) -> None:
        """Registered whether or not it fails, or the denominator would still
        move with the numerator — just in the other direction."""
        source, seen = self._run()
        assert len(seen) == 20
        assert source.last_fetch.failed_scopes == []
        assert source.last_fetch.truncated_scopes == []
        assert source.last_fetch.scopes_requested == len(source.page_keys) + 1
        assert source.last_fetch.scopes_with_data == len(source.page_keys) + 1


class TestEveryPagingAdapterKeepsWhatItAlreadyPaidFor:
    """``_fetch_markets`` and ``_fetch_orders`` gathered into local lists and the
    caller only extended after *both* succeeded, so an order-book batch answering
    403 discarded every successful capture for that sport — 12 of 24 responses
    reached the parser and the raw store.  Five sibling adapters were changed to
    append into the caller's list for exactly this reason, each carrying the note
    "4 of 6 requests thrown away on a run that reported OK"; SX Bet's metadata
    half and Pinnacle's matchups/markets pair were the two that still did it.
    And because the refusal is swallowed rather than raised, the collector's
    refusal-persisting path never ran either, so the sport left nothing on disk
    to diagnose the 403 from.

    Asserted by running each adapter against a transport that refuses one
    request in the middle of one scope, and looking at what came back.  The
    three tests these replace were greps: one required the literal
    ``raws.extend(pages)`` — which is the *losing* shape, so the grep locked in
    the defect it was written to prevent — one read a substring out of the
    callee while the caller was what was wrong, and the third listed five
    adapters by hand and omitted Pinnacle, which had the same defect and kept it
    for four more rounds.  The list is now taken from the registry, so an
    adapter cannot be left off it.
    """

    #: How each adapter is made to fail one request part-way through one scope,
    #: which scope survives it, and what must still be on disk afterwards.
    #:
    #: Two scopes each, deliberately: an adapter whose *only* scope fails raises
    #: out of ``require_something`` before ``fetch_raw`` can return anything, so
    #: a single-scope case cannot see what was kept.
    @staticmethod
    def _wounded(module: str):
        import httpx

        from src.sources._common import HostPacer

        future = int((datetime.now(UTC) + timedelta(days=2)).timestamp())

        def wire(source):
            from src.sources._common import RetryPolicy

            source._http.host_interval = 0.0
            source._http._pacer = HostPacer(0.0)
            # One attempt, no backoff.  What a refusal is retried for is
            # asserted in ``test_source_client`` against the policy itself;
            # here the refusal is a premise, and sleeping through three
            # exponential backoffs per adapter to reach it is dead time.
            source._http.retry = RetryPolicy(attempts=1)
            return source

        if module == "polymarket":
            from src.sources.polymarket import PolymarketAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                tag = request.url.params.get("tag_slug", "")
                offset = int(request.url.params.get("offset", "0"))
                if tag == "mlb" and offset:
                    return httpx.Response(500, json={"error": "boom"})
                return httpx.Response(200, json=[
                    {"id": f"{tag}{index}", "slug": f"{tag}-{index}", "title": "x",
                     "startTime": "2026-08-01T18:00:00Z", "markets": []}
                    for index in range(2)
                ])

            source = PolymarketAdapter(
                ["MLB", "NFL"], page_size=2,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return wire(source), "events:mlb:01"

        if module == "smarkets":
            from src.sources.smarkets import SmarketsAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                url = str(request.url)
                if not request.url.path.rstrip("/").endswith("events"):
                    return httpx.Response(200, json={"markets": []})
                if "basketball_match" in url and "pagination_last_id" in url:
                    return httpx.Response(500, json={"error": "boom"})
                kind = "basketball_match" if "basketball_match" in url else "football_match"
                return httpx.Response(200, json={
                    "events": [
                        {"id": str(1000 + index), "name": f"A{index} vs B{index}",
                         "state": "upcoming", "bettable": True,
                         "start_datetime": "2026-08-01T18:00:00Z",
                         "full_slug": f"/sport/x/y/2026/08/01/18-00/a{index}"}
                        for index in range(2)
                    ],
                    "pagination": {
                        "next_page": f"?state=upcoming&type={kind}&pagination_last_id=9"
                    },
                })

            source = SmarketsAdapter(
                ["NBA", "EPL"], page_size=2,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return wire(source), "events:basketball"

        if module == "sxbet":
            from src.sources.sxbet import SxBetAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/orders"):
                    return httpx.Response(200, json={"data": []})
                sport_id = int(request.url.params.get("sportIds", "0"))
                if sport_id == 3 and request.url.params.get("paginationKey"):
                    return httpx.Response(500, json={"error": "boom"})
                return httpx.Response(200, json={"data": {
                    "markets": [{
                        "marketHash": f"0x{sport_id}",
                        "sportId": sport_id, "type": 226,
                        "leagueLabel": "MLB" if sport_id == 3 else "NBA",
                        "status": "ACTIVE", "gameTime": future,
                    }],
                    "nextKey": "more",
                }})

            source = SxBetAdapter(
                ["MLB", "NBA"],
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return wire(source), "markets:baseball:01"

        if module == "kalshi":
            from src.sources.kalshi import KalshiAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                ticker = request.url.params.get("series_ticker", "")
                if ticker == "KXMLBGAME" and request.url.params.get("cursor"):
                    return httpx.Response(500, json={"error": "boom"})
                return httpx.Response(200, json={
                    "markets": [{"ticker": f"{ticker}-{index}"} for index in range(2)],
                    "cursor": "more",
                })

            source = KalshiAdapter(
                ["MLB"], page_size=2,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return wire(source), "markets:KXMLBGAME:01"

        if module == "matchbook":
            from src.sources.matchbook import MatchbookAdapter

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/navigation"):
                    return httpx.Response(200, json=[{"id": 1, "name": "x"}])
                sport_id = int(request.url.params.get("sport-ids", "0"))
                if sport_id == 3 and int(request.url.params.get("offset", "0")):
                    return httpx.Response(500, json={"error": "boom"})
                return httpx.Response(200, json={
                    "events": [{"id": index, "name": f"A{index} vs B{index}"}
                               for index in range(2)],
                    "total": 10_000,
                })

            source = MatchbookAdapter(
                ["MLB", "NBA"], per_page=2,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            return wire(source), "events:baseball:01"

        from src.sources.pinnacle import PinnacleAdapter

        refused: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("markets/straight"):
                # The first league's markets call, and every retry of it — a
                # transport that recovers on retry never reaches the failure
                # path at all.  The second league is left whole so the source
                # survives to return its pages.
                if not refused:
                    refused.append(path)
                if path == refused[0]:
                    return httpx.Response(500, json={"error": "boom"})
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[
                {"id": 100 + index, "type": "matchup", "parentId": None,
                 "participants": []}
                for index in range(2)
            ])

        source = PinnacleAdapter(
            ["MLB", "NBA"],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            request_pause=0.0,
        )
        return wire(source), None

    @pytest.mark.parametrize(
        "module",
        ["polymarket", "smarkets", "sxbet", "kalshi", "matchbook", "pinnacle"],
    )
    def test_a_scope_that_fails_part_way_keeps_the_responses_before_it(
        self, module
    ) -> None:
        source, kept = self._wounded(module)
        try:
            raws = source.fetch_raw()
        finally:
            source.close()

        endpoints = [raw.endpoint for raw in raws]
        # The refusal is still reported — keeping the pages must not have
        # turned a broken scope into a healthy one.
        assert source.last_fetch.failed_scopes, endpoints
        if kept is None:
            # Pinnacle names its scopes by league, and the response kept is the
            # matchups half of the pair whose markets half was refused.
            assert any(e.startswith("matchups:") and "MLB" in e for e in endpoints), endpoints
        else:
            assert kept in endpoints, endpoints

    def test_pinnacle_still_knows_a_sport_produced_nothing(self) -> None:
        """The guard that "did any league work?" is asked by.

        It used to be asked as ``if not raws``, which was the same question only
        while a failed league left nothing behind.  Now that a league refused on
        its markets call keeps its matchups page, ``raws`` is non-empty for a
        sport where *every single league* was refused — so the question has to be
        asked of what was actually collected, or a wholly refused sport reports
        as one that returned data.
        """
        import httpx

        from src.sources._common import HostPacer, RetryPolicy, ScopeTally
        from src.sources.guards import SourceError
        from src.sources.pinnacle import PinnacleAdapter
        from src.schema import Sport

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("markets/straight"):
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=[
                {"id": 100, "type": "matchup", "parentId": None, "participants": []}
            ])

        source = PinnacleAdapter(
            ["MLB"], client=httpx.Client(transport=httpx.MockTransport(handler)),
            request_pause=0.0,
        )
        source._http._pacer = HostPacer(0.0)
        source._http.retry = RetryPolicy(attempts=1)
        tally = ScopeTally("pinnacle")
        kept: list = []
        try:
            with pytest.raises(SourceError):
                source._fetch_routed_leagues(Sport.BASEBALL, tally=tally, into=kept)
        finally:
            source.close()
        # It raised *and* it kept the page it had already been given.
        assert [raw.endpoint for raw in kept] == ["matchups:league:MLB:246"]
        assert tally.scopes_with_data == 0

    def test_one_scope_refused_twice_is_still_one_scope(self) -> None:
        """"was refused 2 of the 1 scope(s) it asked for".

        ``failed_scopes`` is a list of *messages* and ``requested`` de-duplicates
        names, so a scope that raises twice put the numerator above the
        denominator — the exact failure ``ScopeTally``'s docstring says the
        design eliminates, reached by a different route.  SX Bet is the live
        case: its metadata half and its order-book half are one scope and two
        requests, so a cap on the first and a 403 on the second both name
        ``baseball``.

        Asserted on the count that grades, not on the messages, which are still
        both kept — an operator wants to read both.
        """
        import httpx

        from src.sources._common import HostPacer, RetryPolicy
        from src.sources.guards import SourceError
        from src.sources.sxbet import SxBetAdapter

        future = int((datetime.now(UTC) + timedelta(days=2)).timestamp())

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/orders"):
                return httpx.Response(403, json={"error": "blocked"})
            return httpx.Response(200, json={"data": {
                "markets": [{
                    "marketHash": "0x1", "sportId": 3, "type": 226,
                    "leagueLabel": "MLB", "status": "ACTIVE", "gameTime": future,
                }],
                "nextKey": "more",
            }})

        source = SxBetAdapter(
            ["MLB"], client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        source._http.host_interval = 0.0
        source._http._pacer = HostPacer(0.0)
        source._http.retry = RetryPolicy(attempts=1)
        try:
            with pytest.raises(SourceError):
                source.fetch_raw()
        finally:
            source.close()

        tally = source.last_fetch
        assert tally.scopes_requested == 1
        assert tally.scopes_failed == 1, tally.failed_scopes
        assert len(tally.truncated_scopes) == 1
        # Both messages survive; only the arithmetic was wrong.
        assert len(tally.failed_scopes) == 1

    def test_the_numerator_never_exceeds_the_denominator(self) -> None:
        """Stated where it is graded, so no adapter can reintroduce it.

        ``asked`` is now the larger of the two, so even a tally that somehow
        reports more losses than requests cannot print a share above 1.0 or a
        sentence that counts past its own denominator.
        """
        from src.sources._common import ScopeTally
        from src.sources.guards import SourceError

        tally = ScopeTally("sxbet")
        tally.requested("baseball")
        tally.failed("baseball", SourceError("first"))
        tally.failed("baseball", SourceError("second"))
        assert tally.scopes_failed == 1 and tally.scopes_requested == 1

        source = type("S", (), {"source_key": "sxbet", "last_fetch": tally})()
        report = _grade_scope_refusals(source)
        message = next(f for f in report.findings if f.code == "scopes_refused").message
        assert "refused 1 of the 1 scope(s)" in message, message

    COVERED = frozenset(
        {"polymarket", "smarkets", "sxbet", "kalshi", "matchbook", "pinnacle"}
    )

    def test_no_multi_request_adapter_is_left_off_that_list(self) -> None:
        """The parametrize list above is hand-written, and a hand-written list is
        exactly what let Pinnacle sit outside this guarantee for four rounds
        while three tests asserted that "all six" adapters had it and named five.

        So the list is checked against the source tree in both directions: an
        adapter that grows a scope spanning more than one request grows an
        ``into=`` helper to hold the responses, and the day it does, it has to
        be exercised here too.
        """
        # Resolved against this file, not the working directory — the same
        # correction this round made in ``test_report.py``, and it would have
        # been a fresh instance of it.
        sources = pathlib.Path(__file__).resolve().parent.parent / "src" / "sources"
        found = {
            path.stem
            for path in sources.glob("*.py")
            if "into: list[RawResponse] | None = None" in path.read_text()
        }
        assert found == self.COVERED, sorted(found ^ self.COVERED)


class TestTheWiringIsTestedAndNotOnlyTheMechanism:
    """A deletion sweep — remove a whole mechanism rather than change an
    expression — found three places with the same shape: the check is tested by
    importing and calling it directly, while the one call that makes it *run* is
    either uncovered, covered only by a source-text grep, or bypassed because
    every test passes the argument explicitly.

    Each of these deletions left the entire suite green.
    """

    def test_find_opportunities_measures_the_mirror_gate_by_default(self) -> None:
        """``if one_counterparty is None: one_counterparty =
        counterparty_groups(quotes)`` is deletable: all twelve tests that
        exercise the gate pass ``one_counterparty=`` explicitly, so nothing
        covered the default.  Both production callers pass it too, which makes
        this the defence-in-depth layer — and the suite had already learned this
        exact lesson one level up, for the CLI argument, and stopped there.

        Called with no gate argument at all, on rows that *are* one book.
        """
        rows = []
        for index in range(24):
            for source in ("tenant_a", "tenant_b"):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=2.10,
                    source_market_id=f"m{index}",
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                    away_participant="MLB-PHI", home_participant=f"MLB-MI{index}",
                ))
        # the market the phantom would be built from
        rows += [
            make_quote(source="tenant_a", selection=Selection.HOME,
                       decimal_odds=2.20, source_market_id="z",
                       event_key="MLB-PHI@MLB-ZZZ:2026-07-28",
                       away_participant="MLB-PHI", home_participant="MLB-ZZZ"),
            make_quote(source="tenant_b", selection=Selection.AWAY,
                       decimal_odds=2.20, source_market_id="z",
                       event_key="MLB-PHI@MLB-ZZZ:2026-07-28",
                       away_participant="MLB-PHI", home_participant="MLB-ZZZ"),
        ]
        report = find_opportunities(rows, as_of=None)
        assert report.opportunities == [], [
            o.describe() for o in report.opportunities
        ]

    def test_collect_once_actually_runs_the_distinctness_check(
        self, tmp_path, monkeypatch
    ) -> None:
        """``_check_distinctness(unfiltered_quotes, report)`` was defended only by
        ``assert '_check_distinctness(unfiltered_quotes, report)' in text``.
        Replacing the call with ``if False:`` keeps that substring and the whole
        suite stays green — so a registry that gains a mirror stays green forever,
        which is the regression the function's own docstring exists to prevent
        ("Measuring without acting is the same as not measuring")."""
        import src.collector
        from src.raw_store import RawStore
        from src.sources.base import ParseOutcome, SourceHealth

        rows = []
        for index in range(24):
            for source in ("tenant_a", "tenant_b"):
                rows.append(make_quote(
                    source=source, selection=Selection.HOME, decimal_odds=2.10,
                    source_market_id=f"m{index}",
                    event_key=f"MLB-PHI@MLB-MI{index}:2026-07-28",
                    away_participant="MLB-PHI", home_participant=f"MLB-MI{index}",
                ))

        class _Named:
            def __init__(self, key):
                self.source_key = key
                self.leagues = ("MLB",)

            def close(self):
                pass

        def _collected(source, **kwargs):
            mine = [row for row in rows if row.source == source.source_key]
            return (
                SourceHealth(source_key=source.source_key, ok=True,
                             checked_at=datetime.now(UTC), quote_count=len(mine),
                             event_count=len(mine)),
                ParseOutcome(quotes=mine),
            )

        monkeypatch.setattr(src.collector, "_collect_source", _collected)
        result = src.collector.collect_once(
            [_Named("tenant_a"), _Named("tenant_b")],
            raw_store=RawStore(tmp_path / "raw"), store=None,
        )
        codes = [f.code for f in result.report.findings]
        assert any("one_counterparty" in code for code in codes), codes

    def test_validate_actually_runs_the_observation_window_check(self) -> None:
        """``_check_observation_window(quotes, report)`` is deletable from
        ``validate()`` with the suite green: both of its tests import the private
        function and call it directly, so the public entry point was never
        checked.  What it tells an operator is that the coverage number is
        fiction — N sources on a fixture where no two can be legs of one
        position."""
        from src.validation import validate

        # Grouped by *selection*, so the two sources have to price the same one:
        # these are the two prices that would actually be the legs.
        base = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
        rows = []
        for source, offset in (("pinnacle", timedelta(0)),
                               ("bovada", timedelta(hours=3))):
            for selection, odds in ((Selection.HOME, 2.10), (Selection.AWAY, 2.05)):
                rows.append(make_quote(
                    source=source, selection=selection, decimal_odds=odds,
                    source_market_id="m", observed_at=base + offset,
                ))
        codes = [f.code for f in validate(rows).findings]
        assert "collected_outside_the_comparable_window" in codes, codes

        # ...and two sources read close together are not reported.
        together = []
        for source in ("pinnacle", "bovada"):
            for selection, odds in ((Selection.HOME, 2.10), (Selection.AWAY, 2.05)):
                together.append(make_quote(
                    source=source, selection=selection, decimal_odds=odds,
                    source_market_id="m", observed_at=base,
                ))
        assert "collected_outside_the_comparable_window" not in [
            f.code for f in validate(together).findings
        ]


class TestTheChecksNobodyWasTesting:
    """Three whole branches in ``validate()`` had zero references anywhere in
    ``tests/`` and could each be deleted with the suite green."""

    def _codes(self, rows, **kwargs):
        from src.validation import validate

        return [f.code for f in validate(rows, **kwargs).findings]

    def test_an_extreme_overround_is_reported(self) -> None:
        """A book keeping far more than any real margin is either a mispricing or
        a parser reading the wrong field."""
        rows = [
            make_quote(source="pinnacle", selection=selection, decimal_odds=1.20,
                       source_market_id="m")
            for selection in (Selection.HOME, Selection.AWAY)
        ]
        assert "extreme_overround" in self._codes(rows)

    def test_a_core_market_at_the_wrong_window_is_reported(self) -> None:
        """"The market exists but at a window you cannot compare against" — the
        partial-upstream-rename shape."""
        rows = [
            make_quote(source="pinnacle", selection=selection, decimal_odds=2.00,
                       period=Period.FIRST_5_INNINGS, source_market_id="m")
            for selection in (Selection.HOME, Selection.AWAY)
        ]
        assert "core_market_in_a_different_period" in self._codes(rows)

    def test_a_long_stale_commence_time_is_reported(self) -> None:
        """``commence_time_too_far``, the ``if`` arm above it, has eight test
        references; this ``elif`` had none."""
        rows = [
            make_quote(source="pinnacle", selection=selection, decimal_odds=2.00,
                       source_market_id="m",
                       commence_time=datetime(2026, 6, 20, 18, 0, tzinfo=UTC))
            for selection in (Selection.HOME, Selection.AWAY)
        ]
        assert "commence_time_stale" in self._codes(rows)


class TestOrphanRowsCannotBeWritten:
    """``PRAGMA foreign_keys = ON`` was deletable with the whole suite green.

    SQLite disables foreign keys per connection by default, so without that line
    a finding, health row or quote can be written against a ``run_id`` that does
    not exist — and every read command joins on it.  ``migrate_database`` runs
    ``PRAGMA foreign_key_check`` and refuses on a violation, which only means
    something if violations cannot be written in the first place.
    """

    def test_the_connection_enforces_them(self, tmp_path) -> None:
        from src.store import Store

        with Store(tmp_path / "db.sqlite3") as store:
            assert store.query("PRAGMA foreign_keys")[0][0] == 1

    def test_a_finding_against_a_missing_run_is_refused(self, tmp_path) -> None:
        import sqlite3

        from src.store import Store

        with Store(tmp_path / "db.sqlite3") as store:
            with pytest.raises(sqlite3.IntegrityError):
                store._conn.execute(
                    "INSERT INTO finding (run_id, severity, code, message) "
                    "VALUES (?, 'error', 'x', 'y')", (999_999,)
                )

    def test_a_quote_against_a_missing_run_is_refused(self, tmp_path) -> None:
        import sqlite3

        from src.store import Store

        with Store(tmp_path / "db.sqlite3") as store:
            with pytest.raises(sqlite3.IntegrityError):
                store.save_quotes(999_999, [make_quote()])


class TestReAnalysisAppliesTheStoredRunsJurisdiction:
    """``_cmd_arb`` judged every stored run by Illinois's rules and then texted it.

    ``find_opportunities`` was called without ``view_only_sources``, so it fell
    back to ``registry.VIEW_ONLY_SOURCES`` — frozen at import from
    ``settings.STATE``.  Analysing a stored Pennsylvania run therefore applied
    *Illinois's* view-only set, and the exact-state leg filter that
    ``collect_once`` and ``src.report`` both apply was missing entirely.  The
    result printed, and with alerts on by default it sent.

    ``--run`` defaults to the latest run, which in a batch is the last state
    collected, so the operator neither picked the jurisdiction nor was shown it.
    """

    def _kickoff(self):
        return datetime.now(UTC) + timedelta(hours=6)

    def _stored_pa_run(self, tmp_path, sources):
        from src.store import Store
        from src.validation import ValidationReport

        rows = [
            make_quote(source=source, selection=selection, decimal_odds=odds,
                       source_market_id="m", commence_time=self._kickoff())
            for source, selection, odds in sources
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), jurisdiction="PA")
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
                counterparties={},
            )
        return run

    def _run_arb(self, tmp_path, monkeypatch, capsys, sources):
        import src.collector
        import src.settings

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        self._stored_pa_run(tmp_path, sources)
        assert src.collector.main(["arb", "--no-alert"]) == 0
        return capsys.readouterr().out

    def test_the_runs_jurisdiction_is_stated(self, tmp_path, monkeypatch, capsys):
        """An operator who did not choose the state must at least be told it."""
        out = self._run_arb(tmp_path, monkeypatch, capsys, [
            ("fanduel", Selection.HOME, 2.10),
            ("pinnacle", Selection.AWAY, 2.10),
        ])
        assert "jurisdiction: PA" in out

    def test_a_stored_key_the_registry_forgot_cannot_leg_a_position(
        self, tmp_path, monkeypatch, capsys
    ):
        """Rows outlive registrations, and a ghost key is never a leg.

        ``unibet_au`` left 68 real rows behind when it was deregistered, and
        re-analysing those runs formed it into alert-eligible legs — a
        counterparty with no commission schedule, no settlement rule and no
        bet link.  ``_cmd_arb`` now folds the run's stored keys into
        ``view_only_for_run``, so the ghost's rows are context, not a side
        of a position.
        """
        out = self._run_arb(tmp_path, monkeypatch, capsys, [
            ("unibet_au", Selection.HOME, 2.20),
            ("fanduel", Selection.AWAY, 2.20),
        ])
        assert "unibet_au" not in out.split("jurisdiction:")[-1] or (
            "0 opportunities" in out
        ), out
        assert "0 opportunities" in out, out

    def test_include_started_never_reaches_the_notifier(
        self, tmp_path, monkeypatch, capsys
    ):
        """A historical study must not text, even with alerts left on.

        The command prints "a historical study, not positions anyone can
        take" under ``--include-started`` — and then handed the same
        opportunities to ``notify_opportunities``, whose body says "PLACE
        BOTH NOW — prices move" about games that settled days earlier.  The
        CLI now refuses to call the notifier at all under the flag (and
        ``AlertBook.notify``'s own clock refuses started fixtures for every
        other caller).
        """
        import src.collector
        import src.settings

        calls: list = []
        monkeypatch.setattr(
            src.collector, "notify_opportunities",
            lambda *a, **k: calls.append(a) or [],
        )
        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        from src.store import Store
        from src.validation import ValidationReport

        started = datetime.now(UTC) - timedelta(hours=6)
        rows = [
            make_quote(source=source, selection=selection, decimal_odds=odds,
                       source_market_id="m", commence_time=started)
            for source, selection, odds in (
                ("fanduel", Selection.HOME, 2.20),
                ("pinnacle", Selection.AWAY, 2.20),
            )
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(datetime.now(UTC), jurisdiction="PA")
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
                counterparties={},
            )
        assert src.collector.main(["arb", "--include-started"]) == 0
        out = capsys.readouterr().out
        assert "historical study" in out, out
        assert "alerts suppressed: --include-started" in out, out
        assert calls == [], "the notifier was reached from a historical study"

    def test_an_arb_with_no_pennsylvania_leg_is_reported_flagged(
        self, tmp_path, monkeypatch, capsys
    ):
        """Every leg offshore or global — a "PA" position with no PA book in it.

        Shown, because national and out-of-state odds may be displayed — but
        never unmarked: the position carries per-leg labels and an
        informational line, so nothing here can read as a PA price.
        """
        out = self._run_arb(tmp_path, monkeypatch, capsys, [
            ("pinnacle", Selection.HOME, 2.20),
            ("bovada", Selection.AWAY, 2.20),
        ])
        assert "0 opportunities" not in out, out
        assert "[not reachable from PA]" in out, out
        assert "no leg reachable from PA — informational" in out, out

    def test_the_flagged_positions_are_counted_out_loud(
        self, tmp_path, monkeypatch, capsys
    ):
        """The marking must not rely on per-leg tags alone.

        A reader skimming for a count should learn how many positions are
        wholly foreign without reading every leg — and the count is what keeps
        this surface agreeing with the same run's dashboard.
        """
        out = self._run_arb(tmp_path, monkeypatch, capsys, [
            ("pinnacle", Selection.HOME, 2.20),
            ("bovada", Selection.AWAY, 2.20),
        ])
        assert "1 position(s) have no leg you can reach from PA" in out, out

    def test_nothing_is_flagged_when_every_position_has_a_local_leg(
        self, tmp_path, monkeypatch, capsys
    ):
        """The caveat is guarded on the count, so a clean run does not carry it."""
        out = self._run_arb(tmp_path, monkeypatch, capsys, [
            ("fanduel", Selection.HOME, 2.20),
            ("pinnacle", Selection.AWAY, 2.20),
        ])
        assert "no leg you can reach" not in out, out
        assert "informational" not in out, out

    def test_an_arb_with_a_pennsylvania_leg_still_reports(
        self, tmp_path, monkeypatch, capsys
    ):
        """The marking must not delete or bury anything."""
        out = self._run_arb(tmp_path, monkeypatch, capsys, [
            ("fanduel", Selection.HOME, 2.20),
            ("pinnacle", Selection.AWAY, 2.20),
        ])
        assert "0 opportunities" not in out, out


class TestLineShoppingSaysWhichPricesAreOutOfState:
    """``lines`` printed another state's best price as this state's, unlabelled.

    Run on the committed Pennsylvania run it showed ``home 1.423 onexbet`` and
    ``away 4.840 pinnacle`` with no jurisdiction and no mark — and its own
    docstring calls it "the surface arbitrage is drawn from". That is rule (a)'s
    substitution on an operator-facing surface.

    Marked rather than filtered on purpose: the command exists to tell a genuine
    "no edge today" apart from a market nobody compared, and dropping the
    out-of-state books would hide the comparison. So the out-of-state number stays
    on the page and says what it is, exactly as ``src.coverage`` treats it.
    """

    def _run_lines(
        self, tmp_path, monkeypatch, capsys, sources, *, state="PA", scope="state"
    ):
        import src.collector
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")

        kickoff = datetime.now(UTC) + timedelta(hours=6)
        rows = [
            make_quote(source=source, selection=selection, decimal_odds=odds,
                       source_market_id="m", commence_time=kickoff)
            for source, selection, odds in sources
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(
                datetime.now(UTC), jurisdiction=state, route_scope=scope,
            )
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
                counterparties={},
            )
        assert src.collector.main(["lines", "--run", str(run)]) == 0
        return capsys.readouterr().out

    def test_an_out_of_state_book_is_named_as_such(self, tmp_path, monkeypatch, capsys):
        out = self._run_lines(tmp_path, monkeypatch, capsys, [
            ("pinnacle", Selection.HOME, 2.20),
            ("pinnacle", Selection.AWAY, 2.20),
        ])
        assert "jurisdiction: PA" in out, out
        assert "not reachable from PA" in out, out
        # And the price is still shown — marked, not withheld.
        assert "pinnacle" in out

    def test_a_licensed_book_carries_no_mark(self, tmp_path, monkeypatch, capsys):
        """The other direction, or the mark means nothing."""
        out = self._run_lines(tmp_path, monkeypatch, capsys, [
            ("fanduel", Selection.HOME, 2.20),
            ("fanduel", Selection.AWAY, 2.20),
        ])
        assert "jurisdiction: PA" in out, out
        assert "not reachable" not in out, out

    @pytest.mark.parametrize("ambient", ["IL", "PA"])
    def test_the_line_board_is_the_runs_board_not_the_readers(
        self, tmp_path, monkeypatch, capsys, ambient
    ):
        """Which sources are a counterparty is the *run's* question, not the box's.

        ``best_prices`` defaulted to the module-level ``VIEW_ONLY_SOURCES``, frozen
        at import from ``settings.STATE``. ``hardrock`` is the key that differs
        between the Illinois and Pennsylvania sets — Illinois licenses it, PA does
        not — so a stored PA run read from an IL-configured box printed a Hard Rock
        price as a selection's **best** price on a Pennsylvania board, and the same
        run read from a PA box left it off. ``collector arb`` on that run already
        resolved the set from the run's own jurisdiction, so the two commands
        disagreed about the same stored rows.

        Parametrised over the reader's state because a single value cannot tell "the
        run decides" from "this box happens to agree with the run".

        The ambient set is patched where it is *read* rather than through
        ``settings.STATE``: ``VIEW_ONLY_SOURCES`` is computed once at registry import,
        so setting the environment afterwards changes nothing and a test written that
        way would pass on the unfixed code.
        """
        import src.arb
        from src.sources import registry

        monkeypatch.setattr(
            src.arb, "VIEW_ONLY_SOURCES", registry.view_only_for_state(ambient)
        )
        out = self._run_lines(tmp_path, monkeypatch, capsys, [
            ("hardrock", Selection.HOME, 2.20),
            ("fanduel", Selection.AWAY, 2.20),
        ])
        assert "jurisdiction: PA" in out, out
        assert "fanduel" in out, out
        assert "hardrock" not in out, out

    def test_no_mirror_can_reach_the_surface_the_mark_is_printed_on(self):
        """Why ``_elsewhere`` needs no republisher case, asserted rather than assumed.

        Every republisher is absent from ``takeable_from_state`` — nobody places a bet
        at Action Network — so if one reached the surface it would print "not
        reachable from PA" beside ``an_fanduel``, which in PA is book id 255, *FanDuel
        Pennsylvania*, and which ``src.coverage`` asserts at import time carries PA's
        own licence. It cannot, because ``best_prices`` is given the same set, and
        that is the invariant holding the mark honest.
        """
        from src.sources import registry

        for state in ("PA", "IL", ""):
            view_only = registry.view_only_for_run(state)
            assert registry.REPUBLISHED_SOURCE_KEYS <= view_only, state


class TestTheCoverageRuleIsActuallyWiredIntoARun:
    """``src.coverage`` was unit-tested and unreachable-by-test from the collector.

    Replacing the whole ``check_book_coverage(...)`` call in ``collect_once`` with
    ``pass`` left the pipeline, validation, integration and guard suites green: 190
    passed. Nothing proved rule (c) ran on a real pass, nor that it was measured on
    the *unfiltered* rows, nor that ``configured`` named the built sources. All
    three are separate claims and each gets its own assertion here.
    """

    def _run(self, tmp_path, sources, *, sports=None, state="PA"):
        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.store import Store
        from tests.test_pipeline import FakeSource

        kickoff = datetime.now(UTC) + timedelta(hours=6)
        built = []
        for key, (league, sport) in sources.items():
            quotes = [
                make_quote(
                    source=key, sport=sport, league=league,
                    event_key=f"{league}-A@{league}-H:2026-08-07",
                    home_participant=f"{league}-H", away_participant=f"{league}-A",
                    selection=selection, decimal_odds=odds,
                    source_market_id="m", commence_time=kickoff,
                )
                for selection, odds in (
                    (Selection.HOME, 1.95), (Selection.AWAY, 2.05),
                )
            ]
            built.append(FakeSource(key, quotes, leagues=(league,)))
        with Store(tmp_path / "db.sqlite3") as store:
            return collect_once(
                built,
                raw_store=RawStore(tmp_path / "raw"),
                store=store,
                sports=sports,
                jurisdiction=state,
                route_scope="state",
                state_source_keys=tuple(sources),
                alert=False,
            )

    def _codes(self, result):
        return [f.code for f in result.report.findings]

    def test_a_state_run_reports_the_required_book_rule(self, tmp_path):
        """The wiring itself: a PA run must judge PA's required books."""
        from src.schema import Sport

        result = self._run(tmp_path, {"fanduel": ("MLB", Sport.BASEBALL)})
        codes = self._codes(result)
        assert any(
            code.startswith("required_book_") or code.startswith("book_coverage_")
            for code in codes
        ), codes

    def test_coverage_is_judged_on_the_unfiltered_board(self, tmp_path):
        """Measured on the rows the books returned, not the rows the run kept.

        ``fanduel`` priced only hockey; the run asked for baseball. Judged on the
        filtered rows FanDuel is unobserved and reports at ERROR — faulting a book
        for the operator's scope choice, which is the mistake ``unfiltered_quotes``
        exists to avoid.
        """
        from src.schema import Sport

        result = self._run(
            tmp_path,
            {
                "fanduel": ("NHL", Sport.HOCKEY),
                "pinnacle": ("MLB", Sport.BASEBALL),
            },
            sports=["baseball"],
        )
        faulted = [
            f for f in result.report.findings
            if f.code.startswith("required_book_") and "FanDuel" in f.message
        ]
        assert not faulted, [f.message for f in faulted]

    def test_books_whose_feeds_were_not_requested_are_not_faulted(self, tmp_path):
        """``configured`` must name the built sources, or a narrow run fails wrongly.

        Nothing here asked for bet365 by any route, so it is ``NOT_REQUESTED``
        rather than an error. Passing ``configured=None`` would judge all eleven
        books on a run that collected one.
        """
        from src.schema import Sport

        result = self._run(tmp_path, {"fanduel": ("MLB", Sport.BASEBALL)})
        assert not [
            f for f in result.report.findings if "bet365" in f.message
        ], [f.message for f in result.report.findings]


class TestTheLiveRunFlagsTheSamePositionsReAnalysisDoes:
    """A state run that built no state book skipped the exact-state leg rule.

    The rule was gated on ``state_source_keys`` — *what was built* — and
    ``collect_batch_once`` then learned to tolerate building nothing, because
    ``--source pinnacle`` legitimately names no retail book.  The two changes
    composed into a hole: the one run with no Pennsylvania book in it was the run
    that reported a "PA" arbitrage with no Pennsylvania leg, and ``alert``
    defaults to ``True``, so it sent.

    Re-analysing that same stored run through ``arb`` *did* apply the rule, so
    the live path and the historical path disagreed about one run — with the
    live one permissive.  Both now ask :func:`src.coverage.locality_marking`,
    which keeps the position and flags it rather than withholding it.
    """

    def _run(self, tmp_path, sources, *, alert=False):
        from src.collector import collect_once
        from src.raw_store import RawStore
        from src.store import Store
        from tests.test_pipeline import FakeSource

        kickoff = datetime.now(UTC) + timedelta(hours=6)
        built = []
        for key, rows in sources.items():
            quotes = [
                make_quote(source=key, selection=selection, decimal_odds=odds,
                           source_market_id="m", commence_time=kickoff)
                for selection, odds in rows
            ]
            built.append(FakeSource(key, quotes, leagues=("MLB",)))
        with Store(tmp_path / "db.sqlite3") as store:
            return collect_once(
                built,
                raw_store=RawStore(tmp_path / "raw"),
                store=store,
                jurisdiction="PA",
                route_scope="state",
                # The reproducing condition: a PA run in which no exact-state
                # first-party source was built at all.
                state_source_keys=(),
                alert=alert,
            )

    def test_an_offshore_only_position_is_reported_flagged_on_a_pa_run(self, tmp_path):
        result = self._run(tmp_path, {
            "pinnacle": [(Selection.HOME, 2.20)],
            "bovada": [(Selection.AWAY, 2.20)],
        })
        assert len(result.arb.opportunities) == 1
        flagged = [
            f for f in result.report.findings
            if f.code == "non_local_positions_flagged"
        ]
        assert flagged, [f.code for f in result.report.findings]
        assert "1 position(s)" in flagged[0].message
        assert "reachable from PA" in flagged[0].message

    def test_a_position_with_a_pa_leg_is_not_flagged_on_the_same_run(self, tmp_path):
        """Licence, not inventory: ``fanduel`` holds one whether or not it built."""
        result = self._run(tmp_path, {
            "fanduel": [(Selection.HOME, 2.20)],
            "pinnacle": [(Selection.AWAY, 2.20)],
        })
        assert len(result.arb.opportunities) == 1
        assert not [
            f for f in result.report.findings
            if f.code == "non_local_positions_flagged"
        ]

    def _armed_alerts(self, monkeypatch):
        import src.alerts as alerts_mod
        import src.settings as settings_mod

        sent: list[str] = []
        monkeypatch.setattr(settings_mod, "ALERT_TRANSPORT", "twilio")
        monkeypatch.setattr(settings_mod, "TWILIO_ACCOUNT_SID", "ACxxxx")
        monkeypatch.setattr(settings_mod, "TWILIO_AUTH_TOKEN", "token")
        monkeypatch.setattr(settings_mod, "TWILIO_FROM_NUMBER", "+15551234567")
        monkeypatch.setattr(settings_mod, "ALERT_TO", "+15550000000")
        monkeypatch.setattr(
            alerts_mod,
            "DEFAULT_BOOK",
            alerts_mod.AlertBook(send=lambda body: sent.append(body) or "SMid"),
        )
        return sent

    def test_the_text_for_a_flagged_position_carries_the_disclaimer(
        self, tmp_path, monkeypatch
    ):
        """Showing and disclaiming travel together, all the way to the SMS.

        The policy that lets a wholly non-local position out of the collector is
        the labelling; a text that reaches a phone without the disclaimer is the
        old defect with extra steps.
        """
        sent = self._armed_alerts(monkeypatch)
        result = self._run(tmp_path, {
            "pinnacle": [(Selection.HOME, 2.20)],
            "bovada": [(Selection.AWAY, 2.20)],
        }, alert=True)
        assert len(result.arb.opportunities) == 1
        assert len(sent) == 1, "the flagged position no longer texts at all"
        assert "NO LEG reachable from PA — informational, not PA prices" in sent[0]
        assert "[not reachable from PA]" in sent[0]

    def test_the_live_summary_prints_the_same_labels(self, tmp_path, capsys):
        """`collect` itself is a rendering surface, and it was the unlabelled one.

        The old filter reassigned the report's list before ``RunResult`` was
        built, so ``print_summary`` never saw a wholly-foreign position; keeping
        the position without threading the marking through would print it here
        bare — the one surface the finding's own "shown and labelled" text
        cannot excuse.
        """
        result = self._run(tmp_path, {
            "pinnacle": [(Selection.HOME, 2.20)],
            "bovada": [(Selection.AWAY, 2.20)],
        })
        capsys.readouterr()
        result.print_summary()
        out = capsys.readouterr().out
        assert "[not reachable from PA]" in out, out
        assert "no leg reachable from PA — informational, not PA prices" in out, out

    def test_the_live_summary_accounts_for_positions_past_its_slice(self, capsys):
        """`print_summary` shows ten positions; the eleventh must not vanish.

        The flagged finding says every position is shown and labelled, and a
        silent ``[:10]`` slice made that false for whatever ranked eleventh.
        """
        from src.arb import ArbReport
        from src.collector import RunResult
        from src.validation import ValidationReport
        from tests.test_alerts import _opportunity

        result = RunResult(
            None, [], [], ValidationReport(quote_count=0, event_count=0),
            ArbReport(opportunities=[_opportunity() for _ in range(12)],
                      diagnostics=[]),
        )
        result.print_summary()
        out = capsys.readouterr().out
        assert "... 2 more position(s)" in out, out
        assert "prints all of a stored run" in out, out

    def test_the_re_analysis_text_carries_the_disclaimer_too(
        self, tmp_path, monkeypatch, capsys
    ):
        """`arb --run` is the other path that texts, and it must not diverge.

        Its twin defect is the round's origin story: the stored-run path once
        printed and texted a "PA" position no Pennsylvanian could take.  The
        collect-path test above cannot cover it — the two call sites build their
        markings independently, so dropping ``marking=`` from this one leaves
        every other test green.
        """
        import src.collector

        sent = self._armed_alerts(monkeypatch)
        import src.settings

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")
        from src.store import Store
        from src.validation import ValidationReport

        kickoff = datetime.now(UTC) + timedelta(hours=6)
        rows = [
            make_quote(source=source, selection=selection, decimal_odds=2.20,
                       source_market_id="m", commence_time=kickoff)
            for source, selection in (
                ("pinnacle", Selection.HOME), ("bovada", Selection.AWAY),
            )
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(
                datetime.now(UTC), jurisdiction="PA", route_scope="state",
            )
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
                counterparties={},
            )
        assert src.collector.main(["arb", "--run", str(run)]) == 0
        capsys.readouterr()
        assert len(sent) == 1, "the arb command no longer texts the position"
        assert "NO LEG reachable from PA — informational, not PA prices" in sent[0]
        assert "[not reachable from PA]" in sent[0]


class TestEveryReadCommandNamesTheJurisdiction:
    """One state's prices were shown with nothing on the page naming the state.

    ``--run`` defaults to the latest run, which in a batch is the last state
    collected — so the operator neither chose the jurisdiction nor was told it.
    ``arb`` and ``lines`` state it; ``show``, ``replay``, ``mirrors`` and ``runs``
    did not, and ``runs`` printed a GLOBAL row beside an IL row and a PA row as
    though they were three passes over the same slate.
    """

    def _two_state_db(self, tmp_path, monkeypatch):
        import src.collector
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")

        kickoff = datetime.now(UTC) + timedelta(hours=6)
        runs = {}
        with Store(tmp_path / "db.sqlite3") as store:
            for state in ("IL", "PA"):
                rows = [
                    make_quote(source=source, selection=selection, decimal_odds=2.05,
                               source_market_id="m", commence_time=kickoff)
                    for source, selection in (
                        ("fanduel", Selection.HOME), ("draftkings", Selection.AWAY),
                    )
                ]
                run = store.start_run(
                    datetime.now(UTC), jurisdiction=state, route_scope="state",
                )
                store.save_quotes_by_source(run, rows)
                store.finish_run(
                    run, finished_at=datetime.now(UTC),
                    report=ValidationReport(quote_count=len(rows), event_count=1),
                    counterparties={},
                )
                runs[state] = run
        return runs

    def test_runs_prints_the_state_of_each_row(self, tmp_path, monkeypatch, capsys):
        import src.collector

        runs = self._two_state_db(tmp_path, monkeypatch)
        assert src.collector.main(["runs"]) == 0
        out = capsys.readouterr().out
        assert "state" in out.splitlines()[0], out.splitlines()[0]
        assert "IL" in out and "PA" in out, out
        del runs

    def test_show_names_the_state_of_the_rows_it_prints(
        self, tmp_path, monkeypatch, capsys
    ):
        import src.collector

        runs = self._two_state_db(tmp_path, monkeypatch)
        assert src.collector.main(["show", "--run", str(runs["PA"])]) == 0
        assert "[PA]" in capsys.readouterr().out

    def test_mirrors_scopes_its_verdict_to_the_state_it_measured(
        self, tmp_path, monkeypatch, capsys
    ):
        """The verdict is "remove this from the registry" — an all-states decision."""
        import src.collector

        runs = self._two_state_db(tmp_path, monkeypatch)
        assert src.collector.main(["mirrors", "--run", str(runs["IL"])]) == 0
        assert "[IL]" in capsys.readouterr().out


class TestArbAndLinesAgreeAboutTheSameStoredRun:
    """Two commands, one run, one answer about what is reachable from the state.

    The shared locality rule was built to end this: three surfaces had spelled the
    condition three ways. Then ``lines`` grew a *fourth* spelling — the jurisdiction
    alone, with no scope test — and the pair contradicted each other again on a run
    whose scope was not ``"state"``:

        arb   →  1 opportunity … pinnacle away … onexbet home …
        lines →  away 2.200 pinnacle  [not reachable from PA]
                 home 2.200 onexbet   [not reachable from PA]

    ``arb`` offered, unmarked, a position that ``lines``, one command later, said
    had no leg the operator could reach. Both now mark identically: the same
    ``[not reachable from PA]`` tag on the same legs, from the same
    :func:`src.coverage.locality_marking`. Both directions are pinned, because
    marking everything and marking nothing are both agreement, and only one of
    them is right per scope.
    """

    def _run(self, tmp_path, monkeypatch, capsys, *, scope):
        import src.collector
        import src.settings
        from src.store import Store
        from src.validation import ValidationReport

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")

        kickoff = datetime.now(UTC) + timedelta(hours=6)
        rows = [
            make_quote(source=source, selection=selection, decimal_odds=2.20,
                       source_market_id="m", commence_time=kickoff)
            for source, selection in (
                ("pinnacle", Selection.HOME), ("onexbet", Selection.AWAY),
            )
        ]
        with Store(tmp_path / "db.sqlite3") as store:
            run = store.start_run(
                datetime.now(UTC), jurisdiction="PA", route_scope=scope,
            )
            store.save_quotes_by_source(run, rows)
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=len(rows), event_count=1),
                counterparties={},
            )
        # ``--no-alert`` because ``arb`` texts by default, which is the whole reason
        # the disagreement mattered rather than being a cosmetic one.
        assert src.collector.main(
            ["arb", "--run", str(run), "--no-alert"]
        ) == 0
        arb_out = capsys.readouterr().out
        assert src.collector.main(["lines", "--run", str(run)]) == 0
        return arb_out, capsys.readouterr().out

    def test_a_legacy_run_is_marked_the_same_way_by_both(
        self, tmp_path, monkeypatch, capsys
    ):
        arb_out, lines_out = self._run(
            tmp_path, monkeypatch, capsys, scope="legacy"
        )
        assert "withheld" not in arb_out, arb_out
        assert "1 position(s) have no leg you can reach from PA" in arb_out, arb_out
        # The exact framing ``lines`` uses — two spaces, brackets — so the two
        # commands' markers cannot drift apart one character at a time.
        assert "  [not reachable from PA]" in arb_out, arb_out
        assert "no leg reachable from PA — informational" in arb_out, arb_out
        assert "  [not reachable from PA]" in lines_out, lines_out

    def test_a_widened_scope_run_is_unmarked_by_both(
        self, tmp_path, monkeypatch, capsys
    ):
        """``--scope all`` from PA is a request to see the wider board.

        Fails on the pre-fix ``lines``, which marked both legs of a position ``arb``
        was offering in the same breath.
        """
        arb_out, lines_out = self._run(tmp_path, monkeypatch, capsys, scope="all")
        assert "not reachable" not in arb_out, arb_out
        assert "no leg you can reach" not in arb_out, arb_out
        assert "not reachable" not in lines_out, lines_out
        assert "pinnacle" in lines_out, lines_out


class TestCaesarsLeagueFallbackMatchesWholeWords:
    """"nba" is a substring of "wnba", and the competition table iterates
    NBA-first — so the name fallback filed every WNBA competition under NBA,
    whose team names then fail participant resolution as loud rejections.
    The fallback exists precisely because seasonal ids rotate, so it has to
    be right the day a rotated WNBA id arrives under an unknown uuid.
    """

    def test_wnba_is_not_swallowed_by_nba(self) -> None:
        from src.sources.caesars import _league_from_event

        assert _league_from_event(
            {"competition": {"id": "rotated-away", "name": "WNBA"}}
        ) == "WNBA"

    def test_the_words_still_match_inside_longer_names(self) -> None:
        from src.sources.caesars import _league_from_event

        assert _league_from_event(
            {"competition": {"id": "rotated-away", "name": "NBA Summer League"}}
        ) == "NBA"
        assert _league_from_event(
            {"competition": {"id": "rotated-away", "name": "WNBA Commissioner's Cup"}}
        ) == "WNBA"
        assert _league_from_event(
            {"competition": {"id": "rotated-away", "name": "nothing known"}}
        ) is None
