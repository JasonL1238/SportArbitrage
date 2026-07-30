"""SQLite persistence for collected quotes and run metadata.

SQLite because the requirement is a pipeline that runs locally and unattended:
it is in the standard library, needs no server to be up, survives restarts, and
is queryable with tools already on the machine.

The ``quote`` table is UNIQUE on ``(run_id, dedup_key)``, so a parser that emits
the same priced selection twice in one run fails at the storage boundary as well
as in validation.  The key is a single text column rather than a constraint over
the individual columns because SQLite treats NULLs as distinct in a UNIQUE
constraint — a moneyline has no line and no side, so a column-wise constraint
would never have fired for exactly the rows most at risk of duplication.

Rows carry both a **sport** and a **league**, and both are indexed, because every
consumer now asks per-sport questions: the CLI filters by them, and the dashboard
reports coverage by them.  ``home_participant``/``away_participant`` are stored
alongside the display names because they are what everything joins on; keeping
them on the row means a re-analysis of stored history never has to re-resolve a
name, which it could only do by guessing at the spelling conventions of the book
that supplied it.

**Schema compatibility is checked before anything is written.**  A database
written by an older build is not opened for use: its market names, event keys and
dedup keys mean different things, so appending to it would produce a table whose
rows cannot be compared with each other.  :func:`migrate_database` upgrades one
deliberately, keeping a backup; :class:`Store` refuses and says so.
"""
from __future__ import annotations

import json
import shutil
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from src.leagues import league as get_league
from src.participants import canonical_participant
from src.raw_store import RawResponse
from src.schema import Market, Period, Quote, QuoteStatus, Selection, Side, Sport

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from src.sources.base import Rejection, SourceHealth
    from src.validation import Finding, ValidationReport

# Storage deliberately imports neither the validator nor the source adapters at
# runtime: it only ever *reads attributes off* what it is handed.  Importing them
# would make persistence unavailable whenever a single adapter is broken — and
# ``src.sources`` pulls in all three books' HTTP clients on import — which is
# exactly backwards for the layer whose job is to keep hold of what was already
# collected.

#: Bumped whenever a stored row changes meaning.  Version 4 is the multi-sport
#: schema: ``sport``/``league`` are meaningful rather than constant, participant
#: keys are stored, market names are sport-neutral (``spread``/``total``/
#: ``team_total``), event keys are built from participant keys, and ``dedup_key``
#: includes ``is_alternate``.
log = logging.getLogger("store")


SCHEMA_VERSION = 4

#: The oldest version :func:`migrate_database` knows how to upgrade from.
OLDEST_MIGRATABLE_VERSION = 3

#: Rows bound per ``executemany``.  A ``Quote`` measures about 3.4 KB resident,
#: so a 300,000-row slate is roughly a gigabyte held at once by validation, the
#: arbitrage pass and the store together.  Chunking the *insert* does not fix
#: that on its own, but it keeps the statement binding bounded and it is the part
#: of the problem this layer owns.
QUOTE_CHUNK_SIZE = 5_000

#: Market renames between v3 and v4.  These are pure renames of the same
#: contract — a baseball "run line" *is* a spread — so applying them to stored
#: rows loses nothing.
V3_MARKET_RENAMES = {
    "run_line": Market.SPREAD.value,
    "total_runs": Market.TOTAL.value,
    "team_total_runs": Market.TEAM_TOTAL.value,
}


class IncompatibleDatabase(RuntimeError):
    """The database on disk was written by a different schema version."""

    def __init__(self, path: Path, found: int | None, expected: int = SCHEMA_VERSION) -> None:
        self.path = path
        self.found = found
        self.expected = expected
        described = "no version marker" if found is None else f"schema version {found}"
        migratable = found is not None and OLDEST_MIGRATABLE_VERSION <= found < expected
        remedy = (
            f"    python -m src.collector migrate --db {path}\n"
            "        upgrades it in place, writing a .bak copy of the current file first\n"
            if migratable
            else f"    (no migration exists from {described} to {expected})\n"
        )
        super().__init__(
            f"{path} has {described}; this build writes version {expected}. "
            "Nothing has been read or written.\n"
            "The two are not interchangeable: v3 rows are baseball-only, name markets "
            "'run_line'/'total_runs'/'team_total_runs', carry no participant keys, and use "
            "event keys built from bare abbreviations (PHI@MIA) rather than participant keys "
            "(MLB-PHI@MLB-MIA). Mixing them in one table would silently produce rows that "
            "cannot be compared with each other.\n"
            "Choose one:\n"
            f"{remedy}"
            f"    ODDS_DB_PATH=<new path> python -m src.collector collect\n"
            "        leaves the old file untouched and starts a fresh database\n"
        )


_SCHEMA_HEAD = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    ok            INTEGER,
    quote_count   INTEGER NOT NULL DEFAULT 0,
    event_count   INTEGER NOT NULL DEFAULT 0,
    error_count   INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    -- The scope the run was collected under, structurally.  It was recoverable
    -- only as prose inside ``note``, which no code could safely read back — so
    -- ``replay`` compared a scope-collected run's stored rows against an
    -- unscoped re-parse and reported the filter's own drops as corruption:
    -- "row count differs: stored 2476, replayed 5429 / replay invented row"
    -- on a run whose note says "2953 rows excluded by filter", and the
    -- dashboard masthead read "replay FAIL".  The README's own quick start
    -- (collect --sport hockey, then replay) walks into it.
    scope_sports  TEXT NOT NULL DEFAULT '',
    scope_leagues TEXT NOT NULL DEFAULT '',
    excluded_count INTEGER NOT NULL DEFAULT 0,
    -- Schema version this run was originally collected under, when it predates
    -- a migration.  NULL for runs collected natively.  Replay differences on a
    -- migrated run can be the parser having legitimately changed since — the
    -- surface has to be able to say that instead of "corruption".
    migrated_from INTEGER,
    -- The counterparty groups the collector measured on the FULL slate, as
    -- JSON {league: [[source, source], ...]}.  A scoped run stores only the
    -- kept rows, so re-measuring the mirror gate from the store alone reopened
    -- it: two licences of one operator, established as one counterparty on 22
    -- shared MLB selections the filter then dropped, were re-judged
    -- independent and ``arb --run N`` published +3.00 with both legs at one
    -- book — the exact defect the gate exists to prevent.
    counterparty_groups TEXT
);

CREATE TABLE IF NOT EXISTS raw_response (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES collection_run(id),
    source       TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    url          TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    content_type TEXT,
    fetched_at   TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    byte_size    INTEGER NOT NULL,
    path         TEXT NOT NULL,
    raw_ref      TEXT NOT NULL,
    headers      TEXT,
    -- 1 when this fetch returned bytes identical to the previous run's fetch of
    -- the same endpoint, so an unchanging feed is visible rather than implied.
    unchanged    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_raw_run ON raw_response(run_id, source);
"""

#: The ``quote`` table on its own, because the migration creates a second table
#: with exactly this shape and then renames it into place.  Keeping one source of
#: truth for the DDL is what guarantees a migrated database and a fresh one have
#: the same constraints — including the UNIQUE that a migration is most tempted to
#: quietly leave off.
QUOTE_DDL = """
CREATE TABLE IF NOT EXISTS quote (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES collection_run(id),
    source              TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    raw_ref             TEXT NOT NULL,
    -- The response that supplied the event identity, when the price came from a
    -- different call (Pinnacle needs two).  Nullable: most rows owe both to one
    -- response, and a compound raw_ref would give that column an undeclared
    -- grammar every reader would have to know to split.
    identity_raw_ref    TEXT,
    sport               TEXT NOT NULL,
    league              TEXT NOT NULL,
    event_key           TEXT NOT NULL,
    source_event_id     TEXT NOT NULL,
    home_participant    TEXT NOT NULL,
    away_participant    TEXT NOT NULL,
    home_team           TEXT NOT NULL,
    away_team           TEXT NOT NULL,
    commence_time       TEXT NOT NULL,
    market              TEXT NOT NULL,
    period              TEXT NOT NULL,
    selection           TEXT NOT NULL,
    side                TEXT,
    line                REAL,
    is_alternate        INTEGER NOT NULL DEFAULT 0,
    decimal_odds        REAL NOT NULL,
    american_odds       INTEGER NOT NULL,
    implied_probability REAL NOT NULL,
    source_market_id    TEXT,
    source_selection_id TEXT,
    limit_amount        REAL,
    status              TEXT NOT NULL,
    last_change_at      TEXT,
    dedup_key           TEXT NOT NULL,
    -- Uniqueness is enforced on the explicit dedup key rather than on the
    -- individual columns: SQLite treats NULLs as distinct in a UNIQUE
    -- constraint, so a moneyline (side and line both NULL) would never have
    -- collided and duplicate moneylines would have stored silently.  The key
    -- itself is (source, event_key, market, period, side, selection, line,
    -- is_alternate) — is_alternate is in it because a book really does price the
    -- same number twice, once on its main market and once as an alternate line.
    UNIQUE (run_id, dedup_key)
);
"""

_SCHEMA_TAIL = """
CREATE INDEX IF NOT EXISTS idx_quote_event ON quote(event_key, market, period);
CREATE INDEX IF NOT EXISTS idx_quote_run ON quote(run_id, source);
-- Every consumer asks per-sport and per-league questions now, so they are
-- indexed rather than filtered by scanning the whole run.
CREATE INDEX IF NOT EXISTS idx_quote_sport ON quote(run_id, sport, league, source);
CREATE INDEX IF NOT EXISTS idx_quote_participants ON quote(home_participant, away_participant);

CREATE TABLE IF NOT EXISTS source_health (
    run_id          INTEGER NOT NULL REFERENCES collection_run(id),
    source_key      TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    checked_at      TEXT NOT NULL,
    request_count   INTEGER NOT NULL DEFAULT 0,
    raw_bytes       INTEGER NOT NULL DEFAULT 0,
    latency_ms      REAL,
    quote_count     INTEGER NOT NULL DEFAULT 0,
    event_count     INTEGER NOT NULL DEFAULT 0,
    rejection_count INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    repaired_count  INTEGER NOT NULL DEFAULT 0,
    unchanged_payloads INTEGER NOT NULL DEFAULT 0,
    scopes_requested   INTEGER NOT NULL DEFAULT 0,
    scopes_refused     TEXT,
    error_kind      TEXT,
    error_message   TEXT,
    PRIMARY KEY (run_id, source_key)
);

-- Which leagues each source was *configured* to collect on a run.  Stored
-- because coverage has to be reported against what was asked for: a league that
-- returned nothing is a gap, and without this row it is indistinguishable from a
-- league nobody requested.
CREATE TABLE IF NOT EXISTS source_league (
    run_id      INTEGER NOT NULL REFERENCES collection_run(id),
    source_key  TEXT NOT NULL,
    league      TEXT NOT NULL,
    sport       TEXT NOT NULL,
    configured  INTEGER NOT NULL DEFAULT 1,
    quote_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, source_key, league)
);

CREATE TABLE IF NOT EXISTS rejection (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL REFERENCES collection_run(id),
    source   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    detail   TEXT NOT NULL,
    context  TEXT
);

CREATE TABLE IF NOT EXISTS skipped (
    run_id   INTEGER NOT NULL REFERENCES collection_run(id),
    source   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    count    INTEGER NOT NULL,
    PRIMARY KEY (run_id, source, reason)
);

-- Rows that were **published** after a field was reconstructed. Kept apart from
-- ``skipped`` because they are in the output: filing them together said they
-- were not collected, and then dropping them from the payload entirely said
-- nothing at all.
CREATE TABLE IF NOT EXISTS repaired (
    run_id   INTEGER NOT NULL REFERENCES collection_run(id),
    source   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    count    INTEGER NOT NULL,
    PRIMARY KEY (run_id, source, reason)
);

CREATE TABLE IF NOT EXISTS finding (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES collection_run(id),
    severity  TEXT NOT NULL,
    code      TEXT NOT NULL,
    message   TEXT NOT NULL,
    source    TEXT,
    event_key TEXT
);
-- The v3->v4 migration rewrites finding.event_key one distinct key at a time,
-- and without this index each of those UPDATEs scans the whole table: quadratic
-- in the number of events, on the one operation an operator runs against their
-- entire history.  Also what makes "show me every finding about this fixture" a
-- lookup rather than a scan.
CREATE INDEX IF NOT EXISTS idx_finding_event ON finding(event_key);
CREATE INDEX IF NOT EXISTS idx_finding_run ON finding(run_id, severity);
"""

_SCHEMA = _SCHEMA_HEAD + QUOTE_DDL + _SCHEMA_TAIL


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _values(items: Sequence[str] | str | None) -> tuple[str, ...]:
    """Normalize a scope argument to a tuple of plain strings.

    Accepts a single value or a sequence, and enum members as well as strings, so
    callers can pass ``Sport.HOCKEY``, ``"hockey"`` or ``["hockey", "soccer"]``
    without the query layer caring which.
    """
    if items is None:
        return ()
    if isinstance(items, str):
        items = [items]
    return tuple(str(getattr(item, "value", item)) for item in items)


def _scope(
    sports: Sequence[str] | str | None,
    leagues: Sequence[str] | str | None,
    *,
    prefix: str = "",
) -> tuple[str, list[Any]]:
    """SQL fragment and parameters restricting a quote query by sport/league.

    The fragment is a series of ``AND`` clauses, so it appends to an existing
    ``WHERE``.  *prefix* qualifies the columns (``"q."``) for use in a subquery.
    """
    clauses: list[str] = []
    params: list[Any] = []
    sport_values = _values(sports)
    league_values = _values(leagues)
    if sport_values:
        clauses.append(f"{prefix}sport IN ({','.join('?' * len(sport_values))})")
        params.extend(sport_values)
    if league_values:
        clauses.append(f"{prefix}league IN ({','.join('?' * len(league_values))})")
        params.extend(league_values)
    return ("".join(f" AND {clause}" for clause in clauses), params)


# ── schema compatibility ─────────────────────────────────────────────────────


def database_version(path: str | Path) -> int | None:
    """The schema version of an existing database file.

    ``None`` means the file does not exist, is empty, or predates versioning.
    Opens read-only and creates nothing, so asking is always safe.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    for target, kwargs in ((f"file:{path}?mode=ro", {"uri": True}), (str(path), {})):
        # Read-only first, so merely asking cannot alter the file.  A WAL database
        # that needs recovery cannot be opened read-only at all, so fall back to a
        # normal connection rather than reporting "unversioned" — which would send
        # the operator down the "no migration exists" path for a database that is
        # perfectly fine.
        try:
            conn = sqlite3.connect(target, **kwargs)
        except sqlite3.Error:
            continue
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if row is None:
                return None
            version = conn.execute("SELECT version FROM schema_meta").fetchone()
            return None if version is None else int(version[0])
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return None


@dataclass(frozen=True)
class Migration:
    """What a schema upgrade actually did."""

    path: Path
    from_version: int
    to_version: int
    quotes_migrated: int = 0
    event_keys_rewritten: int = 0
    markets_renamed: dict[str, int] = field(default_factory=dict)
    findings_rekeyed: int = 0
    backup_path: Path | None = None

    def summary(self) -> str:
        # ``markets_renamed`` maps old name -> **row count**, and formatting it
        # as ``old->new`` told the operator their markets were renamed to
        # numbers: "markets renamed: run_line->3348, total_runs->4272".
        renamed = ", ".join(
            f"{old} ({count:,} rows)" for old, count in sorted(self.markets_renamed.items())
        )
        parts = [
            f"{self.path}: v{self.from_version} -> v{self.to_version}",
            f"{self.quotes_migrated:,} quotes rewritten",
            f"{self.event_keys_rewritten:,} event keys rebuilt from participant keys",
        ]
        if renamed:
            parts.append(f"markets renamed: {renamed}")
        if self.findings_rekeyed:
            parts.append(f"{self.findings_rekeyed} finding(s) re-keyed")
        if self.backup_path is not None:
            parts.append(f"backup at {self.backup_path}")
        return "; ".join(parts)


class MigrationError(RuntimeError):
    """The upgrade could not be completed, and nothing was changed."""


def migrate_database(path: str | Path, *, backup: bool = True) -> Migration:
    """Upgrade a v3 database to v4 in place, or raise without changing anything.

    This is a *real* migration rather than a drop-and-recreate: the v3 rows are
    9,936 real observed prices and throwing them away would destroy the only
    history there is.  It is possible to do exactly, without guessing, because
    every difference between v3 and v4 is recoverable from what v3 already
    stored:

    * the market renames are renames of the *same* contract;
    * ``home_participant``/``away_participant`` are re-resolved from the stored
      display names through the same :func:`canonical_participant` the adapters
      use, and the result is cross-checked against the abbreviations already
      embedded in the stored ``event_key``;
    * ``event_key`` keeps its date and its doubleheader ordinal and only has its
      prefix rewritten from abbreviations to participant keys;
    * ``dedup_key`` is recomputed from the row.

    Every migrated row is rebuilt as a real :class:`~src.schema.Quote`, so a row
    that would not be legal under the new schema stops the migration instead of
    landing in the table.  Anything that cannot be resolved *aborts the whole
    upgrade inside one transaction* — a half-migrated table is the one outcome
    worse than not migrating.
    """
    path = Path(path)
    found = database_version(path)
    if found == SCHEMA_VERSION:
        return Migration(path=path, from_version=found, to_version=SCHEMA_VERSION)
    if found is None or found < OLDEST_MIGRATABLE_VERSION or found > SCHEMA_VERSION:
        raise MigrationError(
            f"{path}: cannot migrate from "
            f"{'an unversioned database' if found is None else f'version {found}'} "
            f"to version {SCHEMA_VERSION}; no upgrade path is recorded for it"
        )

    backup_path: Path | None = None
    if backup:
        backup_path = _backup(path)

    # Autocommit, so the BEGIN below really does wrap the DDL as well as the
    # inserts: sqlite3's implicit transaction handling does not cover DROP/ALTER,
    # and a half-applied table rebuild is the one outcome worse than not
    # migrating at all.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        rows = conn.execute("SELECT * FROM quote ORDER BY id").fetchall()
        quotes, key_map, renamed = _v3_to_v4_rows(rows)

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS quote_v4_migration")
        # ``execute``, not ``executescript``: executescript commits whatever
        # transaction is open before it runs, which would take the rebuild out
        # from under the BEGIN and leave a half-migrated file behind on failure.
        conn.execute(QUOTE_DDL.replace("quote", "quote_v4_migration", 1))
        if quotes:
            conn.executemany(
                f"""INSERT INTO quote_v4_migration
                    (id, {', '.join(_QUOTE_INSERT_COLUMNS)})
                    VALUES (?,{','.join('?' * len(_QUOTE_INSERT_COLUMNS))})""",
                [(row_id, *_quote_values(run_id, quote)) for row_id, run_id, quote in quotes],
            )
        conn.execute("DROP TABLE quote")
        conn.execute("ALTER TABLE quote_v4_migration RENAME TO quote")

        # Created *before* the rewrite, not with the rest of the schema
        # afterwards: the loop below issues one UPDATE per distinct event key
        # against `finding.event_key`, and unindexed that is a full table scan
        # each time — quadratic in the number of events, on the one operation an
        # operator runs against their whole history.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_finding_event ON finding(event_key)")
        rekeyed = 0
        for was, now in key_map.items():
            if was == now:
                continue
            cursor = conn.execute(
                "UPDATE finding SET event_key = ? WHERE event_key = ?", (now, was)
            )
            rekeyed += cursor.rowcount or 0

        # A finding can outlive the rows it was about — it is written for events
        # that were *rejected*, which by definition produced no quote — so the
        # map built from the quote table does not cover every key in `finding`.
        # Left alone, those keep their v3 spelling forever and the index created
        # just above, whose whole purpose is "show me every finding about this
        # fixture", silently misses them.
        #
        # v3 was baseball-only, which is what makes the leftovers recoverable
        # rather than a guess: `PHI@MIA:2026-07-20` resolves through the MLB
        # roster exactly as a quote row would have. Anything that still does not
        # resolve is counted and reported rather than rewritten.
        stranded = 0
        for row in conn.execute(
            "SELECT DISTINCT event_key FROM finding WHERE event_key IS NOT NULL"
        ).fetchall():
            old_key = row["event_key"]
            if not old_key or "-" in old_key.split("@")[0]:
                continue  # already a v4 key
            recovered = _v3_event_key(old_key)
            if recovered is None:
                stranded += 1
                continue
            cursor = conn.execute(
                "UPDATE finding SET event_key = ? WHERE event_key = ?",
                (recovered, old_key),
            )
            rekeyed += cursor.rowcount or 0
        if stranded:
            log.warning(
                "%d finding event key(s) could not be rebuilt and keep their v3 "
                "spelling; queries by fixture will not find them",
                stranded,
            )

        # Every check that can refuse the migration runs **before** the commit.
        #
        # They used to run after it, where `_rollback` is a no-op: a database
        # with one orphaned `quote.run_id` was rebuilt, re-keyed, marked version
        # 4, and *then* refused — leaving a half-migrated file behind an error
        # message that said "the database is unchanged", and, with --no-backup,
        # the remedy "restore None".
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise MigrationError(
                f"{path}: {len(broken)} row(s) have foreign keys that would be "
                "inconsistent after migration; nothing was changed"
            )
        stored = conn.execute("SELECT COUNT(*) FROM quote").fetchone()[0]
        if stored != len(rows):
            raise MigrationError(
                f"{path}: rebuilt {stored} quotes from {len(rows)} read; nothing was "
                "changed"
            )
        conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
        conn.execute("COMMIT")

        # After the commit, and only things that cannot fail the migration: the
        # table was rebuilt from the v4 DDL, so its indexes have to be recreated.
        # `IF NOT EXISTS` throughout, so this is idempotent and safe to re-run.
        conn.executescript(_SCHEMA)
        # Stamp every pre-existing run with the version it was collected under.
        # The current parser has legitimately changed since those runs — a
        # replayed v3 capture can differ from its stored rows without any byte
        # being corrupt — and without the stamp no surface could say so:
        # ``replay`` on a freshly migrated history reported FormatChangeError
        # and field drifts as corruption, and the dashboard masthead read
        # "replay FAIL (21)" immediately after a successful migrate.
        #
        # The additive columns first: ``_SCHEMA``'s CREATE TABLE IF NOT EXISTS
        # is a no-op on the pre-existing ``collection_run``, and the additive
        # pass otherwise only runs when a :class:`Store` next opens the file —
        # which has not happened yet.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(collection_run)")}
        for table, column, decl in Store._ADDED_COLUMNS:
            if table == "collection_run" and column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.execute(
            "UPDATE collection_run SET migrated_from = ? WHERE migrated_from IS NULL",
            (found,),
        )
    except MigrationError:
        _rollback(conn)
        conn.close()
        raise
    except Exception as exc:  # noqa: BLE001 - every failure must leave v3 intact
        _rollback(conn)
        conn.close()
        version = database_version(path)
        state = (
            f"the database is unchanged and still version {found}"
            if version == found
            else f"the database is now at version {version!r} and may be half-migrated; "
            + (f"restore {backup_path}" if backup_path else "no backup was taken")
        )
        raise MigrationError(
            f"{path}: migration aborted, {state} ({type(exc).__name__}: {exc})"
        ) from exc
    else:
        conn.close()

    return Migration(
        path=path,
        from_version=found,
        to_version=SCHEMA_VERSION,
        quotes_migrated=len(quotes),
        event_keys_rewritten=sum(1 for was, now in key_map.items() if was != now),
        markets_renamed=dict(renamed),
        findings_rekeyed=rekeyed,
        backup_path=backup_path,
    )


def _rollback(conn: sqlite3.Connection) -> None:
    """Undo an in-flight migration, tolerating "there was no transaction"."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _backup(path: Path) -> Path:
    """Copy the database aside, WAL included, before touching it."""
    conn = sqlite3.connect(path)
    try:
        # Without the checkpoint the copy can be missing committed pages that
        # still live only in the -wal file.
        busy, wal_frames, checkpointed = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if busy or checkpointed < wal_frames:
            # The one signal that says whether the guarantee above actually held.
            # A reader holding a transaction can block the checkpoint, and the
            # copy then silently omits committed pages — which is exactly the
            # failure the checkpoint was added to prevent, so discarding its
            # result made the safeguard unverifiable.
            raise MigrationError(
                f"could not flush the write-ahead log before copying {path.name}: "
                f"{checkpointed} of {wal_frames} frames checkpointed"
                + (" (another connection is holding it open)" if busy else "")
                + " — the backup would be missing committed data, so no backup "
                "was made and nothing was migrated"
            )
    finally:
        conn.close()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.v{database_version(path)}.{stamp}.bak")
    shutil.copy2(path, target)
    return target


def _v3_event_key(old_key: str) -> str | None:
    """Rebuild a v3 ``AWAY@HOME:date`` key as a v4 participant-key one.

    ``None`` when either abbreviation does not resolve, which is the honest
    answer: v3 stored bare abbreviations with no league beside them, and the only
    reason this is recoverable at all is that v3 was baseball-only.
    """
    head, _, tail = old_key.partition(":")
    away_abbr, _, home_abbr = head.partition("@")
    if not tail or not away_abbr or not home_abbr:
        return None
    mlb = get_league("MLB")
    away = canonical_participant(away_abbr, mlb)
    home = canonical_participant(home_abbr, mlb)
    if away is None or home is None:
        return None
    return f"{away.key}@{home.key}:{tail}"


def _v3_to_v4_rows(
    rows: Sequence[sqlite3.Row],
) -> tuple[list[tuple[int, int, Quote]], dict[str, str], dict[str, int]]:
    """Rebuild v3 rows as v4 :class:`Quote` objects, or raise.

    Returns ``(rows, event_key_map, market_rename_counts)`` where *rows* is
    ``(row_id, run_id, quote)``.  Row ids are preserved so any external note that
    refers to one still points at the same price.
    """
    rebuilt: list[tuple[int, int, Quote]] = []
    key_map: dict[str, str] = {}
    renamed: dict[str, int] = {}
    problems: list[str] = []

    for row in rows:
        league_key = row["league"]
        try:
            competition = get_league(league_key)
        except KeyError as exc:
            problems.append(f"row {row['id']}: {exc}")
            continue

        home = canonical_participant(row["home_team"], competition)
        away = canonical_participant(row["away_team"], competition)
        if home is None or away is None:
            unresolved = row["home_team"] if home is None else row["away_team"]
            problems.append(
                f"row {row['id']}: {unresolved!r} does not resolve to a participant in "
                f"{league_key}, so its identity cannot be recovered"
            )
            continue

        old_key = row["event_key"]
        prefix, _, tail = old_key.rpartition(":")
        expected_prefix = f"{away.abbr}@{home.abbr}"
        if prefix != expected_prefix:
            # The stored key and the stored names disagree.  Rebuilding from
            # either one would be a guess about which is right, and a wrong guess
            # here merges two different fixtures.
            problems.append(
                f"row {row['id']}: event_key {old_key!r} does not match the stored teams "
                f"({expected_prefix!r} expected); refusing to guess which is correct"
            )
            continue
        new_key = f"{away.key}@{home.key}:{tail}"
        key_map[old_key] = new_key

        market_value = row["market"]
        if market_value in V3_MARKET_RENAMES:
            renamed[market_value] = renamed.get(market_value, 0) + 1
            market_value = V3_MARKET_RENAMES[market_value]

        try:
            quote = Quote(
                source=row["source"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                raw_ref=row["raw_ref"],
                # v3 predates the field; a row that owed its identity to another
                # response cannot be reconstructed after the fact, so it stays
                # null rather than being pointed at the price's own response.
                identity_raw_ref=None,
                sport=Sport(row["sport"]),
                league=league_key,
                event_key=new_key,
                source_event_id=row["source_event_id"],
                home_participant=home.key,
                away_participant=away.key,
                home_team=home.name,
                away_team=away.name,
                commence_time=datetime.fromisoformat(row["commence_time"]),
                market=Market(market_value),
                period=Period(row["period"]),
                selection=Selection(row["selection"]),
                side=Side(row["side"]) if row["side"] else None,
                line=row["line"],
                is_alternate=bool(row["is_alternate"]),
                decimal_odds=row["decimal_odds"],
                american_odds=row["american_odds"],
                implied_probability=row["implied_probability"],
                source_market_id=row["source_market_id"],
                source_selection_id=row["source_selection_id"],
                limit_amount=row["limit_amount"],
                status=QuoteStatus(row["status"]),
                last_change_at=(
                    datetime.fromisoformat(row["last_change_at"])
                    if row["last_change_at"]
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a row that cannot be a Quote stops us
            problems.append(f"row {row['id']}: not valid under the new schema ({exc})")
            continue
        rebuilt.append((int(row["id"]), int(row["run_id"]), quote))

    if problems:
        shown = "\n  ".join(problems[:10])
        more = f"\n  ... and {len(problems) - 10} more" if len(problems) > 10 else ""
        raise MigrationError(
            f"{len(problems)} of {len(rows)} stored rows cannot be migrated without "
            f"guessing, so nothing was changed:\n  {shown}{more}"
        )

    # A finer dedup key cannot create a collision that v3 did not already have,
    # but the constraint is what guarantees it, so check before dropping v3.
    seen: dict[tuple[int, tuple[str, ...]], int] = {}
    for row_id, run_id, quote in rebuilt:
        key = (run_id, quote.dedup_key)
        if key in seen:
            raise MigrationError(
                f"rows {seen[key]} and {row_id} collide on the new dedup key "
                f"{'|'.join(quote.dedup_key)}; nothing was changed"
            )
        seen[key] = row_id
    return rebuilt, key_map, renamed


# ── the store ────────────────────────────────────────────────────────────────

#: Column order used by every quote insert, including the migration's.
_QUOTE_INSERT_COLUMNS = (
    "run_id", "source", "observed_at", "raw_ref", "identity_raw_ref", "sport", "league", "event_key",
    "source_event_id", "home_participant", "away_participant", "home_team", "away_team",
    "commence_time", "market", "period", "selection", "side", "line", "is_alternate",
    "decimal_odds", "american_odds", "implied_probability", "source_market_id",
    "source_selection_id", "limit_amount", "status", "last_change_at", "dedup_key",
)


def _quote_values(run_id: int, quote: Quote) -> tuple[Any, ...]:
    return (
        run_id,
        quote.source,
        _iso(quote.observed_at),
        quote.raw_ref,
        quote.identity_raw_ref,
        quote.sport.value,
        quote.league,
        quote.event_key,
        quote.source_event_id,
        quote.home_participant,
        quote.away_participant,
        quote.home_team,
        quote.away_team,
        _iso(quote.commence_time),
        quote.market.value,
        quote.period.value,
        quote.selection.value,
        quote.side.value if quote.side else None,
        quote.line,
        int(quote.is_alternate),
        quote.decimal_odds,
        quote.american_odds,
        quote.implied_probability,
        quote.source_market_id,
        quote.source_selection_id,
        quote.limit_amount,
        quote.status.value,
        _iso(quote.last_change_at),
        "|".join(quote.dedup_key),
    )


class Store:
    """Durable record of what was collected, from where, and whether it held up."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Checked before connecting, so an incompatible file is never opened for
        # writing and never has journal files created beside it.
        self._require_compatible()
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        # A second writer waits instead of dying, rather than handing the loser a
        # raw ``sqlite3.OperationalError: database is locked``.  WAL readers never
        # blocked; this is purely writer-vs-writer.
        #
        # Stated explicitly even though it changes nothing: ``sqlite3.connect``'s
        # own ``timeout=5.0`` default already sets exactly this value, so the
        # line is a no-op today.  An earlier version of this comment claimed to
        # *fix* the lock traceback, which was wrong — the default had always
        # prevented it.  It stays because the guarantee should be visible at the
        # point it is relied on rather than inherited from a default somebody
        # could change, and it is named here so nobody reads it as load-bearing.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()

    def _require_compatible(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        found = database_version(self.path)
        if found == SCHEMA_VERSION:
            return
        if found is None:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            finally:
                conn.close()
            if tables == 0:
                return  # an empty file is ours to initialize
        raise IncompatibleDatabase(self.path, found)

    #: Columns added to an existing table after it was first created.
    #:
    #: Purely additive and nullable or defaulted, so a row written before them
    #: means the same thing after: they record something the pipeline did not
    #: used to observe, not a different reading of something it did.  That is
    #: what separates them from the v3→v4 change, which re-keyed every row and
    #: therefore had to refuse to mix.  ``CREATE TABLE IF NOT EXISTS`` leaves an
    #: existing table alone, so without this an upgraded build fails its first
    #: insert against yesterday's database.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("source_health", "scopes_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("source_health", "scopes_refused", "TEXT"),
        ("source_health", "repaired_count", "INTEGER NOT NULL DEFAULT 0"),
        ("collection_run", "scope_sports", "TEXT NOT NULL DEFAULT ''"),
        ("collection_run", "scope_leagues", "TEXT NOT NULL DEFAULT ''"),
        ("collection_run", "excluded_count", "INTEGER NOT NULL DEFAULT 0"),
        ("collection_run", "migrated_from", "INTEGER"),
        ("collection_run", "counterparty_groups", "TEXT"),
    )

    def _add_missing_columns(self) -> None:
        for table, column, decl in self._ADDED_COLUMNS:
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing or column in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()
            row = self._conn.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row["version"] != SCHEMA_VERSION:
                # Unreachable via __init__, which checks first; kept so a caller
                # that swapped the file underneath us still fails loudly.
                raise IncompatibleDatabase(self.path, int(row["version"]))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── runs ─────────────────────────────────────────────────────────────────

    def start_run(
        self,
        started_at: datetime,
        *,
        sports: Sequence[str] | None = None,
        leagues: Sequence[str] | None = None,
    ) -> int:
        """Open a run, recording the scope it is being collected under.

        Recorded at the start rather than the finish so even an interrupted
        run's rows can be judged against the scope that produced them.
        """
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO collection_run (started_at, scope_sports, scope_leagues) "
                "VALUES (?, ?, ?)",
                (_iso(started_at), ",".join(sports or ()), ",".join(leagues or ())),
            )
        return int(cursor.lastrowid)

    def recorded_counterparty_groups(self, run_id: int) -> dict[str, list[frozenset[str]]]:
        """The counterparty groups the collector measured on this run's full
        slate, before any scope filter — empty for runs that predate the column
        or recorded nothing."""
        row = self._conn.execute(
            "SELECT counterparty_groups FROM collection_run WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None or not row["counterparty_groups"]:
            return {}
        decoded = json.loads(row["counterparty_groups"])
        return {
            league: [frozenset(group) for group in groups]
            for league, groups in decoded.items()
        }

    def run_scope(self, run_id: int) -> tuple[list[str], list[str], int]:
        """The scope a run was collected under: (sports, leagues, excluded).

        Falls back to the run's note when the structural columns are blank.  The
        columns arrived by additive upgrade, which backfills ``''`` — read as
        *unscoped* — so every scoped run collected before the upgrade replayed
        against an unscoped re-parse and was reported corrupt all over again:
        ``row count differs: stored 217, replayed 4454`` on a run whose own note
        says ``--sport hockey``.  The note's scope tokens are machine-written by
        ``_run_note`` (never free text), which is what makes parsing them back
        safe as a fallback and only as a fallback.
        """
        row = self._conn.execute(
            "SELECT scope_sports, scope_leagues, excluded_count, note "
            "FROM collection_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return [], [], 0
        sports = [part for part in (row["scope_sports"] or "").split(",") if part]
        leagues = [part for part in (row["scope_leagues"] or "").split(",") if part]
        if not sports and not leagues and row["note"]:
            for segment in row["note"].split("; "):
                if segment.startswith("--sport "):
                    sports = [
                        part
                        for part in segment[len("--sport "):].split(",")
                        if part
                    ]
                elif segment.startswith("--league "):
                    leagues = [
                        part
                        for part in segment[len("--league "):].split(",")
                        if part
                    ]
            excluded = int(row["excluded_count"] or 0)
            if not excluded:
                for segment in row["note"].split("; "):
                    if segment.endswith(" rows excluded by filter"):
                        head = segment.split(" ", 1)[0]
                        if head.isdigit():
                            excluded = int(head)
            return sports, leagues, excluded
        return sports, leagues, int(row["excluded_count"] or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        report: ValidationReport,
        note: str | None = None,
        excluded: int = 0,
        counterparties: dict[str, list[frozenset[str]]] | None = None,
    ) -> None:
        """Close the run, recording the rows that are **in the table**.

        ``report.quote_count`` is the count that survived validation, and every
        consumer of ``collection_run.quote_count`` reads it as the count that was
        stored: :func:`_resolve_run` refuses a finished run with no rows, the
        ``runs`` listing prints it, and the report's flow step labels it
        "5. stored".  Those are two different numbers whenever an insert fails —
        which is the whole reason :meth:`save_quotes_by_source` isolates sources.

        Recorded with every source's insert failing, the run read ``quotes 7372
        events 414`` against an empty table, and ``arb``, ``show`` and ``lines``
        each printed their zero-row sentence and exited 0 — verbatim the wording
        :func:`_resolve_run`'s guard exists to stop being mistaken for a thin
        slate.  So the number is read back from the table rather than passed in:
        a count that has to be plumbed through a caller is a count that can drift
        from the rows again.
        """
        stored, events = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT event_key) FROM quote WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        with self._conn:
            self._conn.execute(
                """UPDATE collection_run
                      SET finished_at = ?, ok = ?, quote_count = ?, event_count = ?,
                          error_count = ?, warning_count = ?, note = ?,
                          excluded_count = ?, counterparty_groups = ?
                    WHERE id = ?""",
                (
                    _iso(finished_at),
                    int(report.ok),
                    stored,
                    events,
                    len(report.errors),
                    len(report.warnings),
                    note,
                    excluded,
                    json.dumps(
                        {
                            league: sorted(sorted(group) for group in groups)
                            for league, groups in counterparties.items()
                        }
                    )
                    if counterparties
                    else None,
                    run_id,
                ),
            )

    # ── writes ───────────────────────────────────────────────────────────────

    def previous_sha(self, source: str, endpoint: str, *, before_run_id: int) -> str | None:
        """The sha256 this endpoint last returned, for change detection."""
        row = self._conn.execute(
            """SELECT sha256 FROM raw_response
                WHERE source = ? AND endpoint = ? AND run_id < ?
                ORDER BY run_id DESC, id DESC LIMIT 1""",
            (source, endpoint, before_run_id),
        ).fetchone()
        return row["sha256"] if row else None

    def record_raw(
        self, run_id: int, raw: RawResponse, path: Path, *, unchanged: bool = False
    ) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO raw_response
                   (run_id, source, endpoint, url, status_code, content_type,
                    fetched_at, sha256, byte_size, path, raw_ref, headers, unchanged)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    raw.source,
                    raw.endpoint,
                    raw.url,
                    raw.status_code,
                    raw.content_type,
                    _iso(raw.fetched_at),
                    raw.sha256,
                    raw.byte_size,
                    str(path),
                    raw.ref,
                    json.dumps(raw.headers),
                    int(unchanged),
                ),
            )

    def save_quotes(self, run_id: int, quotes: Iterable[Quote]) -> int:
        """Insert every row, or none of them.

        All-or-nothing on purpose: this is the check that a run's rows really do
        satisfy ``dedup_key``, and a caller that wants the guarantee wants it
        whole.  :meth:`save_quotes_by_source` is the variant for a live run,
        where one broken feed must not take the other nine down with it.
        """
        rows = [_quote_values(run_id, quote) for quote in quotes]
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(
                f"""INSERT INTO quote ({', '.join(_QUOTE_INSERT_COLUMNS)})
                    VALUES ({','.join('?' * len(_QUOTE_INSERT_COLUMNS))})""",
                rows,
            )
        return len(rows)

    def save_quotes_by_source(
        self, run_id: int, quotes: Iterable[Quote], *, chunk_size: int = QUOTE_CHUNK_SIZE
    ) -> tuple[int, dict[str, str]]:
        """Insert each source's rows in its own transaction.

        Returns ``(rows_stored, {source: reason})``.

        The failure this exists for is specific and total.  ``dedup_key`` is
        enforced by a UNIQUE constraint over one ``executemany``, so a *single*
        colliding row from *one* source rolls back the insert of **every** source
        in the run: thirty-four thousand good prices discarded because one
        adapter emitted a duplicate.  With ten sources that stops being a tail
        risk and becomes the expected failure mode of adding the tenth.

        Isolating per source keeps the blast radius at the source that caused it,
        and returns the reason so the run still reports the fault loudly rather
        than quietly storing nine tenths of the data.  Rows are inserted in
        chunks inside each source's transaction so a very large slate does not
        have to be bound as one statement.
        """
        by_source: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        for quote in quotes:
            by_source[quote.source].append(_quote_values(run_id, quote))

        statement = (
            f"INSERT INTO quote ({', '.join(_QUOTE_INSERT_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_QUOTE_INSERT_COLUMNS))})"
        )
        stored = 0
        failures: dict[str, str] = {}
        for source in sorted(by_source):
            rows = by_source[source]
            try:
                with self._conn:
                    for start in range(0, len(rows), max(chunk_size, 1)):
                        self._conn.executemany(statement, rows[start : start + max(chunk_size, 1)])
            except sqlite3.Error as exc:
                failures[source] = f"{type(exc).__name__}: {exc}"
            else:
                stored += len(rows)
        return stored, failures

    def save_health(self, run_id: int, health: SourceHealth) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO source_health
                   (run_id, source_key, ok, checked_at, request_count, raw_bytes, latency_ms,
                    quote_count, event_count, rejection_count, skipped_count,
                    repaired_count,
                    unchanged_payloads, scopes_requested, scopes_refused,
                    error_kind, error_message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    health.source_key,
                    int(health.ok),
                    _iso(health.checked_at),
                    health.request_count,
                    health.raw_bytes,
                    health.latency_ms,
                    health.quote_count,
                    health.event_count,
                    health.rejection_count,
                    health.skipped_count,
                    health.repaired_count,
                    health.unchanged_payloads,
                    health.scopes_requested,
                    # Stored, because a refusal that lives only in a finding's
                    # message text cannot be compared across runs — and "this
                    # league was refused yesterday too" is the fact that turns a
                    # bad afternoon into a dead feed.
                    "\n".join(health.failed_scopes) or None,
                    health.error_kind,
                    health.error_message,
                ),
            )

    def save_league_coverage(
        self, run_id: int, source_key: str, coverage: Sequence[tuple[str, str, int, int]]
    ) -> None:
        """Record ``(league, sport, quote_count, event_count)`` per configured league.

        Rows are written for leagues that produced **nothing** as well, because
        that is the whole point: a configured league with a zero count is a gap,
        and if it were simply absent it would look like a league nobody asked
        for.
        """
        if not coverage:
            return
        with self._conn:
            self._conn.executemany(
                """INSERT OR REPLACE INTO source_league
                   (run_id, source_key, league, sport, configured, quote_count, event_count)
                   VALUES (?,?,?,?,1,?,?)""",
                [
                    (run_id, source_key, league, sport, quote_count, event_count)
                    for league, sport, quote_count, event_count in coverage
                ],
            )

    def save_rejections(self, run_id: int, rejections: Sequence[Rejection]) -> None:
        if not rejections:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT INTO rejection (run_id, source, reason, detail, context) VALUES (?,?,?,?,?)",
                [
                    (run_id, r.source, r.reason, r.detail, json.dumps(r.context, default=str))
                    for r in rejections
                ],
            )

    def save_repaired(self, run_id: int, source: str, repaired: dict[str, int]) -> None:
        """Rows published after a field was reconstructed, per reason.

        Its own table for the same reason it has its own counter: these rows are
        in the output, and filing them beside the skips said they were not.
        """
        if not repaired:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO repaired (run_id, source, reason, count) "
                "VALUES (?,?,?,?)",
                [(run_id, source, reason, count) for reason, count in repaired.items()],
            )

    def save_skipped(self, run_id: int, source: str, skipped: dict[str, int]) -> None:
        if not skipped:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO skipped (run_id, source, reason, count) VALUES (?,?,?,?)",
                [(run_id, source, reason, count) for reason, count in skipped.items()],
            )

    def save_findings(self, run_id: int, findings: Sequence[Finding]) -> None:
        if not findings:
            return
        with self._conn:
            self._conn.executemany(
                """INSERT INTO finding (run_id, severity, code, message, source, event_key)
                   VALUES (?,?,?,?,?,?)""",
                [
                    (run_id, f.severity.value, f.code, f.message, f.source, f.event_key)
                    for f in findings
                ],
            )

    # ── reads ────────────────────────────────────────────────────────────────

    def load_quotes(
        self,
        run_id: int,
        *,
        sports: Sequence[str] | str | None = None,
        leagues: Sequence[str] | str | None = None,
    ) -> list[Quote]:
        """Every stored row of a run, optionally narrowed to sports/leagues."""
        clause, params = _scope(sports, leagues)
        rows = self._conn.execute(
            f"SELECT * FROM quote WHERE run_id = ?{clause} ORDER BY id", (run_id, *params)
        ).fetchall()
        return [_quote_from_row(row) for row in rows]

    def sports_for_run(self, run_id: int) -> list[str]:
        return [
            row["sport"]
            for row in self._conn.execute(
                "SELECT DISTINCT sport FROM quote WHERE run_id = ? ORDER BY sport", (run_id,)
            )
        ]

    def leagues_for_run(self, run_id: int, *, sports: Sequence[str] | str | None = None) -> list[str]:
        clause, params = _scope(sports, None)
        return [
            row["league"]
            for row in self._conn.execute(
                f"SELECT DISTINCT league FROM quote WHERE run_id = ?{clause} ORDER BY league",
                (run_id, *params),
            )
        ]

    def sport_coverage(self, run_id: int) -> list[sqlite3.Row]:
        """Per sport: how many books contributed, and how much.

        This is the query behind "which sports are actually usable": a sport
        priced by one book cannot be compared against anything, no matter how
        many rows it has.
        """
        return self._conn.execute(
            """SELECT sport,
                      COUNT(DISTINCT source)    AS source_count,
                      COUNT(DISTINCT league)    AS league_count,
                      COUNT(DISTINCT event_key) AS event_count,
                      COUNT(*)                  AS quote_count
                 FROM quote WHERE run_id = ?
                GROUP BY sport ORDER BY sport""",
            (run_id,),
        ).fetchall()

    def cross_book_event_counts(self, run_id: int, *, min_books: int = 2) -> dict[str, int]:
        """Per sport: how many fixtures were priced by *min_books* or more books.

        The sharper measure of whether a sport is usable.  "Two books produced
        hockey rows" can be true while the two books have no fixture in common —
        one has the NHL openers, the other a Belarusian friendly — and in that
        case there is still nothing to compare.  Counting the *overlap* is what
        tells those two situations apart.
        """
        rows = self._conn.execute(
            """SELECT sport, COUNT(*) AS n FROM (
                   SELECT sport, event_key
                     FROM quote WHERE run_id = ?
                    GROUP BY sport, event_key
                   HAVING COUNT(DISTINCT source) >= ?
               ) GROUP BY sport""",
            (run_id, min_books),
        ).fetchall()
        return {row["sport"]: row["n"] for row in rows}

    def sport_source_matrix(self, run_id: int) -> list[sqlite3.Row]:
        """Per ``(sport, source)`` counts — the coverage grid the dashboard draws."""
        return self._conn.execute(
            """SELECT sport, source, league,
                      COUNT(DISTINCT event_key) AS event_count,
                      COUNT(*)                  AS quote_count
                 FROM quote WHERE run_id = ?
                GROUP BY sport, source, league
                ORDER BY sport, league, source""",
            (run_id,),
        ).fetchall()

    def league_coverage(self, run_id: int) -> list[sqlite3.Row]:
        """What each source was asked for, and what it returned, per league."""
        return self._conn.execute(
            """SELECT source_key, league, sport, configured, quote_count, event_count
                 FROM source_league WHERE run_id = ?
                ORDER BY sport, league, source_key""",
            (run_id,),
        ).fetchall()

    def raw_paths(self, run_id: int, source: str | None = None) -> list[tuple[str, Path]]:
        sql = "SELECT source, path FROM raw_response WHERE run_id = ?"
        params: list[Any] = [run_id]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id"
        return [(row["source"], Path(row["path"])) for row in self._conn.execute(sql, params)]

    def run_row(self, run_id: int) -> sqlite3.Row | None:
        """One run's own record, or ``None`` if there is no such run.

        The distinction a read command needs in order to tell "you named a run
        that does not exist" from "that run found nothing" — two answers that
        used to print the same sentence and exit 0.
        """
        rows = self.query(
            "SELECT id, started_at, finished_at, ok, quote_count, error_count, "
            "migrated_from FROM collection_run WHERE id = ?",
            (run_id,),
        )
        return rows[0] if rows else None

    def latest_run_id(
        self,
        *,
        only_ok: bool = False,
        sports: Sequence[str] | str | None = None,
        leagues: Sequence[str] | str | None = None,
    ) -> int | None:
        """Newest finished run — holding a row in scope, when a scope is given.

        Scope-blind resolution made ``arb --sport baseball`` resolve a run
        collected under ``--sport tennis`` (which kept no baseball by the
        operator's own flag), print the quiet-slate sentence and exit 0 — while
        the previous run's real baseball edge sat one ``--run`` away, and
        ``health --sport baseball`` on the same database described that earlier
        run.  Two commands, seconds apart, describing different collections.
        """
        sql = "SELECT id FROM collection_run r WHERE finished_at IS NOT NULL"
        clause, params = _scope(sports, leagues, prefix="q.")
        if clause:
            # ``status = 'active'``: a suspended price is a row, not a price
            # anybody can act on, so a run whose only in-scope rows are
            # suspended does not "hold prices in scope".  Without it,
            # ``arb --league MLB`` resolved such a run and printed "0
            # opportunities from 0 cross-book markets" — the quiet-slate
            # sentence ``_resolve_run`` exists to prevent — while a takeable
            # edge sat one ``--run`` away.
            sql += (
                " AND EXISTS (SELECT 1 FROM quote q WHERE q.run_id = r.id"
                f"{clause} AND q.status = 'active')"
            )
        if only_ok:
            sql += " AND ok = 1"
        sql += " ORDER BY id DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return int(row["id"]) if row else None

    def run_summaries(
        self,
        limit: int = 10,
        *,
        sports: Sequence[str] | str | None = None,
        leagues: Sequence[str] | str | None = None,
    ) -> list[sqlite3.Row]:
        """Recent runs, newest first.

        With a sport or league scope, only runs that stored a matching row are
        listed — a run that collected no hockey is not a hockey run.
        """
        clause, params = _scope(sports, leagues, prefix="q.")
        where = ""
        if clause:
            where = (
                " WHERE EXISTS (SELECT 1 FROM quote q WHERE q.run_id = collection_run.id"
                f"{clause})"
            )
        return self._conn.execute(
            f"""SELECT id, started_at, finished_at, ok, quote_count, event_count,
                       error_count, warning_count, note
                  FROM collection_run{where}
                 ORDER BY id DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()

    def sources_that_have_produced(self, before_run_id: int) -> set[str]:
        """Every source that has *ever* produced a row before this run.

        The comparison set for "this feed worked and has stopped".

        Unbounded on purpose.  A bounded lookback — "the last ten runs" — makes
        the alarm expire while the fault persists: after ten more runs the dead
        source drops out of the window, the ERROR disappears, and with two other
        sources still producing the run goes green again.  An unattended watch
        loop would then report success indefinitely with a permanently broken
        book.  Silence is the wrong default for "this used to work".

        Read from ``source_health.quote_count`` rather than by scanning ``quote``:
        that table holds one small row per source per run and is keyed
        ``(run_id, source_key)``, so this stays a cheap lookup as the quote table
        grows into millions of rows.  ``quote_count`` is a *fact* about what was
        produced, not the ``ok`` judgement beside it — a source marked unhealthy
        for a rejection still produced prices, and losing them is still a
        regression.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT source_key FROM source_health "
            "WHERE run_id < ? AND quote_count > 0",
            (before_run_id,),
        ).fetchall()
        return {row["source_key"] for row in rows}

    def health_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM source_health WHERE run_id = ? ORDER BY source_key", (run_id,)
        ).fetchall()

    def sources_that_failed_to_persist(self, run_id: int) -> set[str] | None:
        """Sources whose rows raised ``quotes_not_persisted`` on this run.

        ``None`` means the insert failed for the run as a whole rather than per
        source, which is how :meth:`save_quotes_by_source` reports an exception it
        could not attribute — every source lost its rows.

        Needed because "produced more than it stored" is *not* on its own a
        storage fault: the ``--sport``/``--league`` filter runs after a source has
        already been counted, so a scoped collection legitimately keeps fewer rows
        than it fetched.  Reading the difference as a lost write accused the
        database of dropping rows the operator asked it to drop.
        """
        rows = self._conn.execute(
            "SELECT source FROM finding WHERE run_id = ? AND code = 'quotes_not_persisted'",
            (run_id,),
        ).fetchall()
        if any(row["source"] is None for row in rows):
            return None
        return {row["source"] for row in rows}

    def stored_quote_counts(self, run_id: int) -> dict[str, int]:
        """Rows actually in the table for this run, per source.

        ``source_health.quote_count`` is what the adapter produced and validated,
        which is the right number for judging the adapter and the wrong one for
        judging the database.  They differ exactly when that source's insert
        failed — the case :meth:`save_quotes_by_source` isolates — and the
        ``runs`` listing was printing the produced count beside ``ok`` for a
        source with no rows stored at all.
        """
        return {
            row["source"]: row["stored"]
            for row in self._conn.execute(
                "SELECT source, COUNT(*) AS stored FROM quote WHERE run_id = ? "
                "GROUP BY source",
                (run_id,),
            )
        }

    def health_by_sport(
        self, run_id: int, *, sports: Sequence[str] | str | None = None
    ) -> list[sqlite3.Row]:
        """Per-source, per-sport row counts for one run."""
        clause, params = _scope(sports, None)
        return self._conn.execute(
            f"""SELECT source, sport,
                       COUNT(DISTINCT event_key) AS event_count,
                       COUNT(*)                  AS quote_count
                  FROM quote WHERE run_id = ?{clause}
                 GROUP BY source, sport ORDER BY sport, source""",
            (run_id, *params),
        ).fetchall()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()


def _quote_from_row(row: sqlite3.Row) -> Quote:
    return Quote(
        source=row["source"],
        observed_at=datetime.fromisoformat(row["observed_at"]),
        raw_ref=row["raw_ref"],
        identity_raw_ref=row["identity_raw_ref"],
        sport=Sport(row["sport"]),
        league=row["league"],
        event_key=row["event_key"],
        source_event_id=row["source_event_id"],
        home_participant=row["home_participant"],
        away_participant=row["away_participant"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        commence_time=datetime.fromisoformat(row["commence_time"]),
        market=Market(row["market"]),
        period=Period(row["period"]),
        selection=Selection(row["selection"]),
        side=Side(row["side"]) if row["side"] else None,
        line=row["line"],
        is_alternate=bool(row["is_alternate"]),
        decimal_odds=row["decimal_odds"],
        american_odds=row["american_odds"],
        implied_probability=row["implied_probability"],
        source_market_id=row["source_market_id"],
        source_selection_id=row["source_selection_id"],
        limit_amount=row["limit_amount"],
        status=QuoteStatus(row["status"]),
        last_change_at=(
            datetime.fromisoformat(row["last_change_at"]) if row["last_change_at"] else None
        ),
    )
