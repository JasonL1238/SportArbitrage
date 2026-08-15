from __future__ import annotations

from datetime import UTC, datetime
from src.egress import detect_egress, detection_from_payload, save_detection


def _configure_paths(monkeypatch, tmp_path):
    from scripts import probe_sources

    monkeypatch.setattr(
        probe_sources.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json"
    )
    monkeypatch.setattr(
        probe_sources.settings, "PROBE_CACHE_PATH", tmp_path / "probes.sqlite3"
    )
    return probe_sources


def test_template_only_never_requires_egress_or_writes_cache(
    monkeypatch, tmp_path, capsys,
) -> None:
    probe_sources = _configure_paths(monkeypatch, tmp_path)
    proxy_states = []
    monkeypatch.setattr(
        probe_sources,
        "proxy_url",
        lambda state=None: proxy_states.append(state) or None,
    )
    monkeypatch.setattr(
        probe_sources,
        "probe_registered",
        lambda candidate, state: ("OK", "48 MLB quotes"),
    )
    monkeypatch.setattr(
        probe_sources,
        "probe",
        lambda candidate, state, verbose, plain: ("OK", "research candidate"),
    )
    assert probe_sources.main(
        ["--state", "PA", "--template-only", "--only", "draftkings"]
    ) == 0
    assert "UNVALIDATED" in capsys.readouterr().out
    assert proxy_states == ["PA"]
    assert not probe_sources.settings.PROBE_CACHE_PATH.exists()


def test_plain_research_probe_uses_requested_state_proxy(monkeypatch) -> None:
    from scripts import probe_sources

    seen = {}

    class Response:
        status_code = 200
        text = '{"events": []}'
        headers = {"content-type": "application/json"}

    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(
        probe_sources,
        "proxy_url",
        lambda state=None: f"http://{state.lower()}.proxy.invalid:8080",
    )
    monkeypatch.setattr(probe_sources.httpx, "get", fake_get)
    candidate = probe_sources.Candidate(
        family="research",
        name="example",
        url="https://example.invalid/odds",
    )
    verdict, _detail = probe_sources.probe(
        candidate,
        state="PA",
        verbose=False,
        plain=True,
    )
    assert verdict == "OK"
    assert seen["proxy"] == "http://pa.proxy.invalid:8080"


def test_state_mismatch_refuses_before_live_probe(monkeypatch, tmp_path, capsys) -> None:
    probe_sources = _configure_paths(monkeypatch, tmp_path)
    detection = detection_from_payload(
        {"ip": "203.0.113.8", "region_code": "IL"},
        detected_at=datetime.now(UTC),
    )
    save_detection(probe_sources.settings.EGRESS_STATE_PATH, detection)

    def should_not_probe(*args, **kwargs):
        raise AssertionError("network probe ran after state mismatch")

    monkeypatch.setattr(probe_sources, "probe_registered", should_not_probe)
    assert probe_sources.main(["--state", "PA", "--only", "draftkings"]) == 2
    assert "refusing validation" in capsys.readouterr().err


def test_the_strict_lookup_really_raises_both_refusals() -> None:
    """The contract the test below mocks, pinned against the real function.

    Monkeypatching ``descriptor_for_state`` to throw proves the ``except``
    clause catches what it is handed; it cannot prove the real lookup raises
    ``RuntimeError`` for a cross-state route, which is the claim the ``except``
    was widened for. Without this, that claim rests on a docstring.

    The cross-state case is reached through a substitute route because no shipped
    jurisdiction mis-tags one — that is the invariant, not an oversight, and this
    test would be dishonest if it implied otherwise.
    """
    from dataclasses import replace as _replace

    import pytest as _pytest

    import src.jurisdictions as _j
    from src.jurisdictions import JURISDICTIONS, RouteStatus, jurisdiction
    from src.sources.registry import descriptor_for_state

    # 1. No licensed route in this state at all.
    assert jurisdiction("PA").routes["hardrock"].status is RouteStatus.UNAVAILABLE
    with _pytest.raises(KeyError, match="no live PA route"):
        descriptor_for_state("PA", "hardrock")

    # 2. A route that exists but is tagged for another state.
    pa = JURISDICTIONS["PA"]
    good = pa.routes["fanduel"]
    assert good.routed_state == "PA", "fixture assumption changed"
    mistagged = _replace(good, routed_state="IL")
    routes = {**pa.routes, "fanduel": mistagged}
    patched = _replace(pa, routes=routes)
    original = _j.JURISDICTIONS
    _j.JURISDICTIONS = {**original, "PA": patched}
    try:
        with _pytest.raises(RuntimeError, match="^fanduel route is tagged IL"):
            descriptor_for_state("PA", "fanduel")
        # 3. And the refusal is about the book asked for. Resolving the whole
        # retail set meant this raised ``fanduel route is tagged IL`` while
        # ``caesars`` was the book being probed — and ``probe_sources.py`` caches
        # that verdict under the *probed* key, so one bad entry would file every
        # retail book in the state as unlicensed.
        other = descriptor_for_state("PA", "caesars")
        assert other.key == "caesars"
        assert other.route_state == "PA"
    finally:
        _j.JURISDICTIONS = original


def test_a_licensing_refusal_is_a_row_not_a_traceback(monkeypatch) -> None:
    """``descriptor_for_state`` raises two types, and one escaped.

    The strict lookup raises ``KeyError`` when a state declares no route and
    ``RuntimeError`` when the only route is tagged for another state — and the
    second is the case this guard was added for.  ``except KeyError`` let it out,
    and the call sits outside ``probe_registered``'s ``try`` with only a
    ``finally`` above it, so a defence-in-depth check would have ended the whole
    probe pass in a traceback.
    """
    from scripts import probe_sources

    for error in (
        KeyError("'hardrock' has no live PA route"),
        RuntimeError("betrivers_kambi route is tagged IL, not requested state PA"),
    ):
        # Patched on the registry, because ``probe_registered`` imports the name
        # inside the function body.
        monkeypatch.setattr(
            "src.sources.registry.descriptor_for_state",
            lambda state, key, _e=error: (_ for _ in ()).throw(_e),
        )
        candidate = probe_sources.Candidate(
            family="registered",
            name="BetRivers PA",
            url="",
            source_key="betrivers_kambi",
        )
        verdict, detail = probe_sources.probe_registered(candidate, state="PA")
        assert verdict == "UNLICENSED", (verdict, detail)
        # And it is cached as a licensing fact, not as a broken parser.
        assert probe_sources._cache_status(verdict, detail) is (
            probe_sources.ProbeStatus.UNLICENSED
        )


def test_fresh_ok_is_skipped_and_force_reprobes(monkeypatch, tmp_path, capsys) -> None:
    probe_sources = _configure_paths(monkeypatch, tmp_path)
    detection = detection_from_payload(
        {"ip": "203.0.113.9", "region_code": "PA"},
        detected_at=datetime.now(UTC),
    )
    save_detection(probe_sources.settings.EGRESS_STATE_PATH, detection)
    calls = []

    def fake(candidate, state):
        calls.append((candidate.source_key, state))
        return "OK", "48 MLB quotes"

    monkeypatch.setattr(probe_sources, "probe_registered", fake)
    args = ["--state", "PA", "--only", "draftkings"]
    assert probe_sources.main(args) == 0
    assert probe_sources.main(args) == 0
    assert calls == [("draftkings", "PA")]
    assert "CACHED OK" in capsys.readouterr().out
    assert probe_sources.main([*args, "--force"]) == 0
    assert calls == [("draftkings", "PA"), ("draftkings", "PA")]


def test_detect_command_uses_injected_lookup_and_stores_no_raw_ip(
    monkeypatch, tmp_path, capsys,
) -> None:
    from scripts import detect_state

    raw_ip = "203.0.113.10"

    class Response:
        status_code = 200
        text = f'{{"ip": "{raw_ip}", "region_code": "PA"}}'

    class Client:
        def get(self, url, headers):
            assert url == "https://lookup.invalid/json"
            assert headers["Accept"] == "application/json"
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(detect_state.settings, "STATE", "PA")
    monkeypatch.setattr(
        detect_state.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json"
    )
    monkeypatch.setattr(detect_state, "build_default_client", lambda **kwargs: Client())
    assert detect_state.main(["--url", "https://lookup.invalid/json"]) == 0
    assert raw_ip not in detect_state.settings.EGRESS_STATE_PATH.read_text()
    assert "detected_egress=PA" in capsys.readouterr().out


def test_detection_falls_back_after_provider_failure() -> None:
    class Response:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

    class Client:
        def get(self, url, headers):
            assert headers["Accept"] == "application/json"
            if url == "https://first.invalid/":
                return Response(403, "must not be included in an error")
            return Response(200, '{"ip": "203.0.113.11", "region_code": "IL"}')

    detection, provider = detect_egress(
        Client(),
        urls=("https://first.invalid/", "https://second.invalid/"),
    )
    assert detection.state == "IL"
    assert provider == "https://second.invalid/"


def test_batch_state_detection_uses_requested_states_with_reduced_record(
    monkeypatch, tmp_path,
) -> None:
    """The record persisted alongside the selection still carries no raw IP.

    The state assertion here changed with the selection rule: a requested state
    is now the whole answer rather than an addition after the detected one, so
    detecting PA while ``["IL"]`` is asked for collects Illinois alone.  What the
    test is really guarding — that detection writes a fingerprint and never the
    address — is unchanged, and is checked on the same path that now also records
    a reading whose state collection would refuse.
    """
    import src.state_selection as state_selection

    monkeypatch.delenv("ODDS_STATE", raising=False)
    detection = detection_from_payload(
        {"ip": "203.0.113.12", "region_code": "PA"},
        detected_at=datetime.now(UTC),
    )

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(
        state_selection.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json"
    )
    monkeypatch.setattr(
        state_selection,
        "detect_egress",
        lambda client, urls: (detection, "https://second.invalid/"),
    )
    selection = state_selection.detect_and_select(
        ["IL"], client_factory=lambda **kwargs: Client()
    )
    assert selection.states == ("IL",)
    assert selection.detected_state == "PA"
    assert "203.0.113.12" not in state_selection.settings.EGRESS_STATE_PATH.read_text()


def test_retired_auto_state_flag_is_rejected_by_every_command() -> None:
    """``--auto-state`` is gone, and gone loudly rather than silently ignored.

    It survived as a no-op on ``collect`` after live collection started detecting
    state on its own.  A flag that parses and does nothing is worse than one that
    does not parse: a caller still passing it believes it is asking for
    something.  ``runs`` never accepted it; now neither does ``collect``, so the
    caller finds out at argument-parse time.
    """
    import pytest

    import src.collector as collector

    for argv in (["runs", "--auto-state"], ["collect", "--auto-state"]):
        with pytest.raises(SystemExit) as caught:
            collector.main(argv)
        assert caught.value.code == 2


def test_a_route_with_no_probe_url_is_loud_rather_than_skipped(monkeypatch) -> None:
    """An unbranched route key used to vanish from the probe with no error.

    ``state_candidates`` hand-duplicates the retail key set as an ``if/elif``
    chain, and its tail was ``continue``.  A route added to a jurisdiction
    without a matching branch produced no table row, no count, and no cache
    entry — and the only other loop that prints route keys filters to
    ``UNAVAILABLE``, so nothing anywhere reported it.  The operator would read
    "6 of 6 candidate(s) answered" for a state holding seven routes.

    The invented key has to be one that will never earn a branch.  This used
    ``thescore``, which earned one the day that adapter was registered
    (2026-08-13) — at which point the test stopped exercising the tail it is
    about and failed on the borrowed config instead.  Third time that name has
    been a stand-in for "a key nothing claims" and stopped being one.
    """
    import pytest

    from scripts import probe_sources
    from src.jurisdictions import RouteStatus

    configured = probe_sources.jurisdiction("IL")
    borrowed = configured.routes["hardrock"]
    invented = type(borrowed)(
        **{
            **{
                field: getattr(borrowed, field)
                for field in borrowed.__dataclass_fields__
            },
            "status": RouteStatus.TEMPLATE,
        }
    )
    patched = {**configured.routes, "no_such_book": invented}
    monkeypatch.setattr(
        probe_sources,
        "jurisdiction",
        lambda state: type(configured)(
            **{
                **{
                    field: getattr(configured, field)
                    for field in configured.__dataclass_fields__
                },
                "routes": patched,
            }
        ),
    )

    with pytest.raises(SystemExit) as caught:
        probe_sources.state_candidates("IL")
    assert "no_such_book" in str(caught.value)
    assert "no probe URL" in str(caught.value)
