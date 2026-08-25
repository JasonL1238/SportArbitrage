from __future__ import annotations

import hashlib
import importlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.egress import detection_from_payload, load_detection, save_detection
from src.jurisdictions import JURISDICTIONS, RouteStatus, jurisdiction, route_warnings
from src.probe_cache import ProbeCache, ProbeStatus
from src.promos.registry import promo_sources_for_state
from src.promos.store import PromoStore
from src.sources.registry import (
    REPUBLISHED_SOURCE_KEYS,
    republished_sources_for_state,
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
    # ``content_base_url`` carries the state pin now that the v5 ``base_url``
    # route is retired and no longer a constructor parameter.
    assert "US-IL-SB" in il["draftkings"].config["content_base_url"]
    assert "US-PA-SB" in pa["draftkings"].config["content_base_url"]
    assert sum(entry.key == "betrivers_kambi" for entry in pa.values()) == 1


def test_republished_routes_use_the_requested_states_book_ids() -> None:
    il = _by_key(republished_sources_for_state("IL"))
    pa = _by_key(republished_sources_for_state("PA"))
    nj = _by_key(republished_sources_for_state("NJ"))

    assert il["an_fanduel"].config["book_id"] == 270
    assert pa["an_fanduel"].config["book_id"] == 255
    assert nj["an_fanduel"].config["book_id"] == 69
    assert pa["an_caesars"].config["book_id"] == 1906
    assert "an_hardrock" not in pa
    assert "an_bally" not in pa

    # Every Action Network descriptor in one state asks for the same complete
    # set, so a source cannot disappear merely because another descriptor used
    # a narrower expansion list.
    pa_fetch_sets = {
        entry.config["fetch_book_ids"]
        for entry in pa.values()
        if entry.key in jurisdiction("PA").republished
    }
    assert len(pa_fetch_sets) == 1


def test_pa_route_statuses_match_what_the_egress_actually_proved() -> None:
    """Each Pennsylvania route holds the status its evidence earned, exactly.

    VALIDATED was earned on 2026-08-08 by two parser-clean runs per book from a
    detected-PA egress: FanDuel runs 29/30, BetRivers runs 28/29, DraftKings
    runs 29/31, with runs 29/30/31 replaying PASS offline (28 replays FAIL on
    another book's since-fixed rows; BetRivers' own rows in it are drift-free).
    On 2026-08-23 the operator arrived in Pennsylvania and the same bar was
    met from a Philadelphia egress by DraftKings, theScore and bet365 (runs 32
    and 33; 33 replays PASS, 32 FAILs only on cloudbet's zero-line rows), then
    by BetMGM — once its PA access id was read off the PA site — and the newly
    registered betPARX (runs 33 and 34, both replaying PASS).
    Caesars failed on both days from both egresses — blocked at the CDN edge —
    so promoting it would assert evidence that does not exist, and demoting the
    validated routes would discard evidence that does.  Both directions are
    pinned: this table is read by
    ``_state_retail_descriptor`` and graded by ``src.coverage``, and a status
    drifting in either direction misstates what a PA run can be trusted to be.
    """
    routes = jurisdiction("PA").routes
    assert routes["fanduel"].status is RouteStatus.VALIDATED
    assert routes["betrivers_kambi"].status is RouteStatus.VALIDATED
    # DraftKings was demoted on 2026-08-14 because its 2026-08-08 evidence was
    # earned by the retired ``leagueSubcategory`` route, and re-promoted on
    # 2026-08-23 when the ``primaryMarkets`` replacement was exercised from a
    # Pennsylvania egress (runs 32 and 33, 951 quotes each, 0 rejections).
    # The assertion on the path is what keeps the status honest: VALIDATED is
    # a claim about *this* request, not about the one that used to be made.
    assert routes["draftkings"].status is RouteStatus.VALIDATED
    assert "primaryMarkets" in routes["draftkings"].config["content_base_url"]
    # theScore and bet365 were TEMPLATE from 2026-08-13/15 until the operator
    # arrived in Pennsylvania; both self-verify the licence the edge reports,
    # and both answered from Philadelphia on 2026-08-23 (runs 32 and 33).
    assert routes["thescore"].status is RouteStatus.VALIDATED
    assert routes["bet365"].status is RouteStatus.VALIDATED
    # BetMGM's PA access id came off the PA web app on 2026-08-23 (the
    # Illinois id is refused with HTTP 400); runs 33 and 34 were parser-clean
    # and replay PASS.  The id must stay PA's own — the shared IL constant is
    # the 2026-08-08 failure.
    assert routes["betmgm"].status is RouteStatus.VALIDATED
    assert routes["betmgm"].config["access_id"] != jurisdiction("IL").routes["betmgm"].config["access_id"]
    assert routes["caesars"].status is RouteStatus.TEMPLATE
    assert routes["hardrock"].status is RouteStatus.UNAVAILABLE
    # Registered 2026-08-23 off the PA web app's own Kambi tenant and
    # validated on runs 33 and 34 the same night.
    assert routes["betparx_kambi"].status is RouteStatus.VALIDATED
    assert routes["betparx_kambi"].config["operator"] == "parxuspa"


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


def _selection_env(monkeypatch, tmp_path):
    """Point detection at a scratch record and remove the compatibility state."""
    import src.state_selection as state_selection

    monkeypatch.delenv("ODDS_STATE", raising=False)
    monkeypatch.setattr(
        state_selection.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json"
    )

    class Client:
        def close(self):
            pass

    def refuse(client, urls):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    return state_selection, Client, refuse


def test_only_one_detection_provider_is_configured() -> None:
    """The ``ipwho.is`` fallback is gone, and its absence is asserted.

    ``detect_egress`` returns the FIRST provider that answers, so a second
    provider is a safety net only when both agree — and on 2026-08-14 these two
    did not: ipapi.co said IL and ipwho.is said CA for one unchanged egress that
    theScore's own region code independently confirmed as Illinois.  Because
    ipapi.co rate-limits, the disagreeing provider answered precisely when the
    accurate one had been spent, and collection refused with "detected CA".
    """
    from src.egress import DEFAULT_DETECTION_URLS

    assert DEFAULT_DETECTION_URLS == ("https://ipapi.co/json/",)


class _EchoClient:
    """Minimal stand-in for the transport: one canned body, one status."""

    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body
        self._status = status
        self.asked: list[str] = []

    def get(self, url, headers=None):
        self.asked.append(url)

        class _Response:
            status_code = self._status
            text = self._body

        return _Response()

    def close(self):
        pass


def test_continuity_confirms_a_stored_state_when_the_address_has_not_moved() -> None:
    from src.egress import confirm_unchanged_egress

    stored = detection_from_payload(
        {"ip": "203.0.113.7", "region_code": "IL"},
        detected_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    confirmed = confirm_unchanged_egress(
        _EchoClient("203.0.113.7\n"),
        stored,
        detected_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert confirmed is not None
    assert confirmed.state == "IL"
    assert confirmed.egress_fingerprint == stored.egress_fingerprint
    # Same address, same state, but freshly observed — that is the whole point.
    assert confirmed.detected_at != stored.detected_at


def test_continuity_refuses_a_different_address_rather_than_guessing() -> None:
    """The guard the strict path exists to give has to survive the fallback.

    Continuity may carry a verified state forward across an unchanged address.
    It may never assert one about an address nobody has checked — otherwise a
    machine that moved to another state would inherit Illinois and every route
    pinned to Illinois would be collected from the wrong licence.
    """
    from src.egress import confirm_unchanged_egress

    stored = detection_from_payload(
        {"ip": "203.0.113.7", "region_code": "IL"},
        detected_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    assert confirm_unchanged_egress(_EchoClient("198.51.100.9\n"), stored) is None


def test_continuity_refuses_a_body_that_is_not_an_address() -> None:
    """A challenge page is also "200 with a body".

    Hashing one yields a digest that matches nothing, which reads as a *changed*
    address — the safe direction, but for the wrong reason and with a misleading
    story. The shape check makes the provider's failure legible instead.
    """
    from src.egress import EgressDetectionError, fingerprint_now

    with pytest.raises(EgressDetectionError):
        fingerprint_now(_EchoClient("<!DOCTYPE html><title>Just a moment...</title>"))


def test_continuity_providers_are_never_the_detection_providers() -> None:
    """Identity and geolocation are asked of different services, deliberately.

    The recorded hazard for ``DEFAULT_DETECTION_URLS`` is a provider being wrong
    about the *state*. A continuity provider is never asked about the state, so
    it cannot express that failure — which is why a second one is safe here and
    is not safe there.
    """
    from src.egress import CONTINUITY_URLS, DEFAULT_DETECTION_URLS

    assert not set(CONTINUITY_URLS) & set(DEFAULT_DETECTION_URLS)
    assert CONTINUITY_URLS == ("https://checkip.amazonaws.com",)


def test_a_failed_lookup_falls_back_to_a_recent_record_and_says_so(
    monkeypatch, tmp_path
) -> None:
    state_selection, Client, refuse = _selection_env(monkeypatch, tmp_path)
    stored = detection_from_payload(
        {"ip": "203.0.113.7", "region_code": "IL"}, detected_at=datetime.now(UTC)
    )
    save_detection(state_selection.settings.EGRESS_STATE_PATH, stored)
    monkeypatch.setattr(state_selection, "detect_egress", refuse)

    selection = state_selection.detect_and_select(
        None, client_factory=lambda **kwargs: Client()
    )
    assert selection.states == ("IL",)
    assert selection.detected_state == "IL"
    assert "429" in selection.note and "IL" in selection.note
    # The stood-in record keeps its original timestamp — re-saving it would
    # claim the state was detected now, which is the one thing it was not.
    assert load_detection(state_selection.settings.EGRESS_STATE_PATH) == stored


def test_a_stale_record_does_not_decide_a_run_but_a_chosen_state_does(
    monkeypatch, tmp_path
) -> None:
    """Old enough to predate a move ⇒ not a substitute for asking.

    ``is_recent`` runs to a day, which comfortably covers a flight or a drive
    across a state line.  So a stale record refuses rather than guesses — and
    the refusal is answerable, because naming a state needs no lookup at all.
    """
    import pytest

    state_selection, Client, refuse = _selection_env(monkeypatch, tmp_path)
    stale = detection_from_payload(
        {"ip": "203.0.113.7", "region_code": "IL"},
        detected_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    save_detection(state_selection.settings.EGRESS_STATE_PATH, stale)
    monkeypatch.setattr(state_selection, "detect_egress", refuse)

    with pytest.raises(state_selection.StateSelectionError) as caught:
        state_selection.detect_and_select(None, client_factory=lambda **kwargs: Client())
    assert "detection is unavailable" in str(caught.value)

    chosen = state_selection.detect_and_select(
        ["PA"], client_factory=lambda **kwargs: Client()
    )
    assert chosen.states == ("PA",)
    assert chosen.detected is None
    assert chosen.detected_state == ""


def test_an_unsupported_fresh_reading_is_still_recorded(monkeypatch, tmp_path) -> None:
    """The dashboard's jurisdiction warning is what reads this file.

    A ``CA`` reading used to raise before ``save_detection`` ran, so the one
    surface able to tell the reader "this run's state disagrees with your
    connection" was left with nothing written down.  The reading is a fact about
    the machine; whether collection supports the state is a separate question.
    """
    state_selection, Client, _ = _selection_env(monkeypatch, tmp_path)
    reading = detection_from_payload({"ip": "203.0.113.9", "region_code": "CA"})
    monkeypatch.setattr(
        state_selection, "detect_egress", lambda client, urls: (reading, "provider")
    )

    selection = state_selection.detect_and_select(
        ["IL"], client_factory=lambda **kwargs: Client()
    )
    assert selection.states == ("IL",)
    assert selection.detected_state == "CA"
    assert load_detection(state_selection.settings.EGRESS_STATE_PATH) == reading


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


def test_state_selection_prefers_the_chosen_states_and_deduplicates(monkeypatch) -> None:
    """A chosen state wins outright; detection only fills an empty choice.

    This test used to assert ``("PA", "NJ", "IL")`` for the same inputs — the
    detected state forced to the front of states the operator had named.  That
    ordering was the visible half of a gate: ``select_states`` validated the
    detected state *before* reading the requested ones, so a detection answering
    an unsupported state refused the run no matter what was asked for, and there
    was no way to say "I am in Illinois" by hand.  A rate-limited HTTP lookup
    outranking the operator is backwards, so choice now comes first and
    detection is advisory.
    """
    from src.state_selection import select_states

    monkeypatch.delenv("ODDS_STATE", raising=False)
    assert select_states("pa", ["NJ", "pa", "IL"]) == ("NJ", "PA", "IL")
    # Nothing chosen: detection is exactly what fills the gap.
    assert select_states("pa") == ("PA",)
    # An unsupported detection is not an error when a state was chosen — this is
    # the case that produced "detected CA, but collection supports only …" and
    # could not be overridden.
    assert select_states("CA", ["IL"]) == ("IL",)


def test_state_selection_refuses_only_when_nothing_names_a_state(monkeypatch) -> None:
    """The refusal survives, narrowed to the case where there is nothing to collect."""
    import pytest

    from src.state_selection import StateSelectionError, select_states

    monkeypatch.delenv("ODDS_STATE", raising=False)
    with pytest.raises(StateSelectionError) as unsupported:
        select_states("CA")
    assert "detected CA" in str(unsupported.value)
    assert "--state" in str(unsupported.value)

    with pytest.raises(StateSelectionError) as blank:
        select_states("")
    assert "detection is unavailable" in str(blank.value)
    assert "--state" in str(blank.value)


def test_odds_state_stays_an_addition_rather_than_a_choice(monkeypatch) -> None:
    """``ODDS_STATE`` is a compatibility input and must not displace either input.

    Two failure modes bracket it: dropping the detected state (so a PA run
    collects only Illinois) and overriding an explicit choice.  It appends.
    """
    from src.state_selection import select_states

    monkeypatch.setenv("ODDS_STATE", "IL")
    assert select_states("PA") == ("PA", "IL")
    assert select_states("PA", ["NJ"]) == ("NJ", "IL")
    assert select_states("IL", ["IL"]) == ("IL",)


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


def test_a_failure_building_republishers_does_not_leak_the_retail_transports(
    monkeypatch, tmp_path,
) -> None:
    """``retail`` is built before the ``try``, so its ``finally`` cannot reach it.

    The republisher build sits between the two, and a failure there left every
    already-open retail transport unclosed for the rest of the process.
    """
    from src import collector
    from src.raw_store import RawStore

    closed: list[str] = []

    class FakeSource:
        def __init__(self, key: str) -> None:
            self.source_key = key
            self.leagues = ("MLB",)

        def close(self) -> None:
            closed.append(self.source_key)

    calls: list[str] = []

    def build(keys=None, *, state=None, route_scope="all", **kwargs):
        calls.append(route_scope)
        if route_scope == "global":
            return []
        if route_scope == "state_republished":
            raise RuntimeError("action network refused the whole state")
        return [FakeSource(f"retail-{state}")]

    monkeypatch.setattr(collector, "build_sources", build)
    with pytest.raises(RuntimeError, match="refused the whole state"):
        collector.collect_batch_once(
            ("PA",),
            detected_state="PA",
            raw_store=RawStore(tmp_path / "raw"),
            store=None,
        )
    assert closed == ["retail-PA"], (closed, calls)


def test_the_all_scope_really_means_all_three_groups() -> None:
    """No caller reaches this branch, which is why a wrong answer would be believed.

    Splitting the state-licensed republishers out of ``global_sources()`` narrowed
    ``route_scope="all"`` to the retail routes plus those twelve republishers, so
    it silently stopped returning every global venue — Pinnacle, the exchanges,
    the prediction markets. ``collect_batch_once`` asks for each scope by name and
    never noticed.
    """
    from src.collector import build_sources

    built = build_sources(None, leagues=("MLB",), state="PA", route_scope="all")
    try:
        keys = {source.source_key for source in built}
    finally:
        for source in built:
            source.close()

    assert "fanduel" in keys                      # exact-state retail
    assert "an_fanduel" in keys                   # state-licensed republisher
    assert {"pinnacle", "matchbook", "kalshi"} <= keys   # global venues
    # And no source is built twice under one key.
    assert len(keys) == len(built)


def test_asking_a_scope_for_a_source_it_does_not_build_says_so() -> None:
    """A scope mismatch must not be reported as a league problem.

    ``build_sources(["an_caesars"], route_scope="global")`` exited with "none of []
    can collect league(s) ['MLB', 'WNBA', ...]" — the requested key had already been
    filtered out by the scope, so the message blamed the leagues for it and sent the
    reader to look at the wrong thing. The Action Network mirrors are the ones this
    happens to: they look global, one host and no proxy, and their book id is a state
    licence.
    """
    import pytest

    from src.collector import build_sources

    with pytest.raises(SystemExit) as raised:
        build_sources(["an_caesars"], leagues=("MLB",), route_scope="global")
    message = str(raised.value)
    assert "an_caesars" in message
    assert "state_republished" in message, message
    assert "scope mismatch" in message, message
    assert "league" not in message.replace("not a league one", ""), message


def test_report_source_metadata_resolves_stored_state_not_hardcoded_il() -> None:
    from src.report import _source_entry

    assert _source_entry("fanduel", state="PA")["host"] == (
        "sbapi.pa.sportsbook.fanduel.com"
    )
    hardrock = _source_entry("hardrock", state="PA")
    assert hardrock["route_status"] == "unavailable"
    assert hardrock["view_only"] is True


class TestALiveLookupNeverFallsBackToAnotherState:
    """A probe that validates the wrong licence manufactures false evidence.

    ``sources_for_state`` keeps every key registered and fills the gaps with base
    descriptors, which is right for replay and wrong for a caller about to open a
    socket.  ``probe_sources.py`` and ``recon_sources.py`` were both taking the
    lax path while doing exactly that.

    Neither is known to have produced a false validation — each is protected by
    an earlier guard (``state_candidates`` skips ``UNAVAILABLE``; ``profile()``
    raises for an unlicensed book) — so these pin the *lookup*, which is the
    lock that does not depend on those guards holding.  Every test below fails
    on the pre-fix lookup; none of them exercises the two scripts, which is why
    the claim made here is about the lookup and not about them.
    """

    def test_the_lax_lookup_still_serves_replay(self) -> None:
        """Not a regression to fix — the stable registry must stay enumerable."""
        from src.sources.registry import replay_descriptor_for_state

        stale = replay_descriptor_for_state("DC", "betrivers_kambi")
        assert stale.config["market"] == "US-IL"
        assert stale.route_state is None

    def test_an_unlicensed_retail_book_is_refused_rather_than_defaulted(self) -> None:
        from src.sources.registry import descriptor_for_state

        with pytest.raises(KeyError, match="no live DC route"):
            descriptor_for_state("DC", "betrivers_kambi")
        with pytest.raises(KeyError, match="not a Pennsylvania online sportsbook"):
            descriptor_for_state("PA", "hardrock")

    def test_a_republisher_with_no_licence_here_is_refused(self) -> None:
        """Otherwise the registry's New Jersey defaults answer for every state."""
        from src.sources.registry import descriptor_for_state

        with pytest.raises(KeyError, match="republishes no IL licence"):
            descriptor_for_state("IL", "an_parx")
        with pytest.raises(KeyError, match="republishes no DC licence"):
            descriptor_for_state("DC", "an_fanduel")

    def test_a_licensed_route_is_built_with_this_state_pinned(self) -> None:
        from src.sources.registry import descriptor_for_state

        book = descriptor_for_state("PA", "betrivers_kambi")
        assert book.config["market"] == "US-PA"
        assert book.route_state == "PA"
        assert descriptor_for_state("PA", "an_parx").config["book_id"] == 74

    def test_state_neutral_venues_pass_through(self) -> None:
        """Pinnacle and the exchanges have no jurisdiction to get wrong."""
        from src.sources.registry import descriptor_for_state

        assert descriptor_for_state("PA", "pinnacle").key == "pinnacle"
        assert descriptor_for_state("DC", "kalshi").key == "kalshi"


def test_every_registry_name_other_modules_use_is_exported() -> None:
    """``__all__`` was a record of what somebody remembered, not of the surface.

    Renaming ``republished_sources_for_state`` dropped it from ``__all__`` and nothing
    noticed, because it is reached as ``registry.<name>`` rather than imported by
    name. ``STATE_LICENSED_REPUBLISHER_KEYS`` and ``BY_BASE_KEY`` were in the same
    position — used from four modules and two test files, absent from the export list.
    Derived from real usage so the next rename cannot quietly do it again.
    """
    import re
    from pathlib import Path

    from src.sources import registry

    root = Path(__file__).resolve().parent.parent
    used: set[str] = set()
    for path in (*(root / "src").rglob("*.py"), *(root / "scripts").glob("*.py")):
        if path.name == "registry.py" and path.parent.name == "sources":
            continue
        for name in re.findall(r"\bregistry\.([A-Za-z_][A-Za-z0-9_]*)", path.read_text()):
            if not name.startswith("_"):
                used.add(name)

    assert used, "the scan found no registry attribute access; fix the pattern"
    unexported = sorted(
        name for name in used
        if hasattr(registry, name) and name not in registry.__all__
    )
    assert not unexported, (
        f"other modules reach registry.{unexported} while __all__ omits them — "
        "export them or stop using them"
    )
    # And nothing exported has since been deleted or renamed away.
    missing = sorted(name for name in registry.__all__ if not hasattr(registry, name))
    assert not missing, f"__all__ names {missing}, which no longer exist"


def test_the_per_state_book_id_question_is_not_the_state_scoped_question() -> None:
    """Two things that read alike, and the name used to claim the wider one.

    ``files_per_state_book_id`` answers about **republishers**: is this feed asked for
    a different Action Network book id per state. Under its old name and docstring —
    ``files_state_book_ids``, "False means each publishes one number for the whole
    country" — it returned False for ``fanduel``, ``betmgm`` and ``betrivers_kambi``,
    the most state-scoped sources in the repository, every one of which carries a
    per-state host or tenant.

    The workaround is already in the tree: ``src.redundancy`` cannot use it as *the*
    locality predicate and writes ``primary in RETAIL_SOURCE_KEYS or is_local(primary)``
    instead. This pins both halves so the next caller reads the union off a test rather
    than rediscovering it.
    """
    from src.jurisdictions import files_per_state_book_id, jurisdiction
    from src.sources.registry import RETAIL_SOURCE_KEYS

    for key in ("an_fanduel", "an_betmgm", "an_thescore"):
        assert files_per_state_book_id(key), key
    for key in ("vi_fanduel", "vsin_circa", "an_bovada", "pinnacle"):
        assert not files_per_state_book_id(key), key

    # False, and every one of them is nonetheless pinned per state — by a route
    # rather than by a book id, which is the distinction the name now carries.
    for key in sorted(RETAIL_SOURCE_KEYS):
        assert not files_per_state_book_id(key), key
    routes = jurisdiction("PA").routes
    assert routes["fanduel"].host != jurisdiction("IL").routes["fanduel"].host


class TestAddingAStateReachesTheCommandLine:
    """A state exists once the jurisdiction table names it — everywhere.

    Adding one used to mean finding every place the four were spelled out by hand.
    The two command lines now read the table, so a new ``Jurisdiction`` is accepted
    by ``collect --state`` the moment it is declared; this fails if a second list
    reappears.  The dashboard's checkboxes are still written out in
    ``src/report_assets.py`` and are the one remaining site.
    """

    @pytest.mark.parametrize("module", ["src.collector", "src.promos.collector"])
    def test_the_command_line_offers_exactly_the_declared_jurisdictions(
        self, module: str, capsys
    ) -> None:
        import importlib

        entry = importlib.import_module(module)
        with pytest.raises(SystemExit):
            entry.main(["collect", "--state", "ZZ"])

        refusal = capsys.readouterr().err
        assert "invalid choice: 'ZZ'" in refusal, refusal
        for state in JURISDICTIONS:
            assert state in refusal, f"{state} missing from {refusal}"


class TestEveryWatchPassReconfirmsTheEgress:
    """Rule (b) holds per pass, not per process.

    ``_cmd_collect`` detects the state once, before ``while True`` — so a VPN
    drop at hour three kept stamping runs ``PA`` from wherever the traffic now
    exits.  ``_egress_still_matches`` runs before every pass after the first:
    an unchanged fingerprint refreshes ``egress_state.json``, a changed or
    unconfirmable one refuses the pass.
    """

    def _selection(self):
        from src.state_selection import StateSelection

        detected = detection_from_payload({"ip": "203.0.113.55", "region_code": "PA"})
        return StateSelection(detected, ("PA",), "test"), detected

    def test_an_unchanged_address_passes_and_restamps_the_record(
        self, tmp_path, monkeypatch
    ) -> None:
        import src.collector
        import src.egress
        import src.settings

        selection, detected = self._selection()
        monkeypatch.setattr(
            src.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json"
        )
        monkeypatch.setattr(
            src.egress, "fingerprint_now",
            lambda client, urls=None, **kw: detected.egress_fingerprint,
        )
        assert src.collector._egress_still_matches(selection) is True
        stored = load_detection(tmp_path / "egress.json")
        assert stored is not None and stored.state == "PA"
        assert stored.egress_fingerprint == detected.egress_fingerprint

    def test_a_moved_address_refuses_the_pass(self, tmp_path, monkeypatch) -> None:
        import src.collector
        import src.egress
        import src.settings

        selection, _ = self._selection()
        monkeypatch.setattr(
            src.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json"
        )
        monkeypatch.setattr(
            src.egress, "fingerprint_now",
            lambda client, urls=None, **kw: hashlib.sha256(b"198.51.100.7").hexdigest(),
        )
        assert src.collector._egress_still_matches(selection) is False
        assert load_detection(tmp_path / "egress.json") is None, (
            "a refused pass must not overwrite the stored detection"
        )

    def test_an_unconfirmable_address_refuses_rather_than_guesses(
        self, tmp_path, monkeypatch
    ) -> None:
        import src.collector
        import src.egress
        import src.settings

        selection, _ = self._selection()
        monkeypatch.setattr(
            src.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json"
        )

        def down(client, urls=None, **kw):
            raise RuntimeError("echo service unreachable")

        monkeypatch.setattr(src.egress, "fingerprint_now", down)
        assert src.collector._egress_still_matches(selection) is False

    def test_a_forced_state_with_no_detection_is_not_blocked(self) -> None:
        from src.state_selection import StateSelection

        import src.collector

        selection = StateSelection(None, ("PA",), "", note="operator's choice")
        assert src.collector._egress_still_matches(selection) is True


class TestWafFrontedVenuesAreNotReTouched:
    """Both recorded bet365 burns followed bursts that bought no new bytes."""

    def test_fresh_refusal_serves_blocked_within_ttl_and_expires(self, tmp_path) -> None:
        from datetime import timedelta

        cache = ProbeCache(tmp_path / "probe.sqlite3")
        try:
            when = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
            cache.record("bet365", "PA", "fp", ProbeStatus.BLOCKED,
                         "challenge", probed_at=when)
            hit = cache.fresh_refusal(
                "bet365", "PA", "fp", now=when + timedelta(minutes=30)
            )
            assert hit is not None and hit.status is ProbeStatus.BLOCKED
            assert cache.fresh_refusal(
                "bet365", "PA", "fp", now=when + timedelta(minutes=50)
            ) is None, "an expired refusal must be re-probed, not remembered"
            # OK and UNLICENSED are not refusals and must never be served here.
            cache.record("fanduel", "PA", "fp", ProbeStatus.OK, probed_at=when)
            assert cache.fresh_refusal("fanduel", "PA", "fp", now=when) is None
            cache.record("hardrock", "PA", "fp", ProbeStatus.UNLICENSED,
                         probed_at=when)
            assert cache.fresh_refusal("hardrock", "PA", "fp", now=when) is None
        finally:
            cache.close()

    def test_the_collector_cooldown_skips_a_recently_touched_venue(
        self, monkeypatch
    ) -> None:
        from src import settings
        from src.collector import _apply_waf_cooldown

        class FakeSource:
            def __init__(self, key):
                self.source_key = key
                self.closed = False

            def close(self):
                self.closed = True

        class FakeStore:
            def __init__(self, last):
                self._last = last  # keyed (source, jurisdiction)

            def last_fetch_at(self, source, *, jurisdiction):
                return self._last.get((source, jurisdiction))

        now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
        recent = now - timedelta(seconds=120)
        stale = now - timedelta(hours=2)
        bet365, fanduel = FakeSource("bet365"), FakeSource("fanduel")

        kept = _apply_waf_cooldown(
            [bet365, fanduel], FakeStore({("bet365", "PA"): recent}),
            jurisdiction="PA", now=now,
        )
        assert [s.source_key for s in kept] == ["fanduel"]
        assert bet365.closed, "a dropped source must not leak its transport"
        assert not fanduel.closed

        # The cooldown is per state, because the host is the licence: PA
        # touching pa.bet365.com says nothing about il.bet365.com's wall — a
        # source-only key let the first state of a multi-state batch starve
        # every later one forever.
        bet365_il = FakeSource("bet365")
        kept = _apply_waf_cooldown(
            [bet365_il], FakeStore({("bet365", "PA"): recent}),
            jurisdiction="IL", now=now,
        )
        assert kept == [bet365_il] and not bet365_il.closed

        # An old touch passes; a non-WAF venue is never checked at all.
        bet365b = FakeSource("bet365")
        kept = _apply_waf_cooldown(
            [bet365b, FakeSource("fanduel")],
            FakeStore({("bet365", "PA"): stale}), jurisdiction="PA", now=now,
        )
        assert [s.source_key for s in kept] == ["bet365", "fanduel"]

        # 0 disables the cooldown — the deliberate validation session's setting.
        monkeypatch.setattr(settings, "WAF_COOLDOWN_SECONDS", 0)
        bet365c = FakeSource("bet365")
        kept = _apply_waf_cooldown(
            [bet365c], FakeStore({("bet365", "PA"): recent}),
            jurisdiction="PA", now=now,
        )
        assert kept == [bet365c] and not bet365c.closed

    def test_a_storeless_collection_applies_no_cooldown(self) -> None:
        from src.collector import _apply_waf_cooldown

        class FakeSource:
            source_key = "bet365"

            def close(self):
                raise AssertionError("must not be closed")

        source = FakeSource()
        assert _apply_waf_cooldown([source], None, jurisdiction="PA") == [source]

    def test_a_pass_whose_every_source_is_cooling_skips_instead_of_dying(
        self, tmp_path, monkeypatch
    ) -> None:
        """SystemExit is a BaseException: the watch loop's ``except Exception``
        guard never sees it, so a cooldown that emptied the slate killed
        unattended operation on its second tick.  Such a pass is skipped."""
        import src.collector as collector
        from src.raw_store import RawStore
        from src.store import Store
        from src.validation import ValidationReport

        db = tmp_path / "db.sqlite3"
        with Store(db) as store:
            run = store.start_run(datetime.now(UTC), jurisdiction="PA")
            store.record_raw(
                run,
                __import__("src.raw_store", fromlist=["RawResponse"]).RawResponse(
                    source="bet365", endpoint="shell", url="u", status_code=200,
                    body="x", fetched_at=datetime.now(UTC),
                ),
                path=tmp_path / "raw.json",
            )
            store.finish_run(
                run, finished_at=datetime.now(UTC),
                report=ValidationReport(quote_count=0, event_count=0),
            )

        with Store(db) as store:
            batch = collector.collect_batch_once(
                ["PA"],
                detected_state="PA",
                raw_store=RawStore(tmp_path / "raw"),
                store=store,
                source_keys=["bet365"],
                alert=False,
            )
        # No SystemExit, no state run — the pass was skipped, not killed.
        assert batch.runs == ()
