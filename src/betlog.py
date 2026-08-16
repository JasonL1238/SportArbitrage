"""What the operator actually placed, and how it settled.

Its own SQLite file, for the same reason ``src/promos/store.py`` has one: this
is the only *hand-entered* durable record in the repository.  A collector
database can be deleted and re-scraped from the venues at any time; a bet
history cannot be reconstructed from anything, so it must not share a lifecycle,
a migration path, or a ``DELETE FROM`` with collected data.

Rows here are a **snapshot**, deliberately denormalized.  A slip stores the
team names, market, period and price as plain text and numbers rather than
foreign keys into the odds database or the closed vocabularies, because a bet
placed in August is a historical fact: it has to keep reading correctly after
the quote row is pruned, the event key is rewritten by a migration, or a
:mod:`src.vocab` member is renamed.

Money is stored in integer cents.  Summing a season of floating-point dollars
and calling the result a profit is how a ledger ends up off by a cent that
nobody can find; the conversion happens once at the boundary, in
:func:`_cents`, and every total below is exact integer arithmetic.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 2

#: How a leg finished.  ``pending`` is the only state that is not an outcome.
STATUSES: tuple[str, ...] = (
    "pending",
    "won",
    "lost",
    "push",
    "void",
    "cashout",
)

#: Statuses that mean money has come back (or definitively has not).
SETTLED_STATUSES: frozenset[str] = frozenset(STATUSES) - {"pending"}

#: What a slip is.  ``arb`` is a set of legs placed together as one position;
#: ``single`` is one bet on its own.  The distinction is only ever presentational
#: — every total below is computed leg by leg, so a half-filled arb is still
#: accounted for honestly rather than as an all-or-nothing unit.
KINDS: tuple[str, ...] = ("arb", "single", "promo")

#: What a leg was staked with.  ``cash`` is the operator's money; ``bonus`` is
#: stake-not-returned site credit (a winning bonus bet pays winnings only);
#: ``boosted`` is cash whose price carries a boost — informational, priced by
#: the odds the operator logs.  The distinction is load-bearing arithmetic:
#: logging a bonus leg as cash overstates its payout by the whole stake and
#: counts credit the operator never risked as bankroll.
STAKE_KINDS: tuple[str, ...] = ("cash", "bonus", "boosted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bet_slip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'single',
    placed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sport TEXT NOT NULL DEFAULT '',
    league TEXT NOT NULL DEFAULT '',
    event_key TEXT NOT NULL DEFAULT '',
    home_team TEXT NOT NULL DEFAULT '',
    away_team TEXT NOT NULL DEFAULT '',
    commence_time TEXT,
    market TEXT NOT NULL DEFAULT '',
    period TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    line REAL,
    margin_pct REAL,
    expected_profit_cents INTEGER,
    source_run_id INTEGER,
    promo_source TEXT NOT NULL DEFAULT '',
    promo_offer_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bet_leg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slip_id INTEGER NOT NULL REFERENCES bet_slip(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    book TEXT NOT NULL DEFAULT '',
    selection TEXT NOT NULL DEFAULT '',
    line REAL,
    american_odds INTEGER,
    decimal_odds REAL NOT NULL,
    stake_kind TEXT NOT NULL DEFAULT 'cash',
    stake_cents INTEGER NOT NULL,
    to_return_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    returned_cents INTEGER,
    settled_at TEXT,
    link_url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS bet_leg_slip ON bet_leg(slip_id);
CREATE INDEX IF NOT EXISTS bet_slip_placed ON bet_slip(placed_at);
"""


class BetLogError(ValueError):
    """A wager the ledger refuses to record or change."""


# ── conversions ──────────────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _now() -> str:
    stamp = _iso(datetime.now(UTC))
    assert stamp is not None  # datetime.now is never None; narrow for the checker
    return stamp


def _cents(amount: Any, *, label: str, allow_none: bool = False) -> int | None:
    """Dollars in, exact cents out — the only place the conversion happens."""
    if amount is None or amount == "":
        if allow_none:
            return None
        raise BetLogError(f"{label} is required")
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise BetLogError(f"{label} must be a number, got {amount!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise BetLogError(f"{label} must be a finite number")
    if value < 0:
        raise BetLogError(f"{label} cannot be negative")
    if value > 10_000_000:
        raise BetLogError(f"{label} is implausibly large: {value}")
    # round() alone is banker's rounding; 0.005 has to go up, like money does.
    return int((value * 100.0) + 0.5)


def _dollars(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100.0, 2)


def american_from_decimal(decimal_odds: float) -> int:
    """The same price in American notation, for a leg logged by decimal only."""
    if decimal_odds >= 2.0:
        return int(round((decimal_odds - 1.0) * 100.0))
    return int(round(-100.0 / (decimal_odds - 1.0)))


def decimal_from_american(american_odds: int) -> float:
    """The same price as a multiplier, for a leg logged the way a slip prints it."""
    if american_odds == 0:
        raise BetLogError("american odds of 0 is not a price")
    if american_odds > 0:
        return 1.0 + (american_odds / 100.0)
    return 1.0 + (100.0 / abs(american_odds))


def _price(
    *, american_odds: Any, decimal_odds: Any
) -> tuple[int | None, float]:
    """Resolve a leg's price from whichever notation was supplied.

    Both, one, or the other.  A leg logged from the dashboard arrives with both
    because the scrape had both; a leg typed in by hand usually has only the
    American number off the bet slip, and re-deriving the decimal here is what
    keeps the payout arithmetic in one place.
    """
    dec: float | None = None
    if decimal_odds not in (None, ""):
        try:
            dec = float(decimal_odds)
        except (TypeError, ValueError):
            raise BetLogError(f"decimal odds must be a number, got {decimal_odds!r}") from None
    amer: int | None = None
    if american_odds not in (None, ""):
        try:
            amer = int(round(float(american_odds)))
        except (TypeError, ValueError):
            raise BetLogError(f"american odds must be a number, got {american_odds!r}") from None
    if dec is None and amer is None:
        raise BetLogError("a leg needs a price: american odds or decimal odds")
    if dec is None:
        assert amer is not None
        dec = decimal_from_american(amer)
    if not 1.0 < dec <= 1000.0:
        # Same bound the collected schema enforces.  1.0 is "risk everything to
        # win nothing", which is never a price anybody was offered.
        raise BetLogError(f"decimal odds out of range: {dec}")
    if amer is None:
        amer = american_from_decimal(dec)
    return amer, dec


def _text(value: Any, *, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _line(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise BetLogError(f"line must be a number, got {value!r}") from None
    if out != out or out in (float("inf"), float("-inf")):
        raise BetLogError("line must be a finite number")
    return out


def _stake_kind(value: Any) -> str:
    kind = str(value or "cash").strip().lower()
    if kind not in STAKE_KINDS:
        raise BetLogError(
            f"unknown stake kind {value!r}; expected one of {', '.join(STAKE_KINDS)}"
        )
    return kind


def _status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status not in STATUSES:
        raise BetLogError(
            f"unknown status {value!r}; expected one of {', '.join(STATUSES)}"
        )
    return status


def default_return_cents(
    status: str,
    *,
    stake_cents: int,
    to_return_cents: int,
    stake_kind: str = "cash",
) -> int | None:
    """What a settled leg pays back before the operator overrides it.

    ``push`` and ``void`` return the stake, which is why they are not the same
    as ``lost`` and why a ledger that folds them into one is wrong about both
    turnover and record.  ``cashout`` has no derivable answer — the operator
    took a number the book offered — so it stays ``None`` until one is given.

    A ``bonus`` leg's push/void returns **0 cash**: the stake was credit, and
    what most books hand back is the credit itself, not money.  When a book
    does re-credit the bonus bet, that is a fact about the *credit*, recorded
    by re-logging the re-run — the cash ledger saw nothing come back.
    """
    if status == "won":
        return to_return_cents
    if status == "lost":
        return 0
    if status in ("push", "void"):
        return 0 if stake_kind == "bonus" else stake_cents
    return None


# ── inputs ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LegInput:
    """One wager at one book, as the operator placed it."""

    book: str
    selection: str
    stake: float
    american_odds: int | None = None
    decimal_odds: float | None = None
    line: float | None = None
    status: str = "pending"
    returned: float | None = None
    link_url: str = ""
    note: str = ""
    stake_kind: str = "cash"


@dataclass(frozen=True)
class SlipInput:
    """One position: a single bet, or every leg of an arb placed together."""

    legs: Sequence[LegInput]
    kind: str = "single"
    sport: str = ""
    league: str = ""
    event_key: str = ""
    home_team: str = ""
    away_team: str = ""
    commence_time: str | None = None
    market: str = ""
    period: str = ""
    side: str = ""
    line: float | None = None
    margin_pct: float | None = None
    expected_profit: float | None = None
    source_run_id: int | None = None
    note: str = ""
    placed_at: str | None = None
    promo_source: str = ""
    promo_offer_id: str = ""


def slip_from_payload(payload: Mapping[str, Any]) -> SlipInput:
    """Build a :class:`SlipInput` from the dashboard's JSON body.

    The dashboard posts back the very fields it was rendered from, so a bet
    logged off an arb card carries the scraped teams, market, period, line and
    both odds notations without the operator retyping any of it.  Everything is
    still validated here: the page is a convenience, not a trusted source.
    """
    if not isinstance(payload, Mapping):
        raise BetLogError("bet must be a JSON object")
    raw_legs = payload.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        raise BetLogError("a bet needs at least one leg")
    if len(raw_legs) > 20:
        raise BetLogError("a bet cannot have more than 20 legs")
    legs: list[LegInput] = []
    for entry in raw_legs:
        if not isinstance(entry, Mapping):
            raise BetLogError("each leg must be a JSON object")
        legs.append(
            LegInput(
                book=_text(entry.get("book"), limit=80),
                selection=_text(entry.get("selection"), limit=120),
                stake=entry.get("stake"),  # type: ignore[arg-type]  # validated in _cents
                american_odds=entry.get("american_odds"),
                decimal_odds=entry.get("decimal_odds"),
                line=entry.get("line"),
                status=str(entry.get("status") or "pending"),
                returned=entry.get("returned"),
                link_url=_text(entry.get("link_url"), limit=600),
                note=_text(entry.get("note")),
                stake_kind=str(entry.get("stake_kind") or "cash"),
            )
        )
    kind = str(payload.get("kind") or ("arb" if len(legs) > 1 else "single")).strip().lower()
    if kind not in KINDS:
        raise BetLogError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    return SlipInput(
        legs=legs,
        kind=kind,
        sport=_text(payload.get("sport"), limit=40),
        league=_text(payload.get("league"), limit=40),
        event_key=_text(payload.get("event_key"), limit=200),
        home_team=_text(payload.get("home_team"), limit=120),
        away_team=_text(payload.get("away_team"), limit=120),
        commence_time=_text(payload.get("commence_time"), limit=40) or None,
        market=_text(payload.get("market"), limit=40),
        period=_text(payload.get("period"), limit=40),
        side=_text(payload.get("side"), limit=40),
        line=payload.get("line"),
        margin_pct=payload.get("margin_pct"),
        expected_profit=payload.get("expected_profit"),
        source_run_id=payload.get("source_run_id"),
        note=_text(payload.get("note"), limit=1000),
        placed_at=_text(payload.get("placed_at"), limit=40) or None,
        promo_source=_text(payload.get("promo_source"), limit=80),
        promo_offer_id=_text(payload.get("promo_offer_id"), limit=200),
    )


# ── store ────────────────────────────────────────────────────────────────────


@dataclass
class BetLog:
    """The placed-bet ledger.  One SQLite file, opened per caller."""

    path: Path
    _conn: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row["value"]) if row is not None else 0
        if row is not None and current < 2:
            # v1 -> v2: promo awareness.  ALTERs are idempotent against the
            # column set because a v1 file predates every one of these.
            leg_cols = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(bet_leg)").fetchall()
            }
            if "stake_kind" not in leg_cols:
                self._conn.execute(
                    "ALTER TABLE bet_leg ADD COLUMN stake_kind TEXT NOT NULL DEFAULT 'cash'"
                )
            slip_cols = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(bet_slip)").fetchall()
            }
            for name in ("promo_source", "promo_offer_id"):
                if name not in slip_cols:
                    self._conn.execute(
                        f"ALTER TABLE bet_slip ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif current != SCHEMA_VERSION:
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BetLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── writing ──────────────────────────────────────────────────────────────

    def record(self, slip: SlipInput) -> int:
        """Store one placed position and return its slip id."""
        if not slip.legs:
            raise BetLogError("a bet needs at least one leg")
        rows: list[tuple[Any, ...]] = []
        for index, leg in enumerate(slip.legs):
            amer, dec = _price(
                american_odds=leg.american_odds, decimal_odds=leg.decimal_odds
            )
            stake_cents = _cents(leg.stake, label="stake")
            assert stake_cents is not None
            if stake_cents <= 0:
                raise BetLogError("stake must be more than zero")
            stake_kind = _stake_kind(leg.stake_kind)
            # A bonus bet returns winnings only — the stake was credit, never
            # cash.  ``stake × dec`` here overstated the payout by the whole
            # stake, which is exactly the number a promo conversion's profit
            # gets wrong by.
            to_return = int(round(stake_cents * (dec - 1.0 if stake_kind == "bonus" else dec)))
            status = _status(leg.status)
            returned = _cents(leg.returned, label="returned", allow_none=True)
            if status != "pending" and returned is None:
                returned = default_return_cents(
                    status,
                    stake_cents=stake_cents,
                    to_return_cents=to_return,
                    stake_kind=stake_kind,
                )
            if status == "pending" and returned is not None:
                raise BetLogError("a pending leg cannot have a returned amount")
            rows.append((
                index,
                _text(leg.book, limit=80),
                _text(leg.selection, limit=120),
                _line(leg.line),
                amer,
                dec,
                stake_kind,
                stake_cents,
                to_return,
                status,
                returned,
                _now() if status != "pending" else None,
                _text(leg.link_url, limit=600),
                _text(leg.note),
            ))

        now = _now()
        placed_at = _text(slip.placed_at, limit=40) or now
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO bet_slip(
                       kind, placed_at, created_at, updated_at, sport, league,
                       event_key, home_team, away_team, commence_time, market,
                       period, side, line, margin_pct, expected_profit_cents,
                       source_run_id, promo_source, promo_offer_id, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    slip.kind if slip.kind in KINDS else "single",
                    placed_at,
                    now,
                    now,
                    _text(slip.sport, limit=40),
                    _text(slip.league, limit=40),
                    _text(slip.event_key, limit=200),
                    _text(slip.home_team, limit=120),
                    _text(slip.away_team, limit=120),
                    _text(slip.commence_time, limit=40) or None,
                    _text(slip.market, limit=40),
                    _text(slip.period, limit=40),
                    _text(slip.side, limit=40),
                    _line(slip.line),
                    None if slip.margin_pct in (None, "") else float(slip.margin_pct),
                    _cents(slip.expected_profit, label="expected profit", allow_none=True),
                    None if slip.source_run_id in (None, "") else int(slip.source_run_id),
                    _text(slip.promo_source, limit=80),
                    _text(slip.promo_offer_id, limit=200),
                    _text(slip.note, limit=1000),
                ),
            )
            slip_id = int(cur.lastrowid or 0)
            self._conn.executemany(
                """INSERT INTO bet_leg(
                       slip_id, position, book, selection, line, american_odds,
                       decimal_odds, stake_kind, stake_cents, to_return_cents,
                       status, returned_cents, settled_at, link_url, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(slip_id, *row) for row in rows],
            )
        return slip_id

    def update_leg(self, leg_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Edit one leg in place and return its slip, re-read.

        Editing a price or a stake re-derives ``to_return_cents``, and a leg that
        is already settled on the derived amount is moved with it.  Leaving the
        old payout behind is how an edited stake produces a ledger that reports a
        profit nobody was ever paid.
        """
        row = self._conn.execute(
            "SELECT * FROM bet_leg WHERE id = ?", (leg_id,)
        ).fetchone()
        if row is None:
            raise BetLogError(f"no leg with id {leg_id}")

        sets: dict[str, Any] = {}
        if "book" in changes:
            sets["book"] = _text(changes["book"], limit=80)
        if "selection" in changes:
            sets["selection"] = _text(changes["selection"], limit=120)
        if "line" in changes:
            sets["line"] = _line(changes["line"])
        if "note" in changes:
            sets["note"] = _text(changes["note"])
        if "link_url" in changes:
            sets["link_url"] = _text(changes["link_url"], limit=600)

        priced = "american_odds" in changes or "decimal_odds" in changes
        dec = float(row["decimal_odds"])
        if priced:
            # Only what was actually sent is carried in.  Falling back to the
            # stored decimal when the operator retyped the American number meant
            # ``_price`` preferred the stale decimal and silently discarded the
            # edit — the row then showed the new American odds beside a payout
            # computed from the old price.
            amer, dec = _price(
                american_odds=changes.get("american_odds"),
                decimal_odds=changes.get("decimal_odds"),
            )
            sets["american_odds"] = amer
            sets["decimal_odds"] = dec

        stake_cents = int(row["stake_cents"])
        if "stake" in changes:
            new_stake = _cents(changes["stake"], label="stake")
            assert new_stake is not None
            if new_stake <= 0:
                raise BetLogError("stake must be more than zero")
            stake_cents = new_stake
            sets["stake_cents"] = stake_cents

        stake_kind = _stake_kind(row["stake_kind"])
        old_to_return = int(row["to_return_cents"])
        to_return = old_to_return
        if priced or "stake" in changes:
            to_return = int(
                round(stake_cents * (dec - 1.0 if stake_kind == "bonus" else dec))
            )
            sets["to_return_cents"] = to_return

        status = str(row["status"])
        if "status" in changes:
            status = _status(changes["status"])
            sets["status"] = status
            sets["settled_at"] = None if status == "pending" else (
                _text(changes.get("settled_at"), limit=40) or row["settled_at"] or _now()
            )
            if status == "pending":
                sets["returned_cents"] = None
            elif "returned" not in changes:
                sets["returned_cents"] = default_return_cents(
                    status,
                    stake_cents=stake_cents,
                    to_return_cents=to_return,
                    stake_kind=stake_kind,
                )
        elif priced or "stake" in changes:
            # Price or stake moved under a leg already settled.  Only a return
            # that still matches the *old* derived amount is a derived one; a
            # number the operator typed is theirs and is left alone.
            if status != "pending" and row["returned_cents"] is not None:
                was_derived = int(row["returned_cents"]) == default_return_cents(
                    status,
                    stake_cents=int(row["stake_cents"]),
                    to_return_cents=old_to_return,
                    stake_kind=stake_kind,
                )
                if was_derived:
                    sets["returned_cents"] = default_return_cents(
                        status,
                        stake_cents=stake_cents,
                        to_return_cents=to_return,
                        stake_kind=stake_kind,
                    )

        if "returned" in changes:
            returned = _cents(changes["returned"], label="returned", allow_none=True)
            if status == "pending" and returned is not None:
                raise BetLogError("settle the leg before recording what it returned")
            sets["returned_cents"] = returned

        if not sets:
            return self.slip(int(row["slip_id"]))  # type: ignore[return-value]

        assignments = ", ".join(f"{name} = ?" for name in sets)
        with self._conn:
            self._conn.execute(
                f"UPDATE bet_leg SET {assignments} WHERE id = ?",
                (*sets.values(), leg_id),
            )
            self._conn.execute(
                "UPDATE bet_slip SET updated_at = ? WHERE id = ?",
                (_now(), int(row["slip_id"])),
            )
        slip = self.slip(int(row["slip_id"]))
        assert slip is not None
        return slip

    def settle_slip(self, slip_id: int, status: str) -> dict[str, Any]:
        """Apply one outcome to every leg of a slip.

        Only useful for a single, and for the arb case where every leg really did
        land the same way.  Settling the legs of a real arb individually is the
        normal path — that is the whole point of one of them losing.
        """
        checked = _status(status)
        legs = self._conn.execute(
            "SELECT id FROM bet_leg WHERE slip_id = ? ORDER BY position, id", (slip_id,)
        ).fetchall()
        if not legs:
            raise BetLogError(f"no bet with id {slip_id}")
        for leg in legs:
            self.update_leg(int(leg["id"]), {"status": checked})
        slip = self.slip(slip_id)
        assert slip is not None
        return slip

    def update_slip(self, slip_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Edit the descriptive fields of a slip (not its money)."""
        row = self._conn.execute(
            "SELECT id FROM bet_slip WHERE id = ?", (slip_id,)
        ).fetchone()
        if row is None:
            raise BetLogError(f"no bet with id {slip_id}")
        sets: dict[str, Any] = {}
        if "note" in changes:
            sets["note"] = _text(changes["note"], limit=1000)
        if "placed_at" in changes:
            stamp = _text(changes["placed_at"], limit=40)
            if not stamp:
                raise BetLogError("placed_at cannot be blank")
            sets["placed_at"] = stamp
        for name in ("home_team", "away_team", "league", "sport", "market", "period", "side"):
            if name in changes:
                sets[name] = _text(changes[name], limit=120)
        if sets:
            sets["updated_at"] = _now()
            assignments = ", ".join(f"{key} = ?" for key in sets)
            with self._conn:
                self._conn.execute(
                    f"UPDATE bet_slip SET {assignments} WHERE id = ?",
                    (*sets.values(), slip_id),
                )
        slip = self.slip(slip_id)
        assert slip is not None
        return slip

    def delete_slip(self, slip_id: int) -> bool:
        """Remove a slip and its legs.  Returns whether there was one."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM bet_slip WHERE id = ?", (slip_id,))
            # ON DELETE CASCADE needs the pragma, which is set per connection;
            # deleting the legs explicitly means an orphan cannot survive a
            # connection that somehow opened without it.
            self._conn.execute("DELETE FROM bet_leg WHERE slip_id = ?", (slip_id,))
        return cur.rowcount > 0

    # ── reading ──────────────────────────────────────────────────────────────

    def slip(self, slip_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM bet_slip WHERE id = ?", (slip_id,)
        ).fetchone()
        if row is None:
            return None
        legs = self._conn.execute(
            "SELECT * FROM bet_leg WHERE slip_id = ? ORDER BY position, id", (slip_id,)
        ).fetchall()
        return _slip_payload(row, legs)

    def slips(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Every logged position, newest placement first."""
        rows = self._conn.execute(
            "SELECT * FROM bet_slip ORDER BY placed_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        if not rows:
            return []
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        by_slip: dict[int, list[sqlite3.Row]] = {slip_id: [] for slip_id in ids}
        for leg in self._conn.execute(
            f"SELECT * FROM bet_leg WHERE slip_id IN ({placeholders}) "
            "ORDER BY slip_id, position, id",
            ids,
        ):
            by_slip[int(leg["slip_id"])].append(leg)
        return [_slip_payload(row, by_slip[int(row["id"])]) for row in rows]

    def payload(self, *, limit: int = 500) -> dict[str, Any]:
        """Everything the dashboard's bets panel renders from."""
        slips = self.slips(limit=limit)
        return {
            "slips": slips,
            "summary": summarize(slips),
            "statuses": list(STATUSES),
        }


def _cash_stake(leg: sqlite3.Row) -> int:
    """A leg's claim on the bankroll: its stake, unless the stake was credit.

    A bonus bet's face amount is real for computing its payout and is shown on
    the leg — but none of the operator's money is at risk, so it contributes
    nothing to staked, settled-stake, or open-stake totals.  Counting it made
    a converted $150 credit look like $150 of bankroll spent and its profit
    like a loss.
    """
    return 0 if str(leg["stake_kind"]) == "bonus" else int(leg["stake_cents"])


def _slip_payload(row: sqlite3.Row, legs: Sequence[sqlite3.Row]) -> dict[str, Any]:
    leg_payloads = [_leg_payload(leg) for leg in legs]
    staked = sum(_cash_stake(leg) for leg in legs)
    settled = [leg for leg in legs if str(leg["status"]) in SETTLED_STATUSES]
    # A settled leg whose return was never entered (an un-priced cashout) is not
    # counted as returning zero — that would print a loss the operator did not
    # take.  It is excluded from the realized figures and named as unpriced.
    priced = [leg for leg in settled if leg["returned_cents"] is not None]
    returned = sum(int(leg["returned_cents"]) for leg in priced)
    settled_stake = sum(_cash_stake(leg) for leg in priced)
    open_stake = sum(
        _cash_stake(leg) for leg in legs if str(leg["status"]) == "pending"
    )
    return {
        "id": int(row["id"]),
        "kind": str(row["kind"]),
        "placed_at": row["placed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sport": row["sport"],
        "league": row["league"],
        "event_key": row["event_key"],
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "commence_time": row["commence_time"],
        "market": row["market"],
        "period": row["period"],
        "side": row["side"],
        "line": row["line"],
        "margin_pct": row["margin_pct"],
        "expected_profit": _dollars(row["expected_profit_cents"]),
        "source_run_id": row["source_run_id"],
        "promo_source": row["promo_source"],
        "promo_offer_id": row["promo_offer_id"],
        "note": row["note"],
        "legs": leg_payloads,
        "stake": _dollars(staked),
        "open_stake": _dollars(open_stake),
        "settled_stake": _dollars(settled_stake),
        "returned": _dollars(returned) if priced else None,
        "profit": _dollars(returned - settled_stake) if priced else None,
        "status": _slip_status(legs),
        "unpriced_legs": len(settled) - len(priced),
    }


def _slip_status(legs: Sequence[sqlite3.Row]) -> str:
    """One word for the whole position, without inventing an outcome.

    ``partial`` exists because an arb settles one leg at a time and the honest
    answer in between is neither ``pending`` nor a result.
    """
    if not legs:
        return "empty"
    statuses = {str(leg["status"]) for leg in legs}
    if statuses == {"pending"}:
        return "pending"
    if "pending" in statuses:
        return "partial"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "settled"


def _leg_payload(row: sqlite3.Row) -> dict[str, Any]:
    stake = int(row["stake_cents"])
    cash_stake = _cash_stake(row)
    returned = row["returned_cents"]
    return {
        "id": int(row["id"]),
        "slip_id": int(row["slip_id"]),
        "position": int(row["position"]),
        "book": row["book"],
        "selection": row["selection"],
        "line": row["line"],
        "american_odds": row["american_odds"],
        "decimal_odds": row["decimal_odds"],
        "stake": _dollars(stake),
        "to_return": _dollars(int(row["to_return_cents"])),
        "stake_kind": str(row["stake_kind"]),
        "status": str(row["status"]),
        "returned": _dollars(returned),
        # Profit is measured against the *cash* put down: a winning bonus leg's
        # whole return is profit, because nothing of the operator's was staked.
        "profit": None if returned is None else _dollars(int(returned) - cash_stake),
        "settled_at": row["settled_at"],
        "link_url": row["link_url"],
        "note": row["note"],
    }


def summarize(slips: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Bankroll arithmetic over already-rendered slips.

    Computed from the payloads rather than a second set of SQL aggregates so the
    per-row numbers on screen and the totals above them cannot disagree — the
    one failure mode of a ledger that nobody can debug from the page.
    """
    staked_cents = 0
    settled_stake_cents = 0
    returned_cents = 0
    open_cents = 0
    pending_legs = 0
    settled_legs = 0
    unpriced = 0
    record = {"won": 0, "lost": 0, "push": 0, "void": 0, "cashout": 0}
    by_book: dict[str, dict[str, Any]] = {}
    slip_count = 0

    for slip in slips:
        slip_count += 1
        for leg in slip.get("legs") or ():
            stake_cents = _cents(leg.get("stake"), label="stake") or 0
            # Credit is not bankroll: a bonus leg's face stake is shown on the
            # leg but claims none of the operator's money.
            if str(leg.get("stake_kind") or "cash") == "bonus":
                stake_cents = 0
            staked_cents += stake_cents
            status = str(leg.get("status") or "pending")
            book = str(leg.get("book") or "—")
            bucket = by_book.setdefault(
                book,
                {"book": book, "bets": 0, "staked": 0.0, "returned": 0.0,
                 "profit": 0.0, "open": 0.0, "pending": 0},
            )
            bucket["bets"] += 1
            book_stake = stake_cents
            if status == "pending":
                pending_legs += 1
                open_cents += stake_cents
                bucket["open"] += book_stake / 100.0
                bucket["pending"] += 1
            else:
                settled_legs += 1
                if status in record:
                    record[status] += 1
                back = leg.get("returned")
                if back is None:
                    unpriced += 1
                else:
                    back_cents = _cents(back, label="returned") or 0
                    settled_stake_cents += stake_cents
                    returned_cents += back_cents
                    bucket["returned"] += back_cents / 100.0
                    bucket["profit"] += (back_cents - stake_cents) / 100.0
            bucket["staked"] += book_stake / 100.0

    profit_cents = returned_cents - settled_stake_cents
    return {
        "slip_count": slip_count,
        "leg_count": pending_legs + settled_legs,
        "pending_legs": pending_legs,
        "settled_legs": settled_legs,
        "unpriced_legs": unpriced,
        "staked": _dollars(staked_cents),
        "settled_stake": _dollars(settled_stake_cents),
        "returned": _dollars(returned_cents),
        "profit": _dollars(profit_cents),
        "open_stake": _dollars(open_cents),
        "roi_pct": (
            round(profit_cents / settled_stake_cents * 100.0, 2)
            if settled_stake_cents
            else None
        ),
        "record": record,
        "by_book": sorted(
            (
                {**bucket,
                 "staked": round(bucket["staked"], 2),
                 "returned": round(bucket["returned"], 2),
                 "profit": round(bucket["profit"], 2),
                 "open": round(bucket["open"], 2)}
                for bucket in by_book.values()
            ),
            key=lambda entry: (-entry["bets"], entry["book"]),
        ),
    }


def empty_payload() -> dict[str, Any]:
    """What the page embeds when no ledger file exists yet."""
    return {"slips": [], "summary": summarize(()), "statuses": list(STATUSES)}


__all__ = [
    "KINDS",
    "SETTLED_STATUSES",
    "STATUSES",
    "BetLog",
    "BetLogError",
    "LegInput",
    "SlipInput",
    "american_from_decimal",
    "decimal_from_american",
    "default_return_cents",
    "empty_payload",
    "slip_from_payload",
    "summarize",
]
