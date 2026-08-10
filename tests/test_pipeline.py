"""Raw capture, persistence, schema compatibility, and the collect/replay loop.

Nothing here touches the network.  Rows are built with ``tests.conftest.make_quote``
so that what is under test is storage and the pipeline's wiring, not the adapters'
parsing — which has its own tests against captured payloads.  Every price in this
file is invented.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.raw_store import RawResponse, RawStore
from src.schema import Market, Period, Quote, Selection, Side, Sport
from src.store import (
    SCHEMA_VERSION,
    IncompatibleDatabase,
    MigrationError,
    Store,
    database_version,
    migrate_database,
)
from tests.conftest import make_quote, make_raw

# ── raw store ────────────────────────────────────────────────────────────────


def test_raw_response_round_trips_verbatim(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    raw = make_raw('{"events": [{"id": 1}]}')
    path = store.write(raw)
    loaded = store.read(path)

    assert loaded.body == raw.body
    assert loaded == raw
    assert loaded.json() == {"events": [{"id": 1}]}
    assert loaded.sha256 == raw.sha256


def test_raw_ref_is_stable_and_content_addressed() -> None:
    first = make_raw('{"a": 1}')
    same = make_raw('{"a": 1}')
    different = make_raw('{"a": 2}')
    assert first.ref == same.ref
    assert first.ref != different.ref
    assert first.source in first.ref


def test_tampered_raw_file_is_detected(tmp_path: Path) -> None:
    """A stored payload edited after the fact must not silently replay."""
    store = RawStore(tmp_path)
    path = store.write(make_raw('{"a": 1}'))
    envelope = json.loads(path.read_text())
    envelope["body"] = '{"a": 999}'
    path.write_text(json.dumps(envelope))

    with pytest.raises(ValueError, match="sha256"):
        store.read(path)


def test_sha_stripped_raw_file_is_refused(tmp_path: Path) -> None:
    """Deleting the recorded hash must not delete the verification.

    ``from_envelope`` skipped the check when the field was absent, so the
    tamper above became undetectable the moment the tamperer also removed
    the reference value it would have been caught against — rewrite the
    body, drop ``sha256``, and the envelope read back clean.  Every envelope
    this codebase has ever written records the hash (all 5,100 stored
    envelopes measured 2026-08-10, every vintage v1–v3), so absence is
    itself the tamper signal.
    """
    store = RawStore(tmp_path)
    path = store.write(make_raw('{"a": 1}'))
    envelope = json.loads(path.read_text())
    envelope["body"] = '{"a": 999}'
    del envelope["sha256"]
    path.write_text(json.dumps(envelope))

    with pytest.raises(ValueError, match="sha256"):
        store.read(path)


def test_naive_fetched_at_is_rejected() -> None:
    with pytest.raises(ValueError):
        RawResponse(
            source="s", endpoint="e", url="u", status_code=200, body="{}",
            fetched_at=datetime(2026, 7, 28, 7, 0),
        )


def test_latest_finds_the_most_recent_response(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    older = make_raw('{"v": 1}')
    newer = RawResponse(
        source=older.source, endpoint=older.endpoint, url=older.url, status_code=200,
        body='{"v": 2}', fetched_at=older.fetched_at + timedelta(minutes=5),
    )
    store.write(older)
    store.write(newer)
    assert store.latest(older.source).json() == {"v": 2}


# ── multi-sport rows ─────────────────────────────────────────────────────────


def hockey_quote(**overrides) -> Quote:
    """A hockey row.  Regulation hockey is three-way; full game is not."""
    defaults = dict(
        source_market_id="nhl-1",
        sport=Sport.HOCKEY,
        league="NHL",
        event_key="NHL-BOS@NHL-TOR:2026-09-29",
        home_participant="NHL-TOR",
        away_participant="NHL-BOS",
        home_team="Toronto Maple Leafs",
        away_team="Boston Bruins",
        commence_time=datetime(2026, 9, 29, 23, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return make_quote(**defaults)


def soccer_quote(**overrides) -> Quote:
    defaults = dict(
        source_market_id="epl-1",
        sport=Sport.SOCCER,
        league="EPL",
        event_key="SOCCER-arsenal@SOCCER-chelsea:2026-08-15",
        home_participant="SOCCER-chelsea",
        away_participant="SOCCER-arsenal",
        home_team="Chelsea",
        away_team="Arsenal",
        commence_time=datetime(2026, 8, 15, 14, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return make_quote(**defaults)


def tennis_quote(**overrides) -> Quote:
    defaults = dict(
        source_market_id="atp-1",
        sport=Sport.TENNIS,
        league="ATP",
        # Tennis has no home player, so src.events.orient imposes its own order by
        # participant key.  A fixture that ignored that would be rejected by
        # validation for exactly the right reason, so it follows it.
        event_key="TENNIS-alcarazcarlos@TENNIS-humbertugo:2026-07-28",
        home_participant="TENNIS-humbertugo",
        away_participant="TENNIS-alcarazcarlos",
        home_team="Ugo Humbert",
        away_team="Carlos Alcaraz",
    )
    defaults.update(overrides)
    return make_quote(**defaults)


def multi_sport_quotes(source: str = "bookA") -> list[Quote]:
    """One row per sport, each legal for that sport's settlement rules."""
    return [
        make_quote(source=source, source_market_id="mlb-ml"),
        hockey_quote(source=source, source_market_id="nhl-ml"),
        hockey_quote(
            source=source, period=Period.REGULATION, selection=Selection.DRAW,
            decimal_odds=4.2, source_market_id="nhl-reg",
        ),
        soccer_quote(source=source, selection=Selection.DRAW, decimal_odds=3.4,
                     source_market_id="epl-ml"),
        tennis_quote(source=source, source_market_id="atp-ml"),
    ]


# ── store: multi-sport persistence ───────────────────────────────────────────


def test_quotes_round_trip_through_sqlite(tmp_path: Path) -> None:
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        original = [
            make_quote(source="bookA", selection=Selection.HOME),
            make_quote(source="bookA", selection=Selection.AWAY, decimal_odds=2.05),
            make_quote(source="bookA", market=Market.TOTAL, selection=Selection.OVER,
                       line=8.5, decimal_odds=1.92),
            make_quote(source="bookA", market=Market.TEAM_TOTAL, selection=Selection.OVER,
                       side=Side.HOME, line=4.5, decimal_odds=1.87),
        ]
        assert store.save_quotes(run_id, original) == 4
        assert store.load_quotes(run_id) == original


def test_every_sport_survives_the_round_trip_unchanged(tmp_path: Path) -> None:
    """Sport, league and both participant keys are what everything downstream
    joins on, so a lossy round trip would break comparison rather than the row."""
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        original = multi_sport_quotes()
        assert store.save_quotes(run_id, original) == len(original)

        loaded = store.load_quotes(run_id)
        assert loaded == original
        assert {q.sport for q in loaded} == {
            Sport.BASEBALL, Sport.HOCKEY, Sport.SOCCER, Sport.TENNIS
        }
        assert {q.league for q in loaded} == {"MLB", "NHL", "EPL", "ATP"}
        for before, after in zip(original, loaded, strict=True):
            assert (after.home_participant, after.away_participant) == (
                before.home_participant, before.away_participant
            )
            assert after.dedup_key == before.dedup_key


def test_identity_provenance_is_stored_when_it_differs_from_the_price(tmp_path: Path) -> None:
    """A row whose teams came from a different response than its price must keep
    the pointer, or that provenance is unrecoverable from storage."""
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        quote = make_quote(
            raw_ref="pinnacle/x/markets", identity_raw_ref="pinnacle/x/matchups"
        )
        store.save_quotes(run_id, [quote])
        loaded = store.load_quotes(run_id)[0]
        assert loaded.raw_ref == "pinnacle/x/markets"
        assert loaded.identity_raw_ref == "pinnacle/x/matchups"
        assert store.load_quotes(run_id) == [quote]


def test_duplicate_selection_is_rejected_at_the_storage_boundary(tmp_path: Path) -> None:
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        quote = make_quote()
        store.save_quotes(run_id, [quote])
        with pytest.raises(sqlite3.IntegrityError):
            store.save_quotes(run_id, [quote])


def test_the_unique_constraint_covers_the_whole_dedup_key(tmp_path: Path) -> None:
    """Rows that differ only in a dedup-key component must all be storable, and
    two that differ in nothing must not be.  A constraint that is too tight
    rejects real data; one that is too loose stores the same price twice."""
    def row(**overrides) -> Quote:
        return make_quote(source="bookA", **overrides)

    distinct = [
        row(),
        row(selection=Selection.AWAY, decimal_odds=2.05),
        row(period=Period.FIRST_5_INNINGS, decimal_odds=1.95),
        row(market=Market.TOTAL, selection=Selection.OVER, line=8.5),
        row(market=Market.TOTAL, selection=Selection.OVER, line=9.5),
        # Same bet at the same number, on the book's alternate-line market. Both
        # rows are real; is_alternate is what keeps them apart.
        row(market=Market.TOTAL, selection=Selection.OVER, line=8.5,
            is_alternate=True, decimal_odds=1.83),
        row(market=Market.TEAM_TOTAL, selection=Selection.OVER, side=Side.HOME, line=4.5),
        row(market=Market.TEAM_TOTAL, selection=Selection.OVER, side=Side.AWAY, line=4.5),
        make_quote(source="bookB"),
        hockey_quote(source="bookA"),
    ]
    assert len({q.dedup_key for q in distinct}) == len(distinct)

    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        assert store.save_quotes(run_id, distinct) == len(distinct)

        # A true conflict: same book, same fixture, same market, same number,
        # same side of it — two prices for one bet.
        conflict = row(decimal_odds=1.99)
        assert conflict.dedup_key == distinct[0].dedup_key
        with pytest.raises(sqlite3.IntegrityError):
            store.save_quotes(run_id, [conflict])

        # And the same rows in the *next* run are not a conflict.
        second = store.start_run(datetime.now(UTC))
        assert store.save_quotes(second, distinct) == len(distinct)


def test_quotes_can_be_read_back_per_sport_and_per_league(tmp_path: Path) -> None:
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        store.save_quotes(run_id, multi_sport_quotes())

        assert store.sports_for_run(run_id) == ["baseball", "hockey", "soccer", "tennis"]
        assert store.leagues_for_run(run_id) == ["ATP", "EPL", "MLB", "NHL"]
        assert store.leagues_for_run(run_id, sports="hockey") == ["NHL"]

        hockey = store.load_quotes(run_id, sports="hockey")
        assert len(hockey) == 2
        assert {q.sport for q in hockey} == {Sport.HOCKEY}

        two = store.load_quotes(run_id, sports=[Sport.HOCKEY, Sport.TENNIS])
        assert {q.sport.value for q in two} == {"hockey", "tennis"}

        assert len(store.load_quotes(run_id, leagues="EPL")) == 1
        # A scope naming a sport and a league that do not intersect is empty, not
        # an error: it is a legitimate question with the answer "nothing".
        assert store.load_quotes(run_id, sports="hockey", leagues="EPL") == []


def test_per_sport_coverage_counts_books_and_overlap(tmp_path: Path) -> None:
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        # Two books on the same baseball fixture; hockey from one book only.
        store.save_quotes(
            run_id,
            [
                make_quote(source="bookA", source_market_id="a"),
                make_quote(source="bookB", source_market_id="b"),
                hockey_quote(source="bookA", source_market_id="ha"),
            ],
        )
        coverage = {row["sport"]: dict(row) for row in store.sport_coverage(run_id)}
        assert coverage["baseball"]["source_count"] == 2
        assert coverage["hockey"]["source_count"] == 1
        assert coverage["baseball"]["event_count"] == 1

        shared = store.cross_book_event_counts(run_id)
        assert shared == {"baseball": 1}   # hockey has no fixture two books priced

        matrix = {(row["sport"], row["source"]): row["quote_count"]
                  for row in store.sport_source_matrix(run_id)}
        assert matrix == {
            ("baseball", "bookA"): 1, ("baseball", "bookB"): 1, ("hockey", "bookA"): 1
        }


def test_runs_can_be_listed_per_sport(tmp_path: Path) -> None:
    """A run that collected no hockey is not a hockey run, and must not be listed
    as one — otherwise ``runs --sport hockey`` implies coverage that is not there."""
    with Store(tmp_path / "db.sqlite3") as store:
        baseball_only = store.start_run(datetime.now(UTC))
        store.save_quotes(baseball_only, [make_quote()])
        store.finish_run(
            baseball_only, finished_at=datetime.now(UTC), report=_Report(1, 1)
        )
        both = store.start_run(datetime.now(UTC))
        store.save_quotes(both, [make_quote(), hockey_quote()])
        store.finish_run(both, finished_at=datetime.now(UTC), report=_Report(2, 2))

        assert [row["id"] for row in store.run_summaries()] == [both, baseball_only]
        assert [row["id"] for row in store.run_summaries(sports="hockey")] == [both]
        assert [row["id"] for row in store.run_summaries(leagues="MLB")] == [
            both, baseball_only
        ]
        assert store.run_summaries(sports="tennis") == []


def test_configured_league_coverage_records_gaps(tmp_path: Path) -> None:
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        store.save_league_coverage(
            run_id, "bookA", [("MLB", "baseball", 12, 2), ("NHL", "hockey", 0, 0)]
        )
        rows = {row["league"]: dict(row) for row in store.league_coverage(run_id)}
        assert rows["MLB"]["quote_count"] == 12
        assert rows["NHL"]["quote_count"] == 0
        assert rows["NHL"]["configured"] == 1


class _Report:
    """Minimal stand-in for a validation report; storage is duck-typed."""

    def __init__(self, quote_count: int, event_count: int) -> None:
        self.quote_count = quote_count
        self.event_count = event_count
        self.errors: list[object] = []
        self.warnings: list[object] = []
        self.findings: list[object] = []
        self.ok = True


def test_run_lifecycle_and_health_are_recorded(tmp_path: Path) -> None:
    from src.sources.base import SourceHealth

    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        store.save_health(
            run_id,
            SourceHealth(source_key="bookA", ok=False, checked_at=datetime.now(UTC),
                         error_kind="blocked", error_message="HTTP 403"),
        )
        report = _Report(0, 0)
        report.ok = False
        store.finish_run(run_id, finished_at=datetime.now(UTC), report=report, note="scoped")

        assert store.latest_run_id() == run_id
        health = store.health_for_run(run_id)
        assert len(health) == 1
        assert health[0]["error_kind"] == "blocked"
        assert bool(store.run_summaries()[0]["ok"]) is False
        assert store.run_summaries()[0]["note"] == "scoped"


# ── schema compatibility and the old database ────────────────────────────────

#: The v3 ``quote`` table, verbatim, so the migration is tested against the shape
#: the existing ``data/collector.sqlite3`` actually has rather than a paraphrase.
V3_SCHEMA = """
CREATE TABLE schema_meta (version INTEGER NOT NULL);
CREATE TABLE collection_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    ok INTEGER, quote_count INTEGER NOT NULL DEFAULT 0, event_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0, warning_count INTEGER NOT NULL DEFAULT 0, note TEXT
);
CREATE TABLE raw_response (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES collection_run(id),
    source TEXT NOT NULL, endpoint TEXT NOT NULL, url TEXT NOT NULL, status_code INTEGER NOT NULL,
    content_type TEXT, fetched_at TEXT NOT NULL, sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL,
    path TEXT NOT NULL, raw_ref TEXT NOT NULL, headers TEXT, unchanged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE quote (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES collection_run(id),
    source TEXT NOT NULL, observed_at TEXT NOT NULL, raw_ref TEXT NOT NULL, sport TEXT NOT NULL,
    league TEXT NOT NULL, event_key TEXT NOT NULL, source_event_id TEXT NOT NULL,
    home_team TEXT NOT NULL, away_team TEXT NOT NULL, commence_time TEXT NOT NULL,
    market TEXT NOT NULL, period TEXT NOT NULL, selection TEXT NOT NULL, side TEXT, line REAL,
    is_alternate INTEGER NOT NULL DEFAULT 0, decimal_odds REAL NOT NULL,
    american_odds INTEGER NOT NULL, implied_probability REAL NOT NULL, source_market_id TEXT,
    source_selection_id TEXT, limit_amount REAL, status TEXT NOT NULL, last_change_at TEXT,
    dedup_key TEXT NOT NULL, UNIQUE (run_id, dedup_key)
);
CREATE TABLE source_health (
    run_id INTEGER NOT NULL REFERENCES collection_run(id), source_key TEXT NOT NULL,
    ok INTEGER NOT NULL, checked_at TEXT NOT NULL, request_count INTEGER NOT NULL DEFAULT 0,
    raw_bytes INTEGER NOT NULL DEFAULT 0, latency_ms REAL, quote_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0, rejection_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0, unchanged_payloads INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT, error_message TEXT, PRIMARY KEY (run_id, source_key)
);
CREATE TABLE rejection (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES collection_run(id),
    source TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL, context TEXT
);
CREATE TABLE skipped (
    run_id INTEGER NOT NULL REFERENCES collection_run(id), source TEXT NOT NULL,
    reason TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (run_id, source, reason)
);
CREATE TABLE finding (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES collection_run(id),
    severity TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, source TEXT, event_key TEXT
);
"""

#: Rows as v3 stored them: baseball-only, old market names, event keys built from
#: bare abbreviations, and a dedup key with no ``is_alternate`` component.
V3_ROWS = [
    ("fanduel", "moneyline", "full_game", "home", None, None, "PHI@MIA:2026-07-28",
     "Miami Marlins", "Philadelphia Phillies"),
    ("fanduel", "run_line", "full_game", "home", -1.5, None, "PHI@MIA:2026-07-28",
     "Miami Marlins", "Philadelphia Phillies"),
    ("fanduel", "total_runs", "full_game", "over", 8.5, None, "PHI@MIA:2026-07-28",
     "Miami Marlins", "Philadelphia Phillies"),
    ("pinnacle", "team_total_runs", "full_game", "over", 4.5, "home", "PHI@MIA:2026-07-28",
     "Miami Marlins", "Philadelphia Phillies"),
    # A doubleheader ordinal, which the migration must preserve.
    ("pinnacle", "moneyline", "full_game", "away", None, None, "CLE@CIN:2026-07-28#2",
     "Cincinnati Reds", "Cleveland Guardians"),
]


def write_v3_database(path: Path, rows=V3_ROWS) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(V3_SCHEMA)
        conn.execute("INSERT INTO schema_meta (version) VALUES (3)")
        conn.execute(
            "INSERT INTO collection_run (id, started_at, finished_at, ok) VALUES (1, ?, ?, 1)",
            ("2026-07-28T07:00:00+00:00", "2026-07-28T07:00:05+00:00"),
        )
        for index, (source, market, period, selection, line, side, event_key, home, away) in enumerate(rows):
            dedup = "|".join(
                [source, event_key, market, period, side or "", selection,
                 "" if line is None else f"{line:g}"]
            )
            conn.execute(
                """INSERT INTO quote
                   (run_id, source, observed_at, raw_ref, sport, league, event_key,
                    source_event_id, home_team, away_team, commence_time, market, period,
                    selection, side, line, is_alternate, decimal_odds, american_odds,
                    implied_probability, source_market_id, source_selection_id, limit_amount,
                    status, last_change_at, dedup_key)
                   VALUES (1,?,?,?,'baseball','MLB',?,?,?,?,?,?,?,?,?,?,0,1.9,-111,0.5263,?,NULL,NULL,'active',NULL,?)""",
                (
                    source, "2026-07-28T07:00:00+00:00", f"{source}/x/{index}", event_key,
                    f"evt-{index}", home, away, "2026-07-28T22:41:00+00:00", market, period,
                    selection, side, line, f"mkt-{index}", dedup,
                ),
            )
        conn.execute(
            "INSERT INTO finding (run_id, severity, code, message, source, event_key) "
            "VALUES (1, 'warning', 'stale_price', 'invented', 'fanduel', 'PHI@MIA:2026-07-28')"
        )
    conn.close()


def test_an_old_database_is_refused_rather_than_written_into(tmp_path: Path) -> None:
    """Appending v4 rows to a v3 table would produce one table whose rows mean two
    different things, and nothing downstream could tell which was which."""
    path = tmp_path / "old.sqlite3"
    write_v3_database(path)

    with pytest.raises(IncompatibleDatabase) as caught:
        Store(path)

    message = str(caught.value)
    assert "schema version 3" in message
    assert "version 4" in message
    # The error has to tell the operator what to do, in runnable form.
    assert "migrate" in message
    assert "ODDS_DB_PATH" in message
    # And it must not have touched the file.
    assert database_version(path) == 3
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM quote").fetchone()[0] == len(V3_ROWS)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(quote)")}
    assert "home_participant" not in columns
    conn.close()


def test_an_unversioned_database_is_refused(tmp_path: Path) -> None:
    """A database with tables but no version marker predates versioning; guessing
    that it is compatible is exactly the silent corruption to avoid."""
    path = tmp_path / "ancient.sqlite3"
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE quote (id INTEGER PRIMARY KEY, thing TEXT)")
    conn.close()

    with pytest.raises(IncompatibleDatabase, match="no version marker"):
        Store(path)


def test_a_fresh_database_is_created_at_the_current_version(tmp_path: Path) -> None:
    path = tmp_path / "new.sqlite3"
    with Store(path):
        pass
    assert database_version(path) == SCHEMA_VERSION


def test_migrating_preserves_every_row_and_upgrades_its_meaning(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    write_v3_database(path)

    migration = migrate_database(path)
    assert migration.from_version == 3
    assert migration.to_version == SCHEMA_VERSION
    assert migration.quotes_migrated == len(V3_ROWS)
    assert migration.markets_renamed == {"run_line": 1, "total_runs": 1, "team_total_runs": 1}
    # The pre-migration file is kept, so a mistake here is recoverable.
    assert migration.backup_path is not None and migration.backup_path.exists()
    assert database_version(migration.backup_path) == 3

    with Store(path) as store:
        quotes = store.load_quotes(1)
        assert len(quotes) == len(V3_ROWS)
        assert {q.market.value for q in quotes} == {
            "moneyline", "spread", "total", "team_total"
        }
        assert {q.sport for q in quotes} == {Sport.BASEBALL}
        # Event keys are rebuilt from participant keys, and the doubleheader
        # ordinal survives — it identifies which of two games this is.
        assert {q.event_key for q in quotes} == {
            "MLB-PHI@MLB-MIA:2026-07-28", "MLB-CLE@MLB-CIN:2026-07-28#2"
        }
        assert {q.home_participant for q in quotes} == {"MLB-MIA", "MLB-CIN"}
        # And the identity that was rebuilt agrees with the names it came from.
        marlins = next(q for q in quotes if q.home_participant == "MLB-MIA")
        assert marlins.home_team == "Miami Marlins"
        assert marlins.away_participant == "MLB-PHI"
        # A finding that pointed at the old key now points at the new one.
        assert [row["event_key"] for row in store.query("SELECT event_key FROM finding")] == [
            "MLB-PHI@MLB-MIA:2026-07-28"
        ]


def test_a_migrated_database_accepts_new_rows_and_still_rejects_duplicates(
    tmp_path: Path,
) -> None:
    """The rebuilt table has to carry the UNIQUE constraint.  A migration that
    quietly dropped it would let one run store the same price twice, which is the
    failure the constraint exists to catch."""
    path = tmp_path / "old.sqlite3"
    write_v3_database(path)
    migrate_database(path)

    with Store(path) as store:
        run_id = store.start_run(datetime.now(UTC))
        quote = make_quote()
        assert store.save_quotes(run_id, [quote]) == 1
        with pytest.raises(sqlite3.IntegrityError):
            store.save_quotes(run_id, [quote])
        # A migrated database is indistinguishable from a fresh one, constraints
        # included, so the same DDL is what created both.
        fresh = tmp_path / "fresh.sqlite3"
        with Store(fresh):
            pass
        # SQLite quotes the table name after a RENAME, so the comparison is on
        # the constraint text rather than on that one cosmetic difference.
        assert _quote_ddl(path) == _quote_ddl(fresh)


def _quote_ddl(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='quote'"
        ).fetchone()
        return row[0].replace('CREATE TABLE "quote"', "CREATE TABLE quote")
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    write_v3_database(path)
    migrate_database(path)
    again = migrate_database(path)
    assert again.from_version == again.to_version == SCHEMA_VERSION
    assert again.quotes_migrated == 0


def test_a_row_whose_identity_cannot_be_recovered_aborts_the_whole_migration(
    tmp_path: Path,
) -> None:
    """Half a migration is worse than none.  A team name that does not resolve
    means the row's participant keys would have to be guessed, and a wrong guess
    silently merges two different fixtures — so nothing is changed at all."""
    path = tmp_path / "old.sqlite3"
    write_v3_database(
        path,
        rows=[
            *V3_ROWS,
            ("fanduel", "moneyline", "full_game", "home", None, None,
             "XXX@YYY:2026-07-28", "MLB Futures", "Division Winner"),
        ],
    )

    with pytest.raises(MigrationError) as caught:
        migrate_database(path, backup=False)
    assert "does not resolve to a participant" in str(caught.value)

    # Untouched: still v3, still the old columns, still every row.
    assert database_version(path) == 3
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM quote").fetchone()[0] == len(V3_ROWS) + 1
    columns = {row[1] for row in conn.execute("PRAGMA table_info(quote)")}
    assert "home_participant" not in columns
    assert conn.execute("SELECT market FROM quote WHERE id = 2").fetchone()[0] == "run_line"
    conn.close()


def test_a_disagreement_between_stored_key_and_stored_names_aborts(tmp_path: Path) -> None:
    """Both are recoverable individually; when they contradict each other, picking
    one is a guess, and the wrong guess joins two unrelated fixtures."""
    path = tmp_path / "old.sqlite3"
    write_v3_database(
        path,
        rows=[
            ("fanduel", "moneyline", "full_game", "home", None, None,
             "NYM@ATL:2026-07-28", "Miami Marlins", "Philadelphia Phillies"),
        ],
    )
    with pytest.raises(MigrationError, match="does not match the stored teams"):
        migrate_database(path, backup=False)
    assert database_version(path) == 3


def test_migrating_a_database_that_is_not_migratable_says_so(tmp_path: Path) -> None:
    path = tmp_path / "weird.sqlite3"
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(V3_SCHEMA)
        conn.execute("INSERT INTO schema_meta (version) VALUES (1)")
    conn.close()

    with pytest.raises(MigrationError, match="no upgrade path"):
        migrate_database(path, backup=False)
    assert database_version(path) == 1


def test_the_real_database_in_this_repo_is_handled_deliberately() -> None:
    """The repository ships ``data/collector.sqlite3`` holding real baseball rows
    under the old schema.  Whatever its version, opening it must either work or
    fail with an actionable message — never silently write into it.
    """
    path = Path("data/collector.sqlite3")
    if not path.exists():
        pytest.skip("no local database in this checkout")
    version = database_version(path)
    if version == SCHEMA_VERSION:
        with Store(path) as store:
            assert store.latest_run_id() is not None
        return
    with pytest.raises(IncompatibleDatabase) as caught:
        Store(path)
    assert "migrate" in str(caught.value)
    assert database_version(path) == version, "asking must not change the file"


# ── environment configuration ────────────────────────────────────────────────


def test_the_sport_neutral_env_names_are_read(monkeypatch, tmp_path: Path) -> None:
    """``MLB_DB_PATH`` was a lie the moment a second sport was collected, but a
    rename that silently ignored the old name would send a configured run to the
    default path instead."""
    import importlib

    monkeypatch.setenv("ODDS_DATA_DIR", str(tmp_path / "odds"))
    monkeypatch.delenv("MLB_DATA_DIR", raising=False)
    settings = importlib.reload(importlib.import_module("src.settings"))
    try:
        assert settings.DATA_DIR == tmp_path / "odds"
        assert settings.DB_PATH == tmp_path / "odds" / "collector.sqlite3"
        assert settings.DEPRECATED_ENV_USED == []
        assert settings.deprecation_notice() is None
    finally:
        importlib.reload(settings)


def test_the_old_mlb_names_still_work_and_are_reported(monkeypatch, tmp_path: Path) -> None:
    import importlib

    monkeypatch.delenv("ODDS_DATA_DIR", raising=False)
    monkeypatch.setenv("MLB_DATA_DIR", str(tmp_path / "legacy"))
    monkeypatch.setenv("MLB_HTTP_TIMEOUT", "7.5")
    settings = importlib.reload(importlib.import_module("src.settings"))
    try:
        assert settings.DATA_DIR == tmp_path / "legacy"
        assert settings.HTTP_TIMEOUT == 7.5
        assert ("MLB_DATA_DIR", "ODDS_DATA_DIR") in settings.DEPRECATED_ENV_USED
        notice = settings.deprecation_notice()
        assert notice is not None and "MLB_DATA_DIR" in notice and "ODDS_DATA_DIR" in notice
    finally:
        importlib.reload(settings)


def test_the_new_name_wins_over_the_deprecated_one(monkeypatch, tmp_path: Path) -> None:
    import importlib

    monkeypatch.setenv("ODDS_DB_PATH", str(tmp_path / "new.sqlite3"))
    monkeypatch.setenv("MLB_DB_PATH", str(tmp_path / "old.sqlite3"))
    settings = importlib.reload(importlib.import_module("src.settings"))
    try:
        assert settings.DB_PATH == tmp_path / "new.sqlite3"
    finally:
        importlib.reload(settings)


# ── the collector pipeline ───────────────────────────────────────────────────


class FakeSource:
    """A source that replays rows it was handed, declaring its leagues.

    Deliberately not an adapter: the pipeline's contract is what is under test —
    ordering, coverage, filtering, failure isolation — and a fake makes each of
    those a property of the collector rather than of a book's current slate.
    """

    def __init__(self, key: str, quotes, *, leagues=(), raws=None, fail=None) -> None:
        self._key = key
        self._quotes = list(quotes)
        self._leagues = tuple(leagues)
        self._raws = list(raws or [])
        self._fail = fail

    @property
    def source_key(self) -> str:
        return self._key

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    def fetch_raw(self):
        if self._fail is not None:
            raise self._fail
        return list(self._raws)

    def parse(self, raws):
        from src.sources.base import ParseOutcome

        return ParseOutcome(quotes=list(self._quotes))

    def close(self) -> None:
        pass


@pytest.fixture()
def collector():
    """Imported, not ``importorskip``-ed.

    ``src.collector`` is first-party and is the module this file exists to test.
    ``importorskip`` turns *any* ImportError inside it — a typo, a deleted
    helper, a circular import — into a skip, and pytest exits 0: the whole
    collect-and-replay block reported ``31 passed, 15 skipped`` with the
    pipeline unimportable.  A third-party optional dependency is what that
    helper is for; this is not one.
    """
    import src.collector

    return src.collector


def test_a_run_persists_every_sport_it_collected(tmp_path: Path, collector) -> None:
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", multi_sport_quotes("bookA"), leagues=("MLB", "NHL", "EPL", "ATP"),
                   raws=[make_raw("{}", source="bookA")]),
        FakeSource("bookB", multi_sport_quotes("bookB"), leagues=("MLB", "NHL", "EPL", "ATP"),
                   raws=[make_raw("{}", source="bookB")]),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)

        assert result.run_id is not None
        stored = store.load_quotes(result.run_id)
        assert len(stored) == len(result.quotes) == 10
        assert store.sports_for_run(result.run_id) == [
            "baseball", "hockey", "soccer", "tennis"
        ]
        assert {q.dedup_key for q in stored} == {q.dedup_key for q in result.quotes}


def test_coverage_is_reported_against_the_configured_leagues(tmp_path: Path, collector) -> None:
    """A league that was asked for and produced nothing is a gap.  Without the
    configured list it would be indistinguishable from one nobody wanted."""
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", [make_quote(source="bookA")], leagues=("MLB", "NHL")),
        FakeSource("bookB", [make_quote(source="bookB")], leagues=("MLB", "WNBA")),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)

        gaps = {(entry.source_key, entry.league)
                for entry in result.league_coverage if entry.is_gap}
        assert gaps == {("bookA", "NHL"), ("bookB", "WNBA")}
        stored = {(row["source_key"], row["league"]): row["quote_count"]
                  for row in store.league_coverage(result.run_id)}
        assert stored == {
            ("bookA", "MLB"): 1, ("bookA", "NHL"): 0,
            ("bookB", "MLB"): 1, ("bookB", "WNBA"): 0,
        }
        codes = [f.code for f in result.report.warnings]
        assert "league_returned_nothing" in codes


def test_a_source_that_declares_no_leagues_is_flagged(tmp_path: Path, collector) -> None:
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", [make_quote(source="bookA")]),
        FakeSource("bookB", [make_quote(source="bookB")], leagues=("MLB",)),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)
    codes = [f.code for f in result.report.warnings]
    assert "source_declares_no_leagues" in codes


def test_a_failing_source_does_not_abort_the_run(tmp_path: Path, collector) -> None:
    from src.sources.guards import BlockedError

    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", multi_sport_quotes("bookA"), leagues=("MLB",),
                   raws=[make_raw("{}", source="bookA")]),
        FakeSource("bookB", multi_sport_quotes("bookB"), leagues=("MLB",),
                   raws=[make_raw("{}", source="bookB")]),
        FakeSource("broken", [], leagues=("MLB",), fail=BlockedError("broken:odds: HTTP 403")),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)

    broken = next(h for h in result.health if h.source_key == "broken")
    assert broken.ok is False
    assert broken.error_kind == "blocked"
    assert "403" in broken.error_message
    # The healthy books still collected, and the run does not report itself as
    # short of sources — which is what "the failure did not abort it" means.
    assert len(result.quotes) == 10
    assert [h.source_key for h in result.health if h.quote_count] == ["bookA", "bookB"]
    assert "insufficient_sources" not in {f.code for f in result.report.errors}


def test_raw_bytes_are_written_before_anything_is_parsed(tmp_path: Path, collector) -> None:
    """The reproduction guarantee: a parse that crashes must still leave the bytes
    behind, or the failure cannot be investigated offline."""
    raw_store = RawStore(tmp_path / "raw")

    class Exploding(FakeSource):
        def parse(self, raws):
            raise RuntimeError("boom")

    sources = [
        Exploding("bookA", [], leagues=("MLB",), raws=[make_raw('{"a": 1}', source="bookA")]),
        FakeSource("bookB", [make_quote(source="bookB")], leagues=("MLB",),
                   raws=[make_raw('{"b": 2}', source="bookB")]),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)
        stored = store.raw_paths(result.run_id)
        assert {source for source, _ in stored} == {"bookA", "bookB"}
        assert all(path.exists() for _, path in stored)

    failed = next(h for h in result.health if h.source_key == "bookA")
    assert failed.ok is False
    assert failed.error_kind == "parse_error"
    assert "boom" in failed.error_message


def test_a_single_source_run_fails_the_two_source_requirement(tmp_path: Path, collector) -> None:
    raw_store = RawStore(tmp_path / "raw")
    sources = [FakeSource("bookA", multi_sport_quotes("bookA"), leagues=("MLB",))]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)
    assert not result.ok
    assert "insufficient_sources" in {f.code for f in result.report.errors}


def test_per_sport_book_counts_are_reported_even_when_the_run_is_ok(
    tmp_path: Path, collector
) -> None:
    """Two books overall does not mean two books per sport.  A run can be ``ok``
    and still have a sport nothing can be compared in, and that has to be said."""
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", [make_quote(source="bookA"), hockey_quote(source="bookA")],
                   leagues=("MLB", "NHL")),
        FakeSource("bookB", [make_quote(source="bookB")], leagues=("MLB",)),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)

    assert "insufficient_sources" not in {f.code for f in result.report.errors}
    coverage = {entry.sport: entry for entry in result.coverage}
    assert coverage["baseball"].meets_two_book_bar is True
    assert coverage["baseball"].cross_book_events == 1
    assert coverage["baseball"].is_comparable is True
    assert coverage["hockey"].meets_two_book_bar is False
    assert coverage["hockey"].is_comparable is False
    assert result.usable_sports == ["baseball"]
    assert result.unusable_sports == ["hockey"]
    assert "sport_below_two_books" in {f.code for f in result.report.warnings}
    # ...and the run records its own verdict, so it survives the process.
    with Store(tmp_path / "db.sqlite3") as store:
        note = store.run_summaries()[0]["note"]
    assert "baseball" in note and "hockey" in note


def test_two_books_without_a_shared_fixture_is_reported_separately(
    tmp_path: Path, collector
) -> None:
    raw_store = RawStore(tmp_path / "raw")
    other_hockey = hockey_quote(
        source="bookB",
        event_key="NHL-LA@NHL-COL:2026-09-30",
        home_participant="NHL-COL", away_participant="NHL-LA",
        home_team="Colorado Avalanche", away_team="Los Angeles Kings",
        commence_time=datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
    )
    sources = [
        FakeSource("bookA", [make_quote(source="bookA"), hockey_quote(source="bookA")],
                   leagues=("MLB", "NHL")),
        FakeSource("bookB", [make_quote(source="bookB"), other_hockey], leagues=("MLB", "NHL")),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)

    hockey = next(entry for entry in result.coverage if entry.sport == "hockey")
    assert hockey.meets_two_book_bar is True
    assert hockey.cross_book_events == 0
    assert hockey.is_comparable is False
    assert "sport_without_cross_book_fixtures" in {f.code for f in result.report.warnings}


def test_a_sport_priced_only_by_mirrors_is_not_called_comparable(tmp_path: Path) -> None:
    """The collection-time verdict must apply the exclusion ``arb`` applies.

    Run 27 — ten republished feeds, 2,871 rows, every source view-only — stored
    the note "comparable sports (2+ books on one fixture): baseball" while
    ``runs`` printed "0 cross-book" two lines above it and ``arb`` found 0
    cross-book markets.  ``sport_coverage`` counted every source toward the
    two-book bar; ``store.cross_book_event_counts`` had excluded view-only
    feeds all along, with a docstring explaining why a mirror is not a
    counterparty.  Same slate, two verdicts, and the stored one was the wrong
    one.
    """
    from src.collector import sport_coverage

    quotes = [
        make_quote(source="an_fanduel", source_market_id="m1"),
        make_quote(source="an_betrivers", source_market_id="m2"),
        make_quote(source="an_parx", source_market_id="m3"),
    ]
    view_only = frozenset({"an_fanduel", "an_betrivers", "an_parx"})

    entries = {e.sport: e for e in sport_coverage(quotes, [], view_only=view_only)}
    baseball = entries["baseball"]
    assert baseball.sources == ("an_betrivers", "an_fanduel", "an_parx")
    assert baseball.counterparty_sources == ()
    assert baseball.meets_two_book_bar is False
    assert baseball.cross_book_events == 0
    assert baseball.is_comparable is False

    # And one real counterparty beside the mirrors still is not comparable —
    # a bet needs somebody on the other side.
    with_one = quotes + [make_quote(source="fanduel", source_market_id="m4")]
    entries = {e.sport: e for e in sport_coverage(with_one, [], view_only=view_only)}
    assert entries["baseball"].counterparty_sources == ("fanduel",)
    assert entries["baseball"].is_comparable is False

    # Two counterparties sharing the fixture: comparable, mirrors or not.
    with_two = with_one + [make_quote(source="draftkings", source_market_id="m5")]
    entries = {e.sport: e for e in sport_coverage(with_two, [], view_only=view_only)}
    assert entries["baseball"].counterparty_sources == ("draftkings", "fanduel")
    assert entries["baseball"].is_comparable is True


def test_collect_once_applies_the_runs_view_only_set_to_the_verdict(
    tmp_path: Path, collector
) -> None:
    """The caller's half of the fix above, which a unit test cannot hold.

    ``sport_coverage`` taking ``view_only=`` fixes nothing unless
    ``collect_once`` passes the run's actual set — the argument defaults to
    empty, so dropping it at the call site silently restores the run-27
    mislabel while every direct-call test stays green.
    """
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("an_fanduel", [make_quote(source="an_fanduel")], leagues=("MLB",)),
        FakeSource(
            "an_betrivers", [make_quote(source="an_betrivers")], leagues=("MLB",)
        ),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)

    baseball = next(e for e in result.coverage if e.sport == "baseball")
    assert baseball.sources == ("an_betrivers", "an_fanduel")
    assert baseball.counterparty_sources == (), (
        "both feeds are republished mirrors; the run's view-only set must reach "
        "the verdict"
    )
    assert baseball.is_comparable is False
    assert "sport_below_two_books" in {f.code for f in result.report.warnings}


def test_collect_once_measures_mirrors_under_the_runs_set(tmp_path: Path, collector) -> None:
    """The alerting site's capture pin — reverting it left every suite green.

    ``collect_batch_once`` collects the detected state and ODDS_STATE's in one
    process, so the measurement set must come from the pass's own jurisdiction
    argument, never the ambient constant.
    """
    from src.sources import registry

    captured: list[object] = []
    real = collector.find_mirrors

    def recording(quotes, *, view_only=None):
        captured.append(view_only)
        return real(quotes, view_only=view_only)

    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", [make_quote(source="bookA")], leagues=("MLB",)),
        FakeSource("bookB", [make_quote(source="bookB")], leagues=("MLB",)),
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(collector, "find_mirrors", recording)
        with Store(tmp_path / "db.sqlite3") as store:
            collector.collect_once(
                sources, raw_store=raw_store, store=store, jurisdiction="PA"
            )
    assert captured == [registry.view_only_for_run("PA")], (
        "the mirror measurement must run under the pass's own jurisdiction"
    )


def test_filtering_a_run_by_sport_keeps_only_that_sport(tmp_path: Path, collector) -> None:
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", multi_sport_quotes("bookA"), leagues=("MLB", "NHL", "EPL", "ATP")),
        FakeSource("bookB", multi_sport_quotes("bookB"), leagues=("MLB", "NHL", "EPL", "ATP")),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(
            sources, raw_store=raw_store, store=store, sports=["hockey"]
        )
        assert {q.sport for q in result.quotes} == {Sport.HOCKEY}
        assert store.sports_for_run(result.run_id) == ["hockey"]
        # Excluded rows are counted, never silently dropped.
        assert result.excluded_by_filter == 6
        assert "excluded by filter" in store.run_summaries()[0]["note"]
        # And the out-of-scope leagues are not reported as gaps: they were not
        # asked about on this run.
        assert {entry.league for entry in result.league_coverage} == {"NHL"}


def test_filtering_by_league_narrows_within_a_sport(tmp_path: Path, collector) -> None:
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", multi_sport_quotes("bookA"), leagues=("MLB", "NHL", "EPL", "ATP")),
        FakeSource("bookB", multi_sport_quotes("bookB"), leagues=("MLB", "NHL", "EPL", "ATP")),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(
            sources, raw_store=raw_store, store=store, leagues=["EPL"]
        )
    assert {q.league for q in result.quotes} == {"EPL"}
    assert {q.sport for q in result.quotes} == {Sport.SOCCER}


def test_reconciliation_runs_across_all_sources_before_validation(
    tmp_path: Path, collector
) -> None:
    """One book listing only the late game of a doubleheader numbers it as game 1.
    Left alone, its prices join onto the other book's game 1 — two different games
    reported as one, which is the shape of a phantom arbitrage.
    """
    early = datetime(2026, 7, 28, 17, 40, tzinfo=UTC)
    late = datetime(2026, 7, 28, 23, 10, tzinfo=UTC)
    base = "MLB-CLE@MLB-CIN:2026-07-28"
    pair = dict(
        home_participant="MLB-CIN", away_participant="MLB-CLE",
        home_team="Cincinnati Reds", away_team="Cleveland Guardians",
    )
    both_games = [
        make_quote(source="bookA", event_key=base, commence_time=early,
                   source_event_id="a1", source_market_id="a1ml", **pair),
        make_quote(source="bookA", event_key=f"{base}#2", commence_time=late,
                   source_event_id="a2", source_market_id="a2ml", **pair),
    ]
    # bookB saw only the late game, so its own pass numbered it as game 1.
    late_only = [
        make_quote(source="bookB", event_key=base, commence_time=late,
                   source_event_id="b2", source_market_id="b2ml", **pair),
    ]
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(
            [FakeSource("bookA", both_games, leagues=("MLB",)),
             FakeSource("bookB", late_only, leagues=("MLB",))],
            raw_store=raw_store, store=store,
        )
        stored = store.load_quotes(result.run_id)

    corrected = next(q for q in stored if q.source == "bookB")
    assert corrected.event_key == f"{base}#2"
    assert "event_key_reconciled" in {f.code for f in result.report.warnings}
    # The two games stay two games, in both books' rows.
    assert {q.event_key for q in stored} == {base, f"{base}#2"}
    # Reconciliation happened before validation, so what was validated is what
    # was stored: the report's own counts agree with the table.
    assert result.report.quote_count == len(stored)


def test_replay_reproduces_a_stored_run(tmp_path: Path, collector, monkeypatch) -> None:
    """Replay re-parses the stored bytes.  With a fake adapter registered under a
    key the collector knows, this exercises the comparison itself."""
    raw_store = RawStore(tmp_path / "raw")
    rows = multi_sport_quotes("fanduel")
    other = multi_sport_quotes("pinnacle")

    monkeypatch.setitem(
        collector.SOURCE_FACTORIES, "fanduel",
        lambda **kwargs: FakeSource("fanduel", rows, leagues=("MLB",)),
    )
    monkeypatch.setitem(
        collector.SOURCE_FACTORIES, "pinnacle",
        lambda **kwargs: FakeSource("pinnacle", other, leagues=("MLB",)),
    )
    sources = [
        FakeSource("fanduel", rows, leagues=("MLB",), raws=[make_raw("{}", source="fanduel")]),
        FakeSource("pinnacle", other, leagues=("MLB",), raws=[make_raw("{}", source="pinnacle")]),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)

        class ReplayOnlySource(FakeSource):
            def fetch_raw(self, *, tier=None):
                raise AssertionError("replay must never call fetch_raw")

        # Patched on ``replay_factory``, not ``SOURCE_FACTORIES``: replay resolves
        # a state-scoped run's adapters from the run's own jurisdiction, because the
        # base descriptors carry whichever state's book ids the base entry names.
        # That indirection is the documented seam for injecting a replay adapter.
        replay_fakes = {
            "fanduel": lambda: ReplayOnlySource("fanduel", rows, leagues=("MLB",)),
            "pinnacle": lambda: ReplayOnlySource("pinnacle", other, leagues=("MLB",)),
        }
        monkeypatch.setattr(
            collector, "replay_factory", lambda state, key: replay_fakes[key]
        )
        ok, problems = collector.replay_run(
            result.run_id, store=store, raw_store=raw_store
        )
        assert ok, problems

        # Scoped replay compares only that sport, on both sides.
        ok, problems = collector.replay_run(
            result.run_id, store=store, raw_store=raw_store, sports=["hockey"]
        )
        assert ok, problems


def test_replay_notices_a_parser_that_changed_its_mind(tmp_path: Path, collector, monkeypatch) -> None:
    raw_store = RawStore(tmp_path / "raw")
    rows = [make_quote(source="fanduel", source_market_id="m")]
    drifted = [make_quote(source="fanduel", source_market_id="m", decimal_odds=2.5)]

    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(
            [FakeSource("fanduel", rows, leagues=("MLB",),
                        raws=[make_raw("{}", source="fanduel")]),
             FakeSource("pinnacle", [make_quote(source="pinnacle")], leagues=("MLB",),
                        raws=[make_raw("{}", source="pinnacle")])],
            raw_store=raw_store, store=store,
        )
        # See the note in ``test_replay_reproduces_a_stored_run``.
        fakes = {
            "fanduel": lambda: FakeSource("fanduel", drifted, leagues=("MLB",)),
            "pinnacle": lambda: FakeSource(
                "pinnacle", [make_quote(source="pinnacle")], leagues=("MLB",)
            ),
        }
        monkeypatch.setattr(
            collector, "replay_factory", lambda state, key: fakes[key]
        )
        ok, problems = collector.replay_run(result.run_id, store=store, raw_store=raw_store)
    assert not ok
    assert any("odds changed on replay" in problem for problem in problems)


def test_unchanged_payload_is_detected_across_runs(tmp_path: Path, collector) -> None:
    """A feed that returns byte-identical bytes must say so, so an operator can
    tell a quiet market from a stuck or cached one."""
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", multi_sport_quotes("bookA"), leagues=("MLB",),
                   raws=[make_raw('{"a": 1}', source="bookA")]),
        FakeSource("bookB", multi_sport_quotes("bookB"), leagues=("MLB",),
                   raws=[make_raw('{"b": 1}', source="bookB")]),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        first = collector.collect_once(sources, raw_store=raw_store, store=store)
        assert all(h.unchanged_payloads == 0 for h in first.health)

        second = collector.collect_once(sources, raw_store=raw_store, store=store)
        for health in second.health:
            assert health.unchanged_payloads == health.request_count

        flagged = store.query(
            "SELECT COUNT(*) n FROM raw_response WHERE run_id = ? AND unchanged = 1",
            (second.run_id,),
        )
        assert flagged[0]["n"] == 2


def test_a_duplicate_row_costs_its_own_source_and_no_other(
    tmp_path: Path, collector
) -> None:
    """The storage constraint aborts an insert.  Whose insert is the question.

    One ``executemany`` over the whole run means one colliding row from one
    adapter discards *every* source's prices — thirty-four thousand good rows
    lost to one duplicate.  With three books that was a bad day; with ten it is
    the expected consequence of adding the tenth.  So each source is inserted in
    its own transaction: the offender loses its rows, everybody else keeps
    theirs, and the run still reports the fault loudly and by name.
    """
    duplicate = make_quote(source="bookA", source_market_id="m")
    raw_store = RawStore(tmp_path / "raw")
    sources = [
        FakeSource("bookA", [duplicate, duplicate], leagues=("MLB",)),
        FakeSource("bookB", [make_quote(source="bookB")], leagues=("MLB",)),
    ]
    with Store(tmp_path / "db.sqlite3") as store:
        result = collector.collect_once(sources, raw_store=raw_store, store=store)
        stored = store.load_quotes(result.run_id)
    assert {quote.source for quote in stored} == {"bookB"}
    failures = [f for f in result.report.errors if f.code == "quotes_not_persisted"]
    assert [f.source for f in failures] == ["bookA"]


def test_response_headers_are_stored_for_freshness_auditing(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    raw = RawResponse(
        source="testbook", endpoint="odds", url="https://example.invalid/odds",
        status_code=200, body="{}", fetched_at=datetime(2026, 7, 28, 7, 0, tzinfo=UTC),
        headers={"age": "42", "x-cache": "HIT", "set-cookie": "secret=1"},
    )
    loaded = store.read(store.write(raw))
    assert loaded.cache_hints == {"age": "42", "x-cache": "HIT"}


def test_credential_like_headers_are_never_stored() -> None:
    cleaned = RawResponse.clean_headers(
        {"Set-Cookie": "s=1", "Authorization": "Bearer x", "Age": "3", "Content-Type": "application/json"}
    )
    assert cleaned == {"age": "3", "content-type": "application/json"}


def test_version_1_envelopes_remain_replayable(tmp_path: Path) -> None:
    """Old captures predate header storage; a parser upgrade must not orphan
    them.

    The sha256 is NOT part of what v1 lacks: every stored envelope of every
    version records one (all 5,100 measured 2026-08-10, including all four
    v1 files), and ``from_envelope`` refuses an envelope without it — so
    this synthetic v1 carries the hash the real ones do, and only
    headers/capture_id are absent.
    """
    import hashlib

    body = '{"ok": true}'
    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "envelope_version": 1,
        "source": "testbook",
        "endpoint": "odds",
        "url": "https://example.invalid/odds",
        "status_code": 200,
        "content_type": "application/json",
        "fetched_at": "2026-07-28T07:00:00+00:00",
        "request_params": {},
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body": body,
    }))
    loaded = RawStore(tmp_path).read(path)
    assert loaded.json() == {"ok": True}
    assert loaded.headers == {}


def test_replay_resolves_adapters_from_the_runs_state_not_the_process(monkeypatch) -> None:
    """``replay``'s verdict must not depend on an environment variable.

    ``settings.STATE`` used to short-circuit to ``SOURCE_FACTORIES`` whenever the
    run's jurisdiction matched this process's — and the *matching* case was the
    broken one, because those are the base descriptors, whose config is whichever
    state the base entry happens to name. ``an_parx`` is ``book_id 1929`` (New
    Jersey) there and ``74`` in Pennsylvania.

    So replaying a PA run on a PA-configured box re-parsed PA captures under New
    Jersey book ids, while the same run replayed from an Illinois box resolved the
    PA route and got it right. Same bytes, same database, PASS or FAIL depending on
    ``ODDS_STATE``.
    """
    from src import collector, settings
    from src.sources import registry

    # The two configs that used to be confused, stated so the test cannot pass by
    # them being equal.
    assert registry.BY_BASE_KEY["an_parx"].config["book_id"] == 1929      # New Jersey
    assert registry.replay_descriptor_for_state("PA", "an_parx").config["book_id"] == 74

    base = collector.SOURCE_FACTORIES["an_parx"]
    for ambient in ("PA", "IL", "NJ", "DC"):
        monkeypatch.setattr(settings, "STATE", ambient)
        chosen = collector.replay_factory("PA", "an_parx")
        assert chosen is not base, (
            f"with ODDS_STATE={ambient} replay fell back to the base descriptor "
            "(New Jersey's book id) for a Pennsylvania run"
        )
        assert chosen.keywords["book_id"] == 74, (
            f"with ODDS_STATE={ambient} a PA run resolved book id "
            f"{chosen.keywords['book_id']} instead of PA's 74"
        )


def test_replay_uses_the_base_factory_only_for_stateless_runs(monkeypatch) -> None:
    """A run with no jurisdiction, or a GLOBAL one, has no state route to resolve."""
    from src import collector

    for stateless in ("", "GLOBAL"):
        assert collector.replay_factory(stateless, "pinnacle") is (
            collector.SOURCE_FACTORIES["pinnacle"]
        ), stateless
