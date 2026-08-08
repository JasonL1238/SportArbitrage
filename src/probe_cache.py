"""Separate SQLite cache for live jurisdiction probe results."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path


class ProbeStatus(StrEnum):
    OK = "ok"
    GEO_RESTRICTED = "geo_restricted"
    BLOCKED = "blocked"
    PARSE_FAIL = "parse_fail"
    #: The registry refused to hand out a route for this state at all — no
    #: licensed route exists, or the only one is tagged for another state.  Its own
    #: value because it is a fact about the licence, not about the response: the
    #: site was never asked, so filing it as ``PARSE_FAIL`` blames an adapter that
    #: never ran and ``BLOCKED`` blames a venue that never answered.
    UNLICENSED = "unlicensed"
    UNTESTED = "untested"


@dataclass(frozen=True)
class ProbeRecord:
    source_key: str
    state: str
    egress_fingerprint: str
    status: ProbeStatus
    probed_at: str
    detail: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_result (
    source_key TEXT NOT NULL,
    state TEXT NOT NULL,
    egress_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    probed_at TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_key, state, egress_fingerprint)
);
"""


class ProbeCache:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ProbeCache":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get(self, source_key: str, state: str, fingerprint: str) -> ProbeRecord | None:
        row = self._conn.execute(
            """SELECT source_key, state, egress_fingerprint, status, probed_at, detail
                 FROM probe_result
                WHERE source_key = ? AND state = ? AND egress_fingerprint = ?""",
            (source_key, state.upper(), fingerprint),
        ).fetchone()
        if row is None:
            return None
        return ProbeRecord(
            source_key=row["source_key"],
            state=row["state"],
            egress_fingerprint=row["egress_fingerprint"],
            status=ProbeStatus(row["status"]),
            probed_at=row["probed_at"],
            detail=row["detail"],
        )

    def fresh_ok(
        self,
        source_key: str,
        state: str,
        fingerprint: str,
        *,
        now: datetime | None = None,
        ttl: timedelta = timedelta(days=60),
    ) -> ProbeRecord | None:
        record = self.get(source_key, state, fingerprint)
        if record is None or record.status is not ProbeStatus.OK:
            return None
        try:
            when = datetime.fromisoformat(record.probed_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        age = current.astimezone(UTC) - when.astimezone(UTC)
        return record if timedelta(0) <= age <= ttl else None

    def record(
        self,
        source_key: str,
        state: str,
        fingerprint: str,
        status: ProbeStatus,
        detail: str = "",
        *,
        probed_at: datetime | None = None,
    ) -> ProbeRecord:
        when = probed_at or datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        timestamp = when.astimezone(UTC).isoformat().replace("+00:00", "Z")
        concise = " ".join(detail.split())[:500]
        with self._conn:
            self._conn.execute(
                """INSERT INTO probe_result
                       (source_key, state, egress_fingerprint, status, probed_at, detail)
                     VALUES (?, ?, ?, ?, ?, ?)
                     ON CONFLICT(source_key, state, egress_fingerprint) DO UPDATE SET
                       status = excluded.status,
                       probed_at = excluded.probed_at,
                       detail = excluded.detail""",
                (
                    source_key,
                    state.upper(),
                    fingerprint,
                    status.value,
                    timestamp,
                    concise,
                ),
            )
        return ProbeRecord(
            source_key=source_key,
            state=state.upper(),
            egress_fingerprint=fingerprint,
            status=status,
            probed_at=timestamp,
            detail=concise,
        )


__all__ = ["ProbeCache", "ProbeRecord", "ProbeStatus"]
