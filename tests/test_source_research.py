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


def test_the_exchange_key_names_are_redacted_by_name_not_by_suffix() -> None:
    """The field names the credentialed exchanges actually use.

    These three missed every rule by one character — ``access_key`` matches
    neither ``_api_key`` nor ``_access_token``, and ``"secret_key".endswith(
    "_secret")`` is ``False`` — so this file's sanitization test passed while
    ``sanitize_body`` returned a ProphetX key pair completely unchanged.  A
    recon run against those venues would have written live credentials to
    ``data/research/`` in cleartext, which is why this is pinned separately
    with the literal values rather than folded into the case above.
    """
    live = "AKIA-LIVE-1234"
    body = sanitize_body(
        f'{{"access_key":"{live}","secret_key":"s3cr3t","client_id":"cid-9",'
        '"client_secret":"cs-9","private_key":"pk-9","book_id":74}'
    )
    assert body is not None
    assert live not in body and "s3cr3t" not in body and "cs-9" not in body
    payload = json.loads(body)
    assert payload == {
        "access_key": "[redacted]",
        "secret_key": "[redacted]",
        "client_id": "[redacted]",
        "client_secret": "[redacted]",
        "private_key": "[redacted]",
        # Not a credential.  Redacting the whole payload would make research
        # output useless, so the boundary is pinned from both sides.
        "book_id": 74,
    }

    # Free text, not JSON: a key pair pasted into a log line or a GraphQL body
    # still has to be caught, which is the regex's job rather than the key
    # walker's.
    loose = sanitize_body(f"POST /partner/mm/get_markets access_key={live} secret_key=s3cr3t")
    assert loose is not None
    assert live not in loose and "s3cr3t" not in loose

    # And in a query string, which is how a careless adapter would send them.
    query = parse_qs(
        urlsplit(sanitize_url(f"https://x.test/mm?access_key={live}&sport=mlb")).query
    )
    assert query == {"access_key": ["[redacted]"], "sport": ["mlb"]}


def test_the_spellings_a_real_client_uses_are_covered_too() -> None:
    """Adding literals closed one near-miss and left three open.

    A JSON API spells it ``secretKey``; a client that stores a venue's key
    namespaces it ``prophetx_secret_key``. Both reduce to a name already on the
    list, and neither matched it — so the widening that prompted this test still
    wrote live keys to disk in the two shapes most likely to occur.

    The last two cases are the boundary: ``secretary`` contains "secret" and
    ``market_secret_sauce`` ends in neither rule's suffix. Over-redaction would
    make research output useless, so both sides are pinned.
    """
    body = sanitize_body(
        '{"prophetx_secret_key":"LIVE1","x_access_key":"LIVE2","secretKey":"LIVE3",'
        '"accessKey":"LIVE4","clientId":"LIVE5","book_id":74,"secretary":"Ada",'
        '"market_secret_sauce":"vig"}'
    )
    assert body is not None
    for live in ("LIVE1", "LIVE2", "LIVE3", "LIVE4", "LIVE5"):
        assert live not in body, live
    payload = json.loads(body)
    assert payload["book_id"] == 74
    assert payload["secretary"] == "Ada"
    assert payload["market_secret_sauce"] == "vig"

    # A suffixed name in camelCase — the shape a sportsbook's own SPA sends, and
    # the one that survived the first attempt at this. ``refreshToken`` was
    # redacted while ``authToken`` beside it in the same object was not, because
    # the literal list was squashed and the suffix rules were not.
    spa = sanitize_body(
        '{"data":{"viewer":{"authToken":"LIVE6","idToken":"LIVE7",'
        '"csrfToken":"LIVE8","myApiKey":"LIVE9","privateKey":"LIVE10",'
        '"x_private_key":"LIVE11","source_key":"fanduel","event_id":"evt-1"}}}'
    )
    assert spa is not None
    for live in ("LIVE6", "LIVE7", "LIVE8", "LIVE9", "LIVE10", "LIVE11"):
        assert live not in spa, live
    viewer = json.loads(spa)["data"]["viewer"]
    # This repository's own vocabulary must survive, or research output becomes a
    # page of [redacted]. A bare "key"/"id" suffix rule would eat both of these.
    assert viewer["source_key"] == "fanduel"
    assert viewer["event_id"] == "evt-1"

    # The urlencoded branch, which is a different code path from the JSON one.
    encoded = sanitize_body("prophetx_secret_key=LIVE7&sport=mlb")
    assert encoded is not None and "LIVE7" not in encoded and "sport=mlb" in encoded


def test_a_secret_reachable_only_by_its_shape_is_caught_in_json_too() -> None:
    """The structured path used to be weaker than the free-text one.

    Only the key walker ran on a parsed body, so a secret whose *key* says
    nothing — a JWT under ``"jwt"``, a socket URL carrying ``?token=`` under
    ``"wsUrl"`` — survived being parsed and would not have survived being
    unparseable. Every venue worth reconning answers JSON.

    The shape rules run per string value rather than over the serialized
    document, because ``_SECRET_TEXT`` matches to a delimiter and is not
    quote-aware: run over the finished JSON it eats the closing quote and writes
    a manifest that no longer parses. So this asserts the output is still JSON.
    """
    # The keys here are deliberately innocent. An earlier version of this test
    # put the JWT under ``"jwt"`` — which is itself in ``_SENSITIVE_KEYS`` — so
    # the key walker redacted it and the shape rule it claimed to pin could be
    # deleted with the suite still green. "Reachable only by its shape" has to
    # mean the key says nothing.
    body = sanitize_body(
        '{"pageProps":{"ctx":"eyJhbGciOiJSUzI1NiIsImtpZCI6Ijd9.'
        + "LIVEJWTPAYLOAD" * 4
        + '.LIVE-SIG_x"},'
        '"wsUrl":"wss://push.book.test/socket?token=WS-LIVE-TOKEN",'
        '"book_id":74,"h":1.9,"event_key":"MLB-PHI@MLB-MIA"}'
    )
    assert body is not None
    assert "WS-LIVE-TOKEN" not in body
    assert "LIVEJWTPAYLOAD" not in body

    # And a bearer credential, where the delimiter-terminated rule matches the
    # scheme word and stops: it redacted "Bearer" and left the token, stamping
    # the line as sanitized.
    bearer = sanitize_body(
        '{"headers":{"h1":"Authorization: Bearer LIVE-OPAQUE-SESSION-VALUE-9f3a"}}'
    )
    assert bearer is not None and "LIVE-OPAQUE-SESSION-VALUE-9f3a" not in bearer

    payload = json.loads(body)  # must still parse
    # "h" is how compact odds JSON spells *home*. Deriving the squashed key set
    # without a length floor made "_h" contribute "h" and redacted every one.
    assert payload["h"] == 1.9
    assert payload["book_id"] == 74
    assert payload["event_key"] == "MLB-PHI@MLB-MIA"


def test_the_websocket_and_ip_paths_are_unchanged() -> None:
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
