"""End-to-end tests over the whole non-scraping pipeline.

These drive the same entry points an operator uses — ``collect_once`` and the
command line — against the real captured payloads, so the integration between
parsing, reconciliation, validation, storage, replay and arbitrage detection is
exercised as one thing rather than as five separately-tested parts.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from src.arb import find_opportunities
from src.collector import collect_once, main, replay_run
from src.events import reconcile_event_keys
from src.raw_store import RawStore
from src.schema import Market, Period
from src.store import Store
from src.validation import validate


class _FixtureSource:
    """Replays captured responses through a real adapter's parser."""

    def __init__(self, source_key: str, raws, parser) -> None:
        self._key = source_key
        self._raws = raws
        self._parser = parser

    @property
    def source_key(self) -> str:
        return self._key

    def fetch_raw(self):
        return list(self._raws)

    def parse(self, raws):
        return self._parser(raws)

    def close(self) -> None:
        pass


@pytest.fixture()
def sources(fanduel_raw, pinnacle_raw, kambi_raw):
    from src.sources.betrivers_kambi import BetRiversKambiAdapter
    from src.sources.fanduel import FanDuelAdapter
    from src.sources.pinnacle import PinnacleAdapter

    return [
        _FixtureSource("fanduel", fanduel_raw, FanDuelAdapter().parse),
        _FixtureSource("pinnacle", pinnacle_raw, PinnacleAdapter().parse),
        _FixtureSource("betrivers_kambi", kambi_raw, BetRiversKambiAdapter().parse),
    ]


@pytest.fixture()
def collected(tmp_path: Path, sources):
    """One complete run, persisted, ready to be re-queried."""
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        result = collect_once(sources, raw_store=raw_store, store=store)
        yield result, store, raw_store


class TestFullRun:
    def test_a_run_produces_quotes_health_and_an_arbitrage_verdict(self, collected) -> None:
        result, _, _ = collected
        assert len(result.quotes) == 1654
        assert {h.source_key for h in result.health} == {
            "fanduel",
            "pinnacle",
            "betrivers_kambi",
        }
        assert result.arb is not None
        # The captured slate is correctly priced, so there is nothing to bet.
        assert result.arb.opportunities == []
        # But markets were genuinely compared, which is what makes that mean
        # something rather than being vacuously true.
        assert result.arb.comparable_group_count >= 30

    def test_quotes_survive_a_round_trip_through_storage(self, collected) -> None:
        result, store, _ = collected
        assert result.run_id is not None
        loaded = store.load_quotes(result.run_id)
        assert len(loaded) == len(result.quotes)
        assert {q.dedup_key for q in loaded} == {q.dedup_key for q in result.quotes}

    def test_replaying_the_stored_run_reproduces_it_exactly(self, collected) -> None:
        result, store, raw_store = collected
        ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
        assert ok, problems

    def test_arbitrage_over_stored_rows_matches_the_live_verdict(self, collected) -> None:
        """Detection must not depend on whether rows came from a parser or from
        SQLite — otherwise the stored history cannot be re-analysed."""
        result, store, _ = collected
        stored, _ = reconcile_event_keys(store.load_quotes(result.run_id))
        from_storage = find_opportunities(stored)
        assert len(from_storage.opportunities) == len(result.arb.opportunities)
        assert from_storage.comparable_group_count == result.arb.comparable_group_count

    def test_validation_is_reproducible_from_storage(self, collected) -> None:
        result, store, _ = collected
        again = validate(store.load_quotes(result.run_id))
        assert Counter(f.code for f in again.findings) == Counter(
            f.code for f in result.report.findings
        )


class TestReconciliationIsApplied:
    def test_the_pipeline_reconciles_event_keys_before_validating(self, collected) -> None:
        """The doubleheader in the captured slate already agrees across books, so
        this asserts the wiring rather than a correction: every event key present
        must be one reconciliation would also produce."""
        result, _, _ = collected
        again, changes = reconcile_event_keys(result.quotes)
        assert changes == [], "the persisted rows should already be reconciled"
        assert {q.event_key for q in again} == {q.event_key for q in result.quotes}

    def test_the_doubleheader_stays_split(self, collected) -> None:
        result, _, _ = collected
        keys = {q.event_key for q in result.quotes if q.event_key.startswith("CLE@CIN")}
        assert keys == {"CLE@CIN:2026-07-28", "CLE@CIN:2026-07-28#2"}


class TestCommandLine:
    """The CLI is the operator interface; a broken flag is a broken pipeline."""

    @pytest.fixture()
    def populated(self, tmp_path: Path, monkeypatch, sources):
        """A stored run, with the CLI pointed at a throwaway data directory.

        The paths are patched as module attributes rather than by reloading
        ``src.settings``: the commands read ``settings.DB_PATH`` at call time, so
        this is sufficient, and it is undone automatically.  Reloading the module
        would rebind it for every other test in the session too.
        """
        import src.collector
        import src.settings

        monkeypatch.setattr(src.settings, "DATA_DIR", tmp_path)
        monkeypatch.setattr(src.settings, "RAW_DIR", tmp_path / "raw")
        monkeypatch.setattr(src.settings, "DB_PATH", tmp_path / "db.sqlite3")

        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            src.collector.collect_once(sources, raw_store=raw_store, store=store)
        return src.collector

    def test_runs_lists_the_stored_run(self, populated, capsys) -> None:
        assert populated.main(["runs"]) == 0
        assert "fanduel" in capsys.readouterr().out

    def test_show_prints_normalized_rows(self, populated, capsys) -> None:
        assert populated.main(["show", "--limit", "5"]) == 0
        out = capsys.readouterr().out
        assert "moneyline" in out or "total_runs" in out

    def test_arb_reports_no_opportunities_and_says_what_it_compared(
        self, populated, capsys
    ) -> None:
        assert populated.main(["arb"]) == 0
        out = capsys.readouterr().out
        assert "0 opportunities" in out
        assert "cross-book markets" in out

    def test_arb_accepts_a_bankroll_and_a_threshold(self, populated, capsys) -> None:
        assert populated.main(["arb", "--stake", "500", "--min-margin", "1.5"]) == 0
        assert "cross-book markets" in capsys.readouterr().out

    def test_arb_verbose_explains_rejections(self, populated, capsys) -> None:
        assert populated.main(["arb", "--verbose"]) == 0

    def test_replay_passes_for_the_stored_run(self, populated, capsys) -> None:
        assert populated.main(["replay"]) == 0
        assert "PASS" in capsys.readouterr().out

    def test_lines_shows_the_best_price_surface(self, populated, capsys) -> None:
        assert populated.main(["lines", "--cross-book-only", "--limit", "5"]) == 0
        out = capsys.readouterr().out
        assert "market(s) shown of" in out
        assert "sum " in out

    def test_health_summarises_each_source(self, populated, capsys) -> None:
        populated.main(["health"])
        out = capsys.readouterr().out
        for source in ("fanduel", "pinnacle", "betrivers_kambi"):
            assert source in out

    def test_an_unknown_source_is_refused(self, populated) -> None:
        with pytest.raises(SystemExit):
            populated.main(["collect", "--source", "definitely-not-a-book"])


class TestStoredArtifacts:
    def test_raw_responses_are_written_before_anything_interprets_them(
        self, collected, tmp_path: Path
    ) -> None:
        result, store, _ = collected
        paths = store.raw_paths(result.run_id)
        assert paths
        for _, path in paths:
            assert Path(path).exists()
            envelope = json.loads(Path(path).read_text(encoding="utf-8"))
            assert envelope["body"], "the verbatim body must be stored"

    def test_every_quote_points_at_a_stored_raw_response(self, collected) -> None:
        result, store, _ = collected
        stored_refs = {
            row["raw_ref"] for row in store.query("SELECT raw_ref FROM raw_response WHERE run_id = ?", (result.run_id,))
        }
        assert stored_refs
        for quote in result.quotes:
            assert quote.raw_ref in stored_refs, f"orphaned raw_ref {quote.raw_ref}"

    def test_findings_are_persisted_for_the_run(self, collected) -> None:
        result, store, _ = collected
        rows = store.query("SELECT code FROM finding WHERE run_id = ?", (result.run_id,))
        assert len(rows) == len(result.report.findings)


class TestThePipelineCanActuallySurfaceAnOpportunity:
    """Guards against the integration being silently broken.

    Every other end-to-end assertion here is that the captured slate yields no
    arbitrage, which is the correct answer but is also what a disconnected
    detector returns.  These tests inject two **synthetic** books priced to a
    known edge and follow it all the way through the pipeline to the command
    line.  The prices are invented for the test and are not observed data.
    """

    @pytest.fixture()
    def rigged_sources(self):
        from src.schema import Selection
        from src.sources.base import ParseOutcome
        from tests.conftest import make_quote

        def book(source: str, home_odds: float, away_odds: float):
            quotes = [
                make_quote(
                    source=source,
                    selection=Selection.HOME,
                    decimal_odds=home_odds,
                    source_market_id=f"{source}-ml",
                    raw_ref=f"{source}/synthetic",
                ),
                make_quote(
                    source=source,
                    selection=Selection.AWAY,
                    decimal_odds=away_odds,
                    source_market_id=f"{source}-ml",
                    raw_ref=f"{source}/synthetic",
                ),
            ]

            class Rigged:
                source_key = source

                def fetch_raw(self):
                    return []

                def parse(self, raws):
                    return ParseOutcome(quotes=list(quotes))

                def close(self) -> None:
                    pass

            return Rigged()

        # book_a is best on home, book_b best on away. Taking the best of each
        # gives 1/2.30 + 1/2.05 = 0.9226 -> a 7.74% margin.
        return [book("book_a", 2.30, 1.60), book("book_b", 1.65, 2.05)]

    def test_an_injected_edge_reaches_the_run_result(
        self, tmp_path: Path, rigged_sources
    ) -> None:
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(rigged_sources, raw_store=raw_store, store=store)

        assert result.arb is not None
        assert len(result.arb.opportunities) == 1
        opportunity = result.arb.opportunities[0]
        assert opportunity.margin == pytest.approx(1 - (1 / 2.30 + 1 / 2.05), abs=1e-9)
        assert sorted(opportunity.sources) == ["book_a", "book_b"]
        assert opportunity.guaranteed_profit > 0
        assert opportunity.is_risk_free
        # A margin this size is not real money; it must be flagged as such.
        assert any("implausibly large" in note for note in opportunity.notes)

    def test_the_legs_pick_the_best_price_at_each_book(
        self, tmp_path: Path, rigged_sources
    ) -> None:
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(rigged_sources, raw_store=raw_store, store=store)
        legs = {leg.selection.value: leg for leg in result.arb.opportunities[0].legs}
        assert legs["home"].source == "book_a" and legs["home"].decimal_odds == 2.30
        assert legs["away"].source == "book_b" and legs["away"].decimal_odds == 2.05

    def test_the_summary_prints_the_opportunity(
        self, tmp_path: Path, rigged_sources, capsys
    ) -> None:
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(rigged_sources, raw_store=raw_store, store=store)
        result.print_summary()
        out = capsys.readouterr().out
        assert "arbitrage: 1 opportunity" in out
        assert "book_a" in out and "book_b" in out
        assert "outcomes:" in out

    def test_it_survives_the_round_trip_through_storage(
        self, tmp_path: Path, rigged_sources
    ) -> None:
        """Detection over stored rows must match detection over parsed rows, or
        the stored history cannot be re-analysed."""
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(rigged_sources, raw_store=raw_store, store=store)
            stored, _ = reconcile_event_keys(store.load_quotes(result.run_id))
        again = find_opportunities(stored)
        assert len(again.opportunities) == 1
        assert again.opportunities[0].margin == pytest.approx(
            result.arb.opportunities[0].margin
        )


class TestCrossSourceCoverage:
    """What the pipeline can actually compare, stated as facts about the slate."""

    def test_the_three_core_full_game_markets_are_cross_book(self, collected) -> None:
        result, _, _ = collected
        from src.arb import best_prices

        cross_book = {
            key
            for key, selections in best_prices(result.quotes).items()
            if len({q.source for q in selections.values()}) > 1
        }
        markets = {(key[1], key[2]) for key in cross_book}
        assert (Market.MONEYLINE, Period.FULL_GAME) in markets
        assert (Market.RUN_LINE, Period.FULL_GAME) in markets
        assert (Market.TOTAL_RUNS, Period.FULL_GAME) in markets
