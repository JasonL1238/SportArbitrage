from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from src.sources import browser as browser_module
from src.sources.browser import PageObservation, WebSocketObservation, _playwright_proxy
from src.sources.research import (
    profile,
    safe_request_headers,
    sanitize_body,
    sanitize_url,
    safe_websocket_frame,
    write_observations,
)
from src.sources.transport import proxy_url


def test_exact_state_proxy_wins_without_exposing_it(monkeypatch) -> None:
    monkeypatch.setenv("ODDS_HTTP_PROXY", "http://global.invalid:8000")
    monkeypatch.setenv("ODDS_HTTP_PROXY_PA", "http://user:secret@pa.invalid:9000")
    assert proxy_url("PA") == "http://user:secret@pa.invalid:9000"
    assert proxy_url("IL") == "http://global.invalid:8000"


def test_authenticated_proxy_is_split_for_playwright() -> None:
    assert _playwright_proxy("http://user:p%40ss@pa.invalid:9000") == {
        "server": "http://pa.invalid:9000",
        "username": "user",
        "password": "p@ss",
    }


def test_research_sanitization_removes_credentials_and_ip() -> None:
    url = sanitize_url(
        "https://user:pass@example.test/odds?league=mlb&token=secret&api_key=key"
    )
    assert "user" not in url and "pass" not in url
    query = parse_qs(urlsplit(url).query)
    assert query == {
        "league": ["mlb"],
        "token": ["[redacted]"],
        "api_key": ["[redacted]"],
    }

    headers = safe_request_headers(
        {
            "Accept": "application/json",
            "Cookie": "sid=secret",
            "Authorization": "x",
            "Referer": "https://example.test/app?token=secret",
        }
    )
    assert headers == {
        "accept": "application/json",
        "referer": "https://example.test/app?token=%5Bredacted%5D",
    }

    body = sanitize_body(
        '{"events":[],"token":"secret","ALGOLIA_API_KEY":"public-key",'
        '"nested":{"ip":"203.0.113.1","license_key":"license"}}'
    )
    assert body is not None
    payload = json.loads(body)
    assert payload["token"] == "[redacted]"
    assert payload["ALGOLIA_API_KEY"] == "[redacted]"
    assert payload["nested"] == {
        "ip": "[redacted]",
        "license_key": "[redacted]",
    }
    assert sanitize_body(
        "segment=IL&sessionToken=secret&deviceId=device&state=IL"
    ) == (
        "segment=IL&sessionToken=%5Bredacted%5D&"
        "deviceId=%5Bredacted%5D&state=IL"
    )
    binary = safe_websocket_frame(b"secret bytes")
    assert binary.startswith("[binary bytes=12 sha256=")
    assert "secret" not in binary
    assert safe_websocket_frame("101\x02session-token\x00") == (
        "101[redacted-session]\x00"
    )
    assert "verysecret" not in safe_websocket_frame(
        "#\x03P\x01__time,A_" + "verysecret" * 20 + "\x00"
    )
    script = sanitize_body(
        'var c={"SST":"opaque","SESSION_ID":"sid",'
        '"SITE_CONFIG_LOCATION":"/config?_h=challenge"};'
    )
    assert script is not None
    assert all(secret not in script for secret in ("opaque", "sid", "challenge"))
    assert sanitize_body("egress 203.0.113.9") == "egress [redacted]"
    assert json.loads(sanitize_body('{"address":"203.0.113.9"}')) == {
        "address": "[redacted]"
    }


def test_profiles_refuse_unlicensed_state_routes() -> None:
    assert profile("hardrock", "IL").source == "hardrock"
    assert profile("caesars", "PA").app_url == (
        "https://sportsbook.caesars.com/us/pa/bet/"
    )
    with pytest.raises(ValueError, match="not an online sportsbook"):
        profile("hardrock", "PA")
    with pytest.raises(ValueError, match="not an online sportsbook"):
        profile("bet365", "DC")


def test_browser_client_can_select_installed_channel(monkeypatch) -> None:
    captured = {}

    def fake_session(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(browser_module, "BrowserSession", fake_session)
    monkeypatch.setenv("ODDS_BROWSER_CHANNEL", "chrome")
    browser_module.build_browser_client(proxy="http://proxy.invalid:8080")
    assert captured["proxy"] == "http://proxy.invalid:8080"
    assert captured["channel"] == "chrome"


def test_written_research_artifact_is_sanitized_and_resumable(tmp_path) -> None:
    response = PageObservation(
        method="POST",
        url="https://api.example.test/graphql?token=secret",
        status_code=200,
        resource_type="fetch",
        request_headers={"content-type": "application/json", "cookie": "sid=x"},
        response_headers={"content-type": "application/json"},
        request_body='{"operationName":"Odds","token":"secret"}',
        response_body='{"events":[1,2]}',
    )
    socket = WebSocketObservation(
        url="wss://api.example.test/stream",
        sent=('{"subscribe":"odds"}',),
        received=('{"price":2.1}',),
    )
    path = write_observations(
        tmp_path,
        source="testbook",
        state="IL",
        transport="page",
        egress_fingerprint="a" * 64,
        responses=[response],
        websockets=[socket],
    )
    manifest = json.loads(path.read_text())
    assert "secret" not in path.read_text()
    assert "secret" not in (path.parent / "response-001.json").read_text()
    assert manifest["responses"][0]["url"].endswith("token=%5Bredacted%5D")
    assert manifest["responses"][0]["request_headers"] == {
        "content-type": "application/json"
    }
    assert manifest["responses"][0]["body"]["json"] is True
    assert manifest["responses"][0]["body_file"] == "response-001.json"
    assert json.loads((path.parent / "response-001.json").read_text()) == {
        "events": [1, 2]
    }

    with pytest.raises(ValueError, match="SHA-256"):
        write_observations(
            tmp_path,
            source="testbook",
            state="IL",
            transport="page",
            egress_fingerprint="203.0.113.9",
            responses=[],
            websockets=[],
        )
