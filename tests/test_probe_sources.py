from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

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
    monkeypatch.setattr(
        probe_sources,
        "probe_registered",
        lambda candidate, state: ("OK", "48 MLB quotes"),
    )
    assert probe_sources.main(
        ["--state", "PA", "--template-only", "--only", "draftkings"]
    ) == 0
    assert "UNVALIDATED" in capsys.readouterr().out
    assert not probe_sources.settings.PROBE_CACHE_PATH.exists()


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


def test_collector_auto_state_relaunches_with_detected_state(
    monkeypatch, tmp_path, capsys,
) -> None:
    import src.collector as collector
    import src.egress as egress
    import src.sources.transport as transport

    detection = detection_from_payload(
        {"ip": "203.0.113.12", "region_code": "PA"},
        detected_at=datetime.now(UTC),
    )

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(collector.settings, "EGRESS_STATE_PATH", tmp_path / "egress.json")
    monkeypatch.setattr(transport, "build_default_client", lambda **kwargs: Client())
    monkeypatch.setattr(
        egress,
        "detect_egress",
        lambda client, urls: (detection, "https://second.invalid/"),
    )
    launched = {}

    def run(command, *, env, check):
        launched.update(command=command, env=env, check=check)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(collector.subprocess, "run", run)
    assert collector.main(
        ["collect", "--auto-state", "--source", "draftkings"]
    ) == 7
    assert launched["command"][-3:] == [
        "collect", "--source", "draftkings",
    ]
    assert launched["env"]["ODDS_STATE"] == "PA"
    assert launched["check"] is False
    assert "203.0.113.12" not in collector.settings.EGRESS_STATE_PATH.read_text()
    assert "detected PA" in capsys.readouterr().out


def test_collector_auto_state_is_live_collection_only(monkeypatch, capsys) -> None:
    import src.collector as collector
    import src.sources.transport as transport

    monkeypatch.setattr(
        transport,
        "build_default_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )
    assert collector.main(["runs", "--auto-state"]) == 2
    assert "only valid for live collection" in capsys.readouterr().err
