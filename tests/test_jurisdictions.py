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
    another book's since-fixed rows; BetRivers' own rows in it are drift-free).  The two still-TEMPLATE routes
    failed on the same day from the same egress — BetMGM with HTTP 400 on the
    access id, Caesars blocked at the CDN edge — so promoting them would assert
    evidence that does not exist, and demoting the three would discard evidence
    that does.  Both directions are pinned: this table is read by
    ``_state_retail_descriptor`` and graded by ``src.coverage``, and a status
    drifting in either direction misstates what a PA run can be trusted to be.
    """
    routes = jurisdiction("PA").routes
    assert routes["fanduel"].status is RouteStatus.VALIDATED
    assert routes["betrivers_kambi"].status is RouteStatus.VALIDATED
    # DraftKings was demoted on 2026-08-14 and the demotion is the point: its
    # 2026-08-08 evidence was earned by the ``leagueSubcategory`` route, which
    # has since been retired and replaced with ``primaryMarkets``.  Keeping
    # VALIDATED would assert evidence for a request this code no longer sends.
    # The replacement carries PA's own US-PA-SB segment and was proven from an
    # Illinois egress, which is not the same claim — promote it only after a
    # Pennsylvania egress exercises it.
    assert routes["draftkings"].status is RouteStatus.TEMPLATE
    assert "primaryMarkets" in routes["draftkings"].config["content_base_url"]
    assert routes["betmgm"].status is RouteStatus.TEMPLATE
    assert routes["caesars"].status is RouteStatus.TEMPLATE
    assert routes["hardrock"].status is RouteStatus.UNAVAILABLE


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
