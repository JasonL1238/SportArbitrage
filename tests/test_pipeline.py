"""Raw storage, event identity, persistence, and the collect/replay loop.

These exercise the pipeline end to end without touching the network: a fake
source replays the captured fixtures, so the collector, store, and replay
comparison are all tested against real payloads.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.collector import collect_once, replay_run
from src.events import build_event_key, resolve_doubleheaders, scheduling_date
from src.raw_store import RawResponse, RawStore
from src.schema import Market, Selection
from src.sources.base import ParseOutcome
from src.sources.guards import BlockedError
from src.store import Store
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


# ── event identity ───────────────────────────────────────────────────────────


def test_scheduling_date_uses_eastern_not_utc() -> None:
    """A 01:45Z first pitch is a 21:45 Eastern game the previous calendar day.
    Bucketing on UTC would file two books' views of it under different dates."""
    late = datetime(2026, 7, 29, 1, 45, tzinfo=UTC)
    assert scheduling_date(late).isoformat() == "2026-07-28"
    assert build_event_key("MIL", "SF", late) == "MIL@SF:2026-07-28"


def test_event_key_requires_timezone() -> None:
    with pytest.raises(ValueError):
        scheduling_date(datetime(2026, 7, 28, 12, 0))


def test_doubleheader_keys_are_ordered_by_start_time() -> None:
    base = "CLE@CIN:2026-07-28"
    early = datetime(2026, 7, 28, 17, 40, tzinfo=UTC)
    late = datetime(2026, 7, 28, 23, 10, tzinfo=UTC)

    resolved = resolve_doubleheaders({"g2": (base, late), "g1": (base, early)})
    assert resolved == {"g1": base, "g2": f"{base}#2"}

    # Another book listing the same pair, ids differing, must agree on which
    # game is which — that is what makes the cross-source join work.
    other = resolve_doubleheaders({"xx": (base, early + timedelta(minutes=1)),
                                  "yy": (base, late + timedelta(minutes=1))})
    assert other == {"xx": base, "yy": f"{base}#2"}


def test_single_game_keeps_the_plain_key() -> None:
    key = "ATL@NYM:2026-07-28"
    assert resolve_doubleheaders({"g": (key, datetime(2026, 7, 28, 23, 10, tzinfo=UTC))}) == {"g": key}


# ── store ────────────────────────────────────────────────────────────────────


def test_quotes_round_trip_through_sqlite(tmp_path: Path) -> None:
    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        original = [
            make_quote(source="bookA", selection=Selection.HOME),
            make_quote(source="bookA", selection=Selection.AWAY, decimal_odds=2.05,
                       american_odds=105),
            make_quote(source="bookA", market=Market.TOTAL_RUNS, selection=Selection.OVER,
                       line=8.5, decimal_odds=1.92, american_odds=-108),
        ]
        assert store.save_quotes(run_id, original) == 3
        assert store.load_quotes(run_id) == original


def test_duplicate_selection_is_rejected_at_the_storage_boundary(tmp_path: Path) -> None:
    import sqlite3

    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        quote = make_quote()
        store.save_quotes(run_id, [quote])
        with pytest.raises(sqlite3.IntegrityError):
            store.save_quotes(run_id, [quote])


def test_run_lifecycle_and_health_are_recorded(tmp_path: Path) -> None:
    from src.sources.base import SourceHealth
    from src.validation import validate

    with Store(tmp_path / "db.sqlite3") as store:
        run_id = store.start_run(datetime.now(UTC))
        store.save_health(
            run_id,
            SourceHealth(source_key="bookA", ok=False, checked_at=datetime.now(UTC),
                         error_kind="blocked", error_message="HTTP 403"),
        )
        report = validate([])
        store.finish_run(run_id, finished_at=datetime.now(UTC), report=report)

        assert store.latest_run_id() == run_id
        health = store.health_for_run(run_id)
        assert len(health) == 1
        assert health[0]["error_kind"] == "blocked"
        assert bool(store.run_summaries()[0]["ok"]) is False


# ── collector + replay ───────────────────────────────────────────────────────


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


class _FailingSource:
    source_key = "brokenbook"

    def fetch_raw(self):
        raise BlockedError("brokenbook:odds: HTTP 403")

    def parse(self, raws):
        return ParseOutcome()

    def close(self) -> None:
        pass


@pytest.fixture()
def fixture_sources(fanduel_raw, pinnacle_raw, kambi_raw):
    from src.sources.betrivers_kambi import BetRiversKambiAdapter
    from src.sources.fanduel import FanDuelAdapter
    from src.sources.pinnacle import PinnacleAdapter

    return [
        _FixtureSource("fanduel", fanduel_raw, FanDuelAdapter().parse),
        _FixtureSource("pinnacle", pinnacle_raw, PinnacleAdapter().parse),
        _FixtureSource("betrivers_kambi", kambi_raw, BetRiversKambiAdapter().parse),
    ]


def test_collect_once_stores_raw_quotes_and_health(tmp_path: Path, fixture_sources) -> None:
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        result = collect_once(fixture_sources, raw_store=raw_store, store=store)

        assert result.ok, [str(f) for f in result.report.errors]
        assert len(result.quotes) == 1654
        assert {h.source_key for h in result.health} == {
            "fanduel", "pinnacle", "betrivers_kambi",
        }
        assert all(h.ok for h in result.health)

        # Raw bytes landed on disk and are linked to the run.
        stored_raw = store.raw_paths(result.run_id)
        assert len(stored_raw) == 6
        assert all(path.exists() for _, path in stored_raw)
        assert len(store.load_quotes(result.run_id)) == 1654


def test_replay_reproduces_a_stored_run_exactly(tmp_path: Path, fixture_sources) -> None:
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        result = collect_once(fixture_sources, raw_store=raw_store, store=store)
        ok, problems = replay_run(result.run_id, store=store, raw_store=raw_store)
        assert ok, problems


def test_a_blocked_source_does_not_stop_the_run(tmp_path: Path, fixture_sources) -> None:
    """Unattended operation requires that one failing book not abort the pass."""
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        result = collect_once(
            [*fixture_sources, _FailingSource()], raw_store=raw_store, store=store
        )
        broken = next(h for h in result.health if h.source_key == "brokenbook")
        assert broken.ok is False
        assert broken.error_kind == "blocked"
        assert "403" in broken.error_message
        # The healthy books still collected, so the run still passes.
        assert result.ok, [str(f) for f in result.report.errors]
        assert len(result.quotes) == 1654


def test_a_single_source_run_fails_the_two_source_requirement(tmp_path: Path, fixture_sources) -> None:
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        result = collect_once(fixture_sources[:1], raw_store=raw_store, store=store)
        assert not result.ok
        assert "insufficient_sources" in {f.code for f in result.report.errors}


def test_health_reports_rejections_with_an_explanation(tmp_path: Path) -> None:
    """An unhealthy source must never be reported with an empty reason."""
    from src.sources.base import Rejection

    def parser(raws):
        outcome = ParseOutcome(quotes=[make_quote(source="halfbook")])
        outcome.rejections.append(
            Rejection(source="halfbook", reason="unknown_team", detail="'MLB Futures'")
        )
        return outcome

    raw_store = RawStore(tmp_path / "raw")
    source = _FixtureSource("halfbook", [make_raw("{}", source="halfbook")], parser)
    with Store(tmp_path / "db.sqlite3") as store:
        result = collect_once([source], raw_store=raw_store, store=store)
        health = result.health[0]
        assert health.ok is False
        assert health.error_kind == "rejections"
        assert "unknown_team" in health.error_message


def test_unchanged_payload_is_detected_across_runs(tmp_path: Path, fixture_sources) -> None:
    """A feed that returns byte-identical bytes must say so, so an operator can
    tell a quiet market from a stuck or cached one."""
    raw_store = RawStore(tmp_path / "raw")
    with Store(tmp_path / "db.sqlite3") as store:
        first = collect_once(fixture_sources, raw_store=raw_store, store=store)
        assert all(h.unchanged_payloads == 0 for h in first.health)

        second = collect_once(fixture_sources, raw_store=raw_store, store=store)
        for health in second.health:
            assert health.unchanged_payloads == health.request_count

        flagged = store.query(
            "SELECT count(*) n FROM raw_response WHERE run_id = ? AND unchanged = 1",
            (second.run_id,),
        )
        assert flagged[0]["n"] == 6


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
    them."""
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
        "body": '{"ok": true}',
    }))
    loaded = RawStore(tmp_path).read(path)
    assert loaded.json() == {"ok": True}
    assert loaded.headers == {}
