from __future__ import annotations

import hashlib
import importlib
import sqlite3
from datetime import UTC, datetime, timedelta

from src.egress import detection_from_payload, load_detection, save_detection
from src.jurisdictions import JURISDICTIONS, RouteStatus, jurisdiction, route_warnings
from src.probe_cache import ProbeCache, ProbeStatus
from src.promos.registry import promo_sources_for_state
from src.promos.store import PromoStore
from src.sources.registry import (
    REPUBLISHED_SOURCE_KEYS,
    sources_for_state,
    state_sources_for_state,
    view_only_for_state,
)
from src.store import Store
from src.validation import ValidationReport


def _by_key(entries):
    return {entry.key: entry for entry in entries}


def test_il_and_pa_retail_routes_keep_stable_source_keys() -> None:
    il = _by_key(sources_for_state("IL"))
    pa = _by_key(sources_for_state("pa"))
    assert il.keys() == pa.keys(), "unavailable books remain registered for replay"
    assert il["fanduel"].config["state"] == "il"
    assert pa["fanduel"].config["state"] == "pa"
    assert il["betrivers_kambi"].config == {
        "operator": "rsiusil", "market": "US-IL", "lang": "en_US",
    }
    assert pa["betrivers_kambi"].config == {
        "operator": "rsiuspa", "market": "US-PA", "lang": "en_US",
    }
    assert "US-IL-SB" in il["draftkings"].config["base_url"]
    assert "US-PA-SB" in pa["draftkings"].config["base_url"]
    assert sum(entry.key == "betrivers_kambi" for entry in pa.values()) == 1


def test_pa_hardrock_is_unavailable_not_an_invented_pa_segment() -> None:
    route = jurisdiction("PA").routes["hardrock"]
    assert route.status is RouteStatus.UNAVAILABLE
    assert route.config["segment"] == "nj"
    assert any("no licensed route" in warning for warning in route_warnings("PA"))
    assert {"hardrock", "vi_hardrock", "an_hardrock"} <= view_only_for_state("PA")
    assert "hardrock" not in view_only_for_state("IL")
    assert REPUBLISHED_SOURCE_KEYS <= view_only_for_state("IL")


def test_promos_use_one_active_state_region_and_landing() -> None:
    pa = _by_key(promo_sources_for_state("PA"))
    assert pa["fanduel"].config["regions"] == ("PA",)
    targets = pa["betrivers_kambi"].config["targets"]
    assert len(targets) == 1
    assert targets[0].url == "https://pa.betrivers.com/"
    assert targets[0].region == "PA"


def test_settings_normalize_state_and_refuse_unknown(monkeypatch) -> None:
    import src.settings as settings

    monkeypatch.setenv("ODDS_STATE", "pa")
    settings = importlib.reload(settings)
    assert settings.STATE == "PA"
    assert settings.BAD_SETTINGS == []

    monkeypatch.setenv("ODDS_STATE", "xx")
    settings = importlib.reload(settings)
    assert settings.STATE == "IL"
    assert any("ODDS_STATE" in line and "unknown" in line for line in settings.BAD_SETTINGS)
    assert settings.refuse_bad_settings() == 2

    monkeypatch.delenv("ODDS_STATE")
    importlib.reload(settings)


def test_run_jurisdiction_is_stored_and_legacy_rows_stay_blank(tmp_path) -> None:
    with Store(tmp_path / "odds.sqlite3") as store:
        legacy = store.start_run(datetime.now(UTC))
        pa = store.start_run(
            datetime.now(UTC), jurisdiction="pa", batch_id="batch-1", route_scope="state"
        )
        assert store.run_row(legacy)["jurisdiction"] == ""
        assert store.run_row(pa)["jurisdiction"] == "PA"
        assert store.run_row(pa)["batch_id"] == "batch-1"
        store.finish_run(
            pa,
            finished_at=datetime.now(UTC),
            report=ValidationReport(),
        )
        assert store.latest_run_id(jurisdiction="PA") == pa
        assert store.latest_run_id(jurisdiction="NJ") is None

    promo_store = PromoStore(tmp_path / "promos.sqlite3")
    try:
        promo_store.start_run(jurisdiction="pa", batch_id="batch-1")
        assert promo_store.list_runs(limit=1)[0]["jurisdiction"] == "PA"
        assert promo_store.list_runs(limit=1)[0]["batch_id"] == "batch-1"
    finally:
        promo_store.close()


def test_existing_promo_database_gains_jurisdiction_in_place(tmp_path) -> None:
    path = tmp_path / "promos.sqlite3"
    store = PromoStore(path)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE promo_runs DROP COLUMN jurisdiction")
    migrated = PromoStore(path)
    try:
        run_id = migrated.start_run(jurisdiction="PA")
        row = migrated.list_runs(limit=1)[0]
        assert row["id"] == run_id
        assert row["jurisdiction"] == "PA"
    finally:
        migrated.close()


def test_egress_record_hashes_ip_and_never_persists_it(tmp_path) -> None:
    raw_ip = "203.0.113.42"
    detected = detection_from_payload(
        {"ip": raw_ip, "region_code": "PA"},
        detected_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    path = tmp_path / "egress.json"
    save_detection(path, detected)
    text = path.read_text()
    assert raw_ip not in text
    assert detected.egress_fingerprint == hashlib.sha256(raw_ip.encode()).hexdigest()
    assert load_detection(path) == detected


def test_probe_cache_skips_only_fresh_ok_for_same_state_and_egress(tmp_path) -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    fingerprint = "a" * 64
    with ProbeCache(tmp_path / "probes.sqlite3") as cache:
        cache.record(
            "draftkings", "PA", fingerprint, ProbeStatus.OK,
            "48 quotes", probed_at=now,
        )
        assert cache.fresh_ok(
            "draftkings", "PA", fingerprint,
            now=now + timedelta(days=59),
        ) is not None
        assert cache.fresh_ok(
            "draftkings", "PA", fingerprint,
            now=now + timedelta(days=61),
        ) is None
        assert cache.fresh_ok("draftkings", "IL", fingerprint, now=now) is None
        cache.record(
            "caesars", "PA", fingerprint, ProbeStatus.BLOCKED,
            "403", probed_at=now,
        )
        assert cache.fresh_ok("caesars", "PA", fingerprint, now=now) is None


def test_all_supported_collection_states_are_configured() -> None:
    assert set(JURISDICTIONS) == {"IL", "PA", "NJ", "DC"}


def test_every_instantiated_retail_route_matches_its_requested_state() -> None:
    for state in JURISDICTIONS:
        configured = jurisdiction(state)
        for entry in state_sources_for_state(state):
            assert configured.routes[entry.key].routed_state == state
    assert "betrivers_kambi" not in _by_key(state_sources_for_state("DC"))
    assert "hardrock" not in _by_key(state_sources_for_state("DC"))
    il = _by_key(state_sources_for_state("IL"))
    assert "/locations/il/" in il["caesars"].config["base_url"]
    assert il["hardrock"].config["segment"] == "il"
    assert il["hardrock"].config["channel"] == "ILLINOIS_ONLINE"
    nj = _by_key(state_sources_for_state("NJ"))
    assert "/locations/nj/" in nj["caesars"].config["base_url"]
    assert nj["hardrock"].config["segment"] == "nj"
    assert nj["hardrock"].config["channel"] == "NEW_JERSEY_ONLINE"


def test_state_selection_includes_detected_first_and_deduplicates(monkeypatch) -> None:
    from src.state_selection import select_states

    monkeypatch.delenv("ODDS_STATE", raising=False)
    assert select_states("pa", ["NJ", "pa", "IL"]) == ("PA", "NJ", "IL")


def test_batch_fetches_global_sources_once_for_multiple_states(tmp_path, monkeypatch) -> None:
    import src.collector as collector
    from src.raw_store import RawResponse, RawStore

    calls: dict[str, int] = {}

    class FakeSource:
        def __init__(self, key: str):
            self.source_key = key
            self.leagues = ()

        def fetch_raw(self, *, tier=None):
            calls[self.source_key] = calls.get(self.source_key, 0) + 1
            return [RawResponse(
                source=self.source_key,
                endpoint="test",
                url="https://example.test",
                status_code=200,
                body="{}",
                fetched_at=datetime.now(UTC),
            )]

        def close(self):
            pass

    def build(keys=None, *, state=None, route_scope="all", **kwargs):
        if route_scope == "global":
            return [FakeSource("pinnacle")]
        return [FakeSource(f"retail-{state}")]

    seen = []

    class Result:
        ok = True
        quotes = []

    def collect(sources, **kwargs):
        for source in sources:
            collector._fetch(source, kwargs["tier"])
        seen.append((kwargs["jurisdiction"], tuple(s.source_key for s in sources)))
        return Result()

    monkeypatch.setattr(collector, "build_sources", build)
    monkeypatch.setattr(collector, "collect_once", collect)
    batch = collector.collect_batch_once(
        ("IL", "PA"),
        detected_state="IL",
        raw_store=RawStore(tmp_path / "raw"),
        store=None,
    )
    assert batch.ok
    assert calls["pinnacle"] == 1
    assert [state for state, _ in seen] == ["GLOBAL", "IL", "PA"]


def test_report_source_metadata_resolves_stored_state_not_hardcoded_il() -> None:
    from src.report import _source_entry

    assert _source_entry("fanduel", state="PA")["host"] == (
        "sbapi.pa.sportsbook.fanduel.com"
    )
    hardrock = _source_entry("hardrock", state="PA")
    assert hardrock["route_status"] == "unavailable"
    assert hardrock["view_only"] is True
