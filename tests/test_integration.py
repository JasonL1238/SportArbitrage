"""End-to-end tests over the whole non-scraping pipeline.

These drive the same entry points an operator uses — ``collect_once``, the command
line, and the dashboard builder — against the **real captured payloads** in
``tests/fixtures/raw`` (fetched live on 2026-07-28), so the integration between
parsing, reconciliation, validation, storage, replay, arbitrage detection and
reporting is exercised as one thing rather than as six separately-tested parts.

Where a test needs a known arbitrage it says so and builds the prices itself;
those are invented and are never presented as observed data.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from src.arb import best_prices, find_opportunities
from src.collector import collect_once, replay_run
from src.events import reconcile_event_keys
from src.raw_store import RawStore
from src.schema import Market, Period, Sport
from src.store import Store, database_version
from src.validation import validate

FIXTURE_RAW = Path(__file__).parent / "fixtures" / "raw"


class _FixtureSource:
    """Replays captured responses through a real adapter.

    ``fetch_raw`` returns the stored bytes instead of making requests; everything
    after that is the production path, including the adapter's own ``parse`` and
    its declared league list.
    """

    def __init__(self, source_key: str, adapter) -> None:
        self._key = source_key
        self._adapter = adapter
        store = RawStore(FIXTURE_RAW)
        paths = sorted(FIXTURE_RAW.glob(f"{source_key}__*.json"))
        if not paths:
            raise AssertionError(f"no captured responses for {source_key} in {FIXTURE_RAW}")
        self._raws = [store.read(path) for path in paths]

    @property
    def source_key(self) -> str:
        return self._key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._adapter.leagues

    def fetch_raw(self):
        return list(self._raws)

    def parse(self, raws):
        return self._adapter.parse(raws)

    def close(self) -> None:
        self._adapter.close()


@pytest.fixture()
def sources():
    from src.sources.betrivers_kambi import BetRiversKambiAdapter
    from src.sources.fanduel import FanDuelAdapter
    from src.sources.pinnacle import PinnacleAdapter

    built = [
        _FixtureSource("fanduel", FanDuelAdapter()),
        _FixtureSource("pinnacle", PinnacleAdapter()),
        _FixtureSource("betrivers_kambi", BetRiversKambiAdapter()),
    ]
    yield built
    for source in built:
        source.close()


#: The instant the captured slate is judged against.
#:
#: Pinned rather than left as "now", because the started-game gate is a
#: comparison against the wall clock: with the fixtures dated 2026-07-28, a run
#: of this suite in the morning and one in the afternoon see different numbers of
#: fixtures still in the future, and any assertion on a count moves under it.
#: Just before the earliest kickoff on the captured slate.
FIXTURE_AS_OF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@pytest.fixture()
def collected(tmp_path: Path, sources):
    """One complete run, persisted, ready to be re-queried."""
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        result = collect_once(
            sources, raw_store=raw_store, store=store, as_of=FIXTURE_AS_OF
        )
        yield result, store, raw_store


class TestFullRun:
    def test_a_run_produces_quotes_health_and_an_arbitrage_verdict(self, collected) -> None:
        result, _, _ = collected
        assert {h.source_key for h in result.health} == {
            "fanduel", "pinnacle", "betrivers_kambi",
        }
        # Every parsed row reached the run: the pipeline drops nothing between
        # parse and persist except what a filter was asked to exclude.
        assert sum(h.quote_count for h in result.health) == len(result.quotes)
        assert result.excluded_by_filter == 0
        assert result.arb is not None
        # The captured slate is correctly priced, so there is nothing to bet.
        assert result.arb.opportunities == []
        # But markets were genuinely compared, which is what makes that mean
        # something rather than being vacuously true.
        assert result.arb.comparable_group_count >= 100

    def test_the_run_passes_validation(self, collected) -> None:
        result, _, _ = collected
        assert result.ok, [str(f) for f in result.report.errors]
        assert result.report.source_count == 3

    def test_quotes_survive_a_round_trip_through_storage(self, collected) -> None:
        result, store, _ = collected
        assert result.run_id is not None
        loaded = store.load_quotes(result.run_id)
        assert len(loaded) == len(result.quotes)
        assert {q.dedup_key for q in loaded} == {q.dedup_key for q in result.quotes}
        # Including the dimensions that only exist in the multi-sport schema.
        assert {(q.sport, q.league) for q in loaded} == {
            (q.sport, q.league) for q in result.quotes
        }
        assert {q.home_participant for q in loaded} == {
            q.home_participant for q in result.quotes
        }

    def test_replaying_the_stored_run_reproduces_it_exactly(self, collected) -> None:
        result, store, raw_store = collected
        ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
        assert ok, problems

    def test_replay_reconciles_before_comparing_and_before_scoping(
        self, collected, monkeypatch
    ) -> None:
        """The regression the plain replay test structurally cannot catch.

        ``reconcile_event_keys`` rewrites ``event_key``, which is part of
        ``dedup_key``, so a stored row and a freshly parsed one disagree wherever
        reconciliation did anything.  ``replay_run`` originally compared *raw*
        parse output against *reconciled* stored rows, so it could never match
        those rows: on a live run a tennis match Pinnacle timed 5½ hours before
        FanDuel came back as "lost" at one date and "invented" at the next, and a
        soccer fixture two books timed 3 hours apart gained a phantom ``#2``.  Both
        were correct behaviour being reported as corruption.

        The captured fixtures need no reconciliation, which is exactly why the
        existing replay test passed throughout the bug — so this asserts the
        ordering itself rather than an outcome that depends on the capture.

        Both halves matter.  Reconciliation has to happen, and it has to happen
        *before* the scope filter: clustering start times is global, so narrowing to
        one sport first would cluster a subset and could legitimately produce keys
        that differ from the ones stored.
        """
        import src.collector as collector

        calls: list[list] = []
        real = collector.reconcile_event_keys

        def spy(quotes):
            calls.append(list(quotes))
            return real(quotes)

        monkeypatch.setattr(collector, "reconcile_event_keys", spy)

        result, store, raw_store = collected
        ok, problems = replay_run(
            result.run_id, store=store, raw_store=raw_store, sports=["hockey"]
        )
        assert ok, problems
        assert calls, "replay compared without reconciling — stored keys are reconciled"

        handed_over = calls[0]
        sports_seen = {quote.sport.value for quote in handed_over}
        assert len(sports_seen) > 1, (
            "reconciliation was handed a scoped subset; clustering is global and "
            f"must see every source's rows, got only {sports_seen}"
        )

    def test_replay_can_be_scoped_to_one_sport(self, collected) -> None:
        """Scoping must narrow both sides of the comparison; a scope applied to
        only the stored side would report every other sport as lost on replay."""
        result, store, raw_store = collected
        ok, problems = replay_run(
            result.run_id, store=store, raw_store=raw_store, sports=["hockey"]
        )
        assert ok, problems

    def test_arbitrage_over_stored_rows_matches_the_live_verdict(self, collected) -> None:
        """Detection must not depend on whether rows came from a parser or from
        SQLite — otherwise the stored history cannot be re-analysed."""
        result, store, _ = collected
        stored, _ = reconcile_event_keys(store.load_quotes(result.run_id))
        from_storage = find_opportunities(stored, as_of=FIXTURE_AS_OF)
        assert len(from_storage.opportunities) == len(result.arb.opportunities)
        assert from_storage.comparable_group_count == result.arb.comparable_group_count

    def test_validation_is_reproducible_from_storage(self, collected) -> None:
        result, store, _ = collected
        again = validate(store.load_quotes(result.run_id))
        # The run's own report also carries the coverage warnings the collector
        # adds, so validation's own findings are compared against themselves.
        collector_codes = {
            "sport_below_two_books",
            "sport_without_cross_book_fixtures",
            "league_returned_nothing",
            "source_declares_no_leagues",
            "event_key_reconciled",
            "insufficient_sources",
        }
        from_run = Counter(
            f.code for f in result.report.findings if f.code not in collector_codes
        )
        assert Counter(f.code for f in again.findings) == from_run


class TestMultiSportCoverage:
    """What the pipeline collected, stated per sport rather than as one total."""

    def test_several_sports_are_collected_in_one_run(self, collected) -> None:
        result, store, _ = collected
        sports = {q.sport for q in result.quotes}
        assert sports >= {
            Sport.BASEBALL, Sport.BASKETBALL, Sport.FOOTBALL,
            Sport.HOCKEY, Sport.SOCCER, Sport.TENNIS,
        }
        assert store.sports_for_run(result.run_id) == sorted(s.value for s in sports)

    def test_every_sport_is_reported_with_its_book_count_and_overlap(self, collected) -> None:
        result, _, _ = collected
        coverage = {entry.sport: entry for entry in result.coverage}
        # Every sport that produced a row is covered, and so is every sport some
        # source was configured for and got nothing from.
        assert {q.sport.value for q in result.quotes} <= set(coverage)
        assert {entry.sport for entry in result.league_coverage} <= set(coverage)

        for sport, entry in coverage.items():
            assert entry.quote_count == len(
                [q for q in result.quotes if q.sport.value == sport]
            )
            assert entry.event_count == len(
                {q.event_key for q in result.quotes if q.sport.value == sport}
            )
            # The overlap count is the number of fixtures more than one book
            # priced, and it is what decides comparability.
            books_per_event: dict[str, set[str]] = defaultdict(set)
            for quote in result.quotes:
                if quote.sport.value == sport:
                    books_per_event[quote.event_key].add(quote.source)
            assert entry.cross_book_events == sum(
                1 for books in books_per_event.values() if len(books) >= 2
            )
            assert entry.is_comparable == (
                entry.meets_two_book_bar and entry.cross_book_events > 0
            )

    def test_the_stores_own_query_agrees_with_the_runs_coverage(self, collected) -> None:
        result, store, _ = collected
        from_store = {row["sport"]: dict(row) for row in store.sport_coverage(result.run_id)}
        shared = store.cross_book_event_counts(result.run_id)
        for entry in result.coverage:
            if entry.quote_count == 0:
                continue
            assert from_store[entry.sport]["source_count"] == len(entry.sources)
            assert from_store[entry.sport]["quote_count"] == entry.quote_count
            assert shared.get(entry.sport, 0) == entry.cross_book_events

    def test_pinnacle_returning_no_nhl_is_reported_as_a_gap(self, collected) -> None:
        """The honest statement of a real hole in the slate.

        Pinnacle is configured for the NHL and returned nothing for it on this
        capture; its hockey rows are a friendly in another league entirely. Left
        unstated, that is invisible — hockey still has two other books — so it is
        recorded per source rather than inferred from a smaller number.
        """
        result, store, _ = collected
        gaps = {(entry.source_key, entry.league)
                for entry in result.league_coverage if entry.is_gap}
        assert ("pinnacle", "NHL") in gaps
        stored = {(row["source_key"], row["league"]): row["quote_count"]
                  for row in store.league_coverage(result.run_id)}
        assert stored[("pinnacle", "NHL")] == 0
        # Pinnacle did produce hockey — in a different competition — which is
        # exactly why the gap has to be per league and not per sport.
        assert any(
            q.sport is Sport.HOCKEY and q.source == "pinnacle" for q in result.quotes
        )
        assert {q.league for q in result.quotes if q.source == "pinnacle" and q.sport is Sport.HOCKEY} != {"NHL"}

    def test_hockey_is_only_comparable_where_two_books_share_a_fixture(self, collected) -> None:
        result, _, _ = collected
        books_per_event: dict[str, set[str]] = defaultdict(set)
        for quote in result.quotes:
            if quote.sport is Sport.HOCKEY:
                books_per_event[quote.event_key].add(quote.source)
        shared = {key for key, books in books_per_event.items() if len(books) >= 2}
        alone = {key for key, books in books_per_event.items() if len(books) == 1}
        # Both kinds exist in this capture, which is what makes the distinction
        # worth drawing rather than theoretical.
        assert shared and alone
        hockey = next(entry for entry in result.coverage if entry.sport == "hockey")
        assert hockey.cross_book_events == len(shared)

    def test_a_configured_league_nothing_returned_is_warned_about(self, collected) -> None:
        result, _, _ = collected
        codes = {f.code for f in result.report.warnings}
        assert "league_returned_nothing" in codes

    def test_coverage_is_reported_against_what_each_adapter_declares(
        self, collected, sources
    ) -> None:
        """Not against what came back: a league that returned nothing has to still
        appear, or every gap is invisible by construction."""
        result, _, _ = collected
        reported: dict[str, set[str]] = defaultdict(set)
        for entry in result.league_coverage:
            reported[entry.source_key].add(entry.league)

        for source in sources:
            declared = set(source.leagues)
            assert declared, f"{source.source_key} declares no leagues"
            # Every league the adapter says it collects is accounted for, whether
            # or not it produced a row.
            assert declared <= reported[source.source_key]

        # The NBA is the plainest case: declared, in its offseason, zero rows.
        assert "NBA" in reported["fanduel"]


class TestReconciliationIsApplied:
    def test_the_pipeline_reconciles_event_keys_before_validating(self, collected) -> None:
        """The doubleheader in the captured slate already agrees across books, so
        this asserts the wiring rather than a correction: every event key present
        must be one reconciliation would also produce."""
        result, _, _ = collected
        again, changes = reconcile_event_keys(result.quotes)
        assert changes == [], "the persisted rows should already be reconciled"
        assert {q.event_key for q in again} == {q.event_key for q in result.quotes}

    def test_reconciliation_runs_across_every_source_at_once(self, collected) -> None:
        """A per-source pass cannot produce this: the keys agree across books, on
        fixtures each book numbered over its own slate."""
        result, _, _ = collected
        shared = defaultdict(set)
        for quote in result.quotes:
            shared[quote.event_key].add(quote.source)
        multi = [key for key, books in shared.items() if len(books) > 1]
        assert len(multi) >= 40
        # Participant keys are namespaced, so no two sports can share a key.
        assert all("@" in key and ":" in key for key in shared)


class TestStoredArtifacts:
    def test_raw_responses_are_written_before_anything_interprets_them(
        self, collected
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
            row["raw_ref"]
            for row in store.query(
                "SELECT raw_ref FROM raw_response WHERE run_id = ?", (result.run_id,)
            )
        }
        assert stored_refs
        for quote in result.quotes:
            assert quote.raw_ref in stored_refs, f"orphaned raw_ref {quote.raw_ref}"
            if quote.identity_raw_ref is not None:
                assert quote.identity_raw_ref in stored_refs, (
                    f"orphaned identity_raw_ref {quote.identity_raw_ref}"
                )

    def test_identity_provenance_survives_storage(self, collected) -> None:
        """Pinnacle owes its participants to a different response than its prices,
        and that pointer has to still be there after a round trip."""
        result, store, _ = collected
        stored = store.load_quotes(result.run_id)
        with_identity = [q for q in stored if q.identity_raw_ref is not None]
        assert with_identity, "no row carried separate identity provenance"
        assert {q.identity_raw_ref for q in with_identity} == {
            q.identity_raw_ref for q in result.quotes if q.identity_raw_ref is not None
        }

    def test_findings_are_persisted_for_the_run(self, collected) -> None:
        result, store, _ = collected
        rows = store.query("SELECT code FROM finding WHERE run_id = ?", (result.run_id,))
        assert len(rows) == len(result.report.findings)

    def test_the_runs_note_records_which_sports_were_comparable(self, collected) -> None:
        result, store, _ = collected
        note = store.run_summaries()[0]["note"]
        assert note
        for sport in result.usable_sports:
            assert sport in note


class TestScoping:
    def test_collecting_one_sport_stores_only_that_sport(self, tmp_path: Path, sources) -> None:
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                sources, raw_store=raw_store, store=store, sports=["hockey"]
            )
            assert {q.sport for q in result.quotes} == {Sport.HOCKEY}
            assert store.sports_for_run(result.run_id) == ["hockey"]
            # The rows that were parsed and then excluded are counted, not lost.
            assert result.excluded_by_filter > 0
            assert (
                result.excluded_by_filter + len(result.quotes)
                == sum(h.quote_count for h in result.health)
            )
            assert "excluded by filter" in store.run_summaries()[0]["note"]

    def test_collecting_one_league_stores_only_that_league(self, tmp_path: Path, sources) -> None:
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                sources, raw_store=raw_store, store=store, leagues=["MLB"]
            )
        assert {q.league for q in result.quotes} == {"MLB"}
        assert {q.sport for q in result.quotes} == {Sport.BASEBALL}

    def test_a_scoped_run_only_reports_gaps_inside_its_scope(self, tmp_path: Path, sources) -> None:
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                sources, raw_store=raw_store, store=store, sports=["hockey"]
            )
        assert {entry.sport for entry in result.league_coverage} == {"hockey"}
        assert ("pinnacle", "NHL") in {
            (entry.source_key, entry.league) for entry in result.league_coverage if entry.is_gap
        }


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

    def test_runs_lists_the_stored_run_with_per_sport_coverage(self, populated, capsys) -> None:
        assert populated.main(["runs"]) == 0
        out = capsys.readouterr().out
        assert "fanduel" in out
        for sport in ("baseball", "hockey", "tennis"):
            assert sport in out
        # And it says which sports are comparable, in words.
        assert "usable" in out or "NO OVERLAP" in out or "1 BOOK ONLY" in out
        assert "gap: pinnacle returned nothing for NHL" in out

    def test_runs_can_be_narrowed_to_one_sport(self, populated, capsys) -> None:
        assert populated.main(["runs", "--sport", "hockey"]) == 0
        out = capsys.readouterr().out
        # The per-sport rows are the scoped part; the run's own note still says
        # what the whole run collected, which is a fact about the run and not a
        # leak of the filter.
        coverage_rows = [
            line for line in out.splitlines()
            if line.startswith(" " * 10) and not line.strip().startswith(("gap:", "note:"))
        ]
        assert any("hockey" in line for line in coverage_rows)
        assert not any("tennis" in line for line in coverage_rows)

    def test_show_prints_sport_and_league_on_every_row(self, populated, capsys) -> None:
        assert populated.main(["show", "--limit", "5"]) == 0
        out = capsys.readouterr().out
        assert "moneyline" in out or "total" in out
        assert "baseball" in out or "basketball" in out

    def test_show_can_be_narrowed_to_one_league(self, populated, capsys) -> None:
        assert populated.main(["show", "--league", "NHL", "--limit", "20"]) == 0
        out = capsys.readouterr().out
        assert "NHL" in out
        assert "league=NHL" in out
        assert "MLB" not in out

    def test_show_can_be_narrowed_to_one_sport(self, populated, capsys) -> None:
        assert populated.main(["show", "--sport", "tennis", "--limit", "10"]) == 0
        out = capsys.readouterr().out
        assert "tennis" in out
        assert "baseball" not in out

    def test_arb_reports_no_opportunities_and_says_what_it_compared(
        self, populated, capsys
    ) -> None:
        assert populated.main(["arb"]) == 0
        out = capsys.readouterr().out
        assert "0 opportunities" in out
        assert "cross-book markets" in out

    def test_arb_accepts_a_bankroll_a_threshold_and_a_scope(self, populated, capsys) -> None:
        assert populated.main(
            ["arb", "--stake", "500", "--min-margin", "1.5", "--sport", "baseball"]
        ) == 0
        out = capsys.readouterr().out
        assert "cross-book markets" in out
        assert "sport=baseball" in out

    def test_arb_verbose_explains_rejections(self, populated, capsys) -> None:
        assert populated.main(["arb", "--verbose"]) == 0

    def test_replay_passes_for_the_stored_run(self, populated, capsys) -> None:
        assert populated.main(["replay"]) == 0
        assert "PASS" in capsys.readouterr().out

    def test_replay_can_be_scoped(self, populated, capsys) -> None:
        assert populated.main(["replay", "--sport", "hockey"]) == 0
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "sport=hockey" in out

    def test_lines_shows_the_best_price_surface_with_its_sport(self, populated, capsys) -> None:
        assert populated.main(["lines", "--cross-book-only", "--limit", "5"]) == 0
        out = capsys.readouterr().out
        assert "market(s) shown of" in out
        assert "sum " in out
        # Each market says which sport and league it belongs to, because the same
        # market name means different contracts in different sports.
        assert "[baseball/MLB]" in out or "[hockey/NHL]" in out

    def test_lines_can_be_narrowed_to_one_sport(self, populated, capsys) -> None:
        assert populated.main(
            ["lines", "--sport", "hockey", "--cross-book-only", "--limit", "50"]
        ) == 0
        out = capsys.readouterr().out
        assert "[hockey/" in out
        assert "[baseball/" not in out

    def test_health_summarises_each_source_and_each_sport(self, populated, capsys) -> None:
        populated.main(["health"])
        out = capsys.readouterr().out
        for source in ("fanduel", "pinnacle", "betrivers_kambi"):
            assert source in out
        for sport in ("baseball", "hockey"):
            assert sport in out
        assert "priced by two or more books" in out

    def test_health_can_be_narrowed_to_one_sport(self, populated, capsys) -> None:
        populated.main(["health", "--sport", "hockey"])
        out = capsys.readouterr().out
        assert "hockey" in out
        assert "tennis" not in out

    def test_an_unknown_source_is_refused(self, populated) -> None:
        with pytest.raises(SystemExit):
            populated.main(["collect", "--source", "definitely-not-a-book"])

    def test_an_unknown_league_is_refused(self, populated) -> None:
        with pytest.raises(SystemExit):
            populated.main(["collect", "--league", "NOT_A_LEAGUE"])

    def test_migrate_upgrades_an_old_database_from_the_command_line(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        import src.collector
        import src.settings
        from tests.test_pipeline import write_v3_database

        old = tmp_path / "legacy.sqlite3"
        write_v3_database(old)
        monkeypatch.setattr(src.settings, "DB_PATH", old)

        assert src.collector.main(["migrate"]) == 0
        out = capsys.readouterr().out
        assert "v3 -> v4" in out
        assert database_version(old) == 4
        # Idempotent, and it says so rather than pretending to work again.
        assert src.collector.main(["migrate"]) == 0
        assert "already at schema version 4" in capsys.readouterr().out

    def test_a_command_pointed_at_an_incompatible_database_exits_with_advice(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import src.collector
        import src.settings
        from tests.test_pipeline import write_v3_database

        old = tmp_path / "legacy.sqlite3"
        write_v3_database(old)
        monkeypatch.setattr(src.settings, "DB_PATH", old)

        with pytest.raises(SystemExit) as caught:
            src.collector.main(["runs"])
        message = str(caught.value)
        assert "schema version 3" in message and "migrate" in message
        assert database_version(old) == 3


class TestTheDashboardOverARealRun:
    """The report is a view over what was collected, so it is built here from a
    real run rather than only from synthetic rows."""

    def test_the_page_states_every_sport_and_its_book_count(self, collected) -> None:
        from src.report import build_report, render_page

        result, store, _ = collected
        data = build_report(store)
        page = render_page(data)

        sports = {entry["sport"]: entry for entry in data["runs"][0]["sports"]}
        for entry in result.coverage:
            if entry.quote_count == 0:
                continue
            assert sports[entry.sport]["books"] == list(entry.sources)
            assert sports[entry.sport]["quote_count"] == entry.quote_count
            assert sports[entry.sport]["cross_book_events"] == entry.cross_book_events
            assert sports[entry.sport]["comparable"] == entry.is_comparable
            assert entry.sport in page

        # The gap Pinnacle has in the NHL is on the page too, not only in the log.
        assert ("pinnacle", "NHL") in {
            (gap["source"], gap["league"]) for gap in data["runs"][0]["league_gaps"]
        }

    def test_the_page_is_self_contained(self, collected) -> None:
        import re

        from src.report import build_report, render_page

        _, store, _ = collected
        page = render_page(build_report(store))
        payload = re.search(
            r'<script type="application/json" id="report-data">.*?</script>', page, re.S
        )
        # The payload legitimately contains the endpoint URLs the collector
        # recorded; they are data on the page, not links off it.
        markup = page.replace(payload.group(0), "")
        assert "http://" not in markup and "https://" not in markup
        static = re.sub(r"<script.*?</script>", "", page, flags=re.S)
        assert [
            url for url in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', static)
            if not url.startswith("#")
        ] == []


class TestThePipelineCanActuallySurfaceAnOpportunity:
    """Guards against the integration being silently broken.

    Every other end-to-end assertion here is that the captured slate yields no
    arbitrage, which is the correct answer but is also what a disconnected
    detector returns.  These tests inject two **synthetic** books priced to a
    known edge and follow it all the way through the pipeline to the command
    line.  The prices are invented for the test and are not observed data.

    ``as_of`` is pinned to :data:`FIXTURE_AS_OF` for the same reason every other
    test in this file pins it.  Left to default to ``datetime.now``, these four
    passed until the wall clock reached the fixture's 22:41 UTC kick-off and
    then failed for good — the started-game gate was doing its job on a slate
    frozen in the past.  The one guard against a silently disconnected detector
    was itself on a timer, and the failure looked like a real regression rather
    than a stale fixture.

    ``jurisdiction="GLOBAL"`` for the same reason: ``collect_once`` defaults to
    :data:`settings.STATE` and ``withhold_non_local`` runs unconditionally, so
    ``book_a``/``book_b`` — invented keys with no entry in the registry at
    all — read as unreachable from whatever state the fallback picked, and the
    one opportunity these tests inject was silently withheld. These tests are
    about the detector, not about any state's retail licensing.
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
                leagues = ("MLB",)

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
            result = collect_once(
                rigged_sources, raw_store=raw_store, store=store, as_of=FIXTURE_AS_OF,
                jurisdiction="GLOBAL",
            )

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
            result = collect_once(
                rigged_sources, raw_store=raw_store, store=store, as_of=FIXTURE_AS_OF,
                jurisdiction="GLOBAL",
            )
        legs = {leg.selection.value: leg for leg in result.arb.opportunities[0].legs}
        assert legs["home"].source == "book_a" and legs["home"].decimal_odds == 2.30
        assert legs["away"].source == "book_b" and legs["away"].decimal_odds == 2.05

    def test_the_summary_prints_the_opportunity_and_the_coverage(
        self, tmp_path: Path, rigged_sources, capsys
    ) -> None:
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                rigged_sources, raw_store=raw_store, store=store, as_of=FIXTURE_AS_OF,
                jurisdiction="GLOBAL",
            )
        result.print_summary()
        out = capsys.readouterr().out
        assert "arbitrage: 1 opportunity" in out
        assert "book_a" in out and "book_b" in out
        assert "outcomes:" in out
        # And the per-sport verdict is printed on every run, not only on failure.
        assert "per-sport coverage" in out
        assert "baseball" in out

    def test_it_survives_the_round_trip_through_storage(
        self, tmp_path: Path, rigged_sources
    ) -> None:
        """Detection over stored rows must match detection over parsed rows, or
        the stored history cannot be re-analysed."""
        raw_store = RawStore(tmp_path / "raw")
        with Store(tmp_path / "db.sqlite3") as store:
            result = collect_once(
                rigged_sources, raw_store=raw_store, store=store, as_of=FIXTURE_AS_OF,
                jurisdiction="GLOBAL",
            )
            stored, _ = reconcile_event_keys(store.load_quotes(result.run_id))
        again = find_opportunities(stored)
        assert len(again.opportunities) == 1
        assert again.opportunities[0].margin == pytest.approx(
            result.arb.opportunities[0].margin
        )


class TestCrossSourceCoverage:
    """What the pipeline can actually compare, stated as facts about the slate."""

    def test_the_core_full_game_markets_are_cross_book_in_baseball(self, collected) -> None:
        result, _, _ = collected
        baseball = [q for q in result.quotes if q.sport is Sport.BASEBALL]
        cross_book = {
            key
            for key, selections in best_prices(baseball).items()
            if len({q.source for q in selections.values()}) > 1
        }
        markets = {(key[1], key[2]) for key in cross_book}
        assert (Market.MONEYLINE, Period.FULL_GAME) in markets
        assert (Market.SPREAD, Period.FULL_GAME) in markets
        assert (Market.TOTAL, Period.FULL_GAME) in markets

    def test_a_market_is_never_compared_across_two_sports(self, collected) -> None:
        """``spread`` means runs in baseball and goals in hockey.  Grouping is on
        the fixture, and participant keys are namespaced per sport, so no group can
        contain two sports — this asserts that rather than assuming it."""
        result, _, _ = collected
        sport_of = {q.event_key: q.sport for q in result.quotes}
        for key, selections in best_prices(result.quotes).items():
            sports = {sport_of[quote.event_key] for quote in selections.values()}
            assert len(sports) == 1, key

    def test_full_game_and_regulation_are_never_mixed(self, collected) -> None:
        """A 60-minute hockey market is three-way; the same market including the
        shootout is two-way. Pairing them looks like a huge edge on fair prices."""
        result, _, _ = collected
        for key, selections in best_prices(result.quotes).items():
            assert len({quote.period for quote in selections.values()}) == 1, key
        hockey_periods = {
            q.period for q in result.quotes if q.sport is Sport.HOCKEY
        }
        # Both windows really are collected, so the guard is not vacuous.
        assert {Period.FULL_GAME, Period.REGULATION} <= hockey_periods
