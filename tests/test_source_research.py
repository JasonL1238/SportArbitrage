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


def test_a_sensitive_field_does_not_swallow_the_prices_after_it() -> None:
    """The unquoted-value rule has to stop at every delimiter, not just JSON's.

    bet365's pull-pod frames are ``;``-and-control-character delimited.  The
    rule stopped only at ``&``, whitespace, ``,`` and ``}``, so one
    sensitive-looking field code deleted the whole rest of the record: the
    frame below sanitized to ``PA;ID=1;UID=[redacted]`` and took three prices
    with it.  A manifest redacted that way says "these frames carry no odds"
    about frames that carried odds, which is why this is pinned rather than
    left to the regex's shape.
    """
    assert safe_websocket_frame("PA;ID=1;UID=44;OD=5/6;") == (
        "PA;ID=1;UID=[redacted];OD=5/6;"
    )
    assert safe_websocket_frame("MA;TOKEN=abc;OD=1/1;") == (
        "MA;TOKEN=[redacted];OD=1/1;"
    )
    # A record with nothing sensitive in it is still passed through untouched.
    assert safe_websocket_frame("\x08PA;ID=99;NA=New York Yankees;OD=5/6;") == (
        "\x08PA;ID=99;NA=New York Yankees;OD=5/6;"
    )
    assert sanitize_body("token=LIVE1;next=2") == "token=[redacted];next=2"
    assert json.loads(sanitize_body('{"token":"LIVE;WITH;SEMIS"}')) == {
        "token": "[redacted]"
    }


def test_narrowing_the_value_did_not_narrow_what_gets_redacted() -> None:
    """The price fix had to be paid for, and this is the payment.

    Stopping the value at ``;`` and the control characters took away an
    *accidental* protection.  ``_SECRET_TEXT`` names 22 keys while the JSON
    walker's ``_is_sensitive_key`` knows many more, and in free text the extra
    ones were only ever covered by the old rule running past the delimiter and
    swallowing them.  Both shapes below are ones this repository actually
    captures, and both regressed to cleartext before the free-text path was
    made to ask the same predicate the structured path asks.

    Every assertion here fails against the delimiter fix alone.
    """
    # A cookie jar written without spaces after the semicolons — RFC-legal, and
    # the shape ``document.cookie`` and echoed headers really take.
    jar = sanitize_body("Cookie: sid=LIVE-SID;auth=LIVE-AUTH;PHPSESSID=LIVE-PHP")
    assert "LIVE-SID" not in jar
    assert "LIVE-AUTH" not in jar, "auth is sensitive to the JSON path too"
    assert "LIVE-PHP" not in jar

    # A pull-pod frame: the same records that carry ``OD=`` carry session
    # material, so this is the exact shape the price fix was written for.
    frame = safe_websocket_frame(
        "token=LIVE-TK\x01sid=LIVE-SID\x02authkey=LIVE-AK\x03signature=LIVE-SIG"
    )
    assert all(
        secret not in frame
        for secret in ("LIVE-TK", "LIVE-SID", "LIVE-AK", "LIVE-SIG")
    )

    # A query string in free text, where the price must survive the same way.
    assert sanitize_body("GET /pod?uid=44;od=5/6 HTTP/1.1") == (
        "GET /pod?uid=[redacted];od=5/6 HTTP/1.1"
    )
    # And the control characters ``\s`` does not cover are stops as well.
    assert sanitize_body("uid=44\x0eod=5/6") == "uid=[redacted]\x0eod=5/6"

    # Repository vocabulary is still not a credential — the word-split rule that
    # kept ``possession`` and a player named Schrauth readable is unchanged.
    kept = sanitize_body("source_key=fanduel;book_key=x;event_id=7;token_count=3")
    assert kept == "source_key=fanduel;book_key=x;event_id=7;token_count=3"


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


# ── observe_page: the research primitive's own failure modes ──────────────────
#
# These exist because the primitive had no tests at all, and three separate
# defects in it silently destroyed evidence rather than reporting anything.


class _FakeRequest:
    def __init__(self, url: str, resource_type: str = "xhr", failure: str = "") -> None:
        self.url = url
        self.resource_type = resource_type
        self.method = "GET"
        self.headers = {"accept": "application/json"}
        self.post_data = None
        self.failure = failure


class _FakeResponse:
    def __init__(self, request: _FakeRequest, status: int, body: str) -> None:
        self.request = request
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._body = body

    def text(self) -> str:
        return self._body


class _FakeConsoleMessage:
    def __init__(self, level: str, text: str, url: str = "") -> None:
        self.type = level
        self.text = text
        self.location = {"url": url}


class _FakePage:
    """The handful of Playwright page methods ``observe_page`` actually calls."""

    def __init__(
        self,
        *,
        click_raises: bool = False,
        evaluate_raises: bool = False,
        script=(),
        after_route=(),
    ) -> None:
        self.handlers: dict[str, list] = {}
        self.removed: list[tuple[str, object]] = []
        self._click_raises = click_raises
        self._evaluate_raises = evaluate_raises
        self._script = list(script)
        self._after_route = list(after_route)
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[tuple[str, object]] = []
        self.order: list[str] = []

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self.removed.append((event, handler))
        self.handlers.get(event, []).remove(handler)

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        self.order.append("goto")

    def evaluate(self, _expression: str, arg=None):
        self.order.append("evaluate")
        self.evaluate_calls.append((_expression, arg))
        if self._evaluate_raises:
            raise RuntimeError("execution context was destroyed")
        # Routing is what makes the *second* batch of traffic arrive, so the
        # fake emits it only once the hash has actually been set.
        self._script.extend(self._after_route)
        self._after_route = []

    def wait_for_timeout(self, _ms) -> None:
        # Traffic arrives during the wait, which is when a real page emits it.
        for event, payload in self._script:
            for handler in list(self.handlers.get(event, [])):
                handler(payload)
        self._script = []

    def get_by_text(self, text: str, exact: bool = False):
        page = self

        class _Locator:
            @property
            def first(self):
                return self

            def click(self, **_kwargs):
                if page._click_raises:
                    raise TimeoutError(f"locator resolved to no element: {text!r}")

        return _Locator()


def _session(page: _FakePage):
    """A BrowserSession around a fake page, without launching Chromium."""
    from src.sources.browser import BrowserSession

    session = object.__new__(BrowserSession)
    session._page = page
    session._timeout_ms = 1_000
    return session


def test_a_missed_click_is_recorded_rather_than_discarding_the_capture() -> None:
    """A selector that misses used to throw away the whole session.

    ``recon_sources`` catches the exception and returns *before* it writes
    anything, so a three-minute capture was lost because one piece of
    navigation text did not match — on a workflow whose entire method is trial
    and error over navigation text.
    """
    page = _FakePage(
        click_raises=True,
        script=[
            (
                "response",
                _FakeResponse(
                    _FakeRequest("https://book.test/api/odds"), 200, '{"odds":[1]}'
                ),
            )
        ],
    )
    responses, sockets = _session(page).observe_page(
        "https://book.test/", wait_ms=100, click_text="Baseball"
    )

    # The traffic survived the missed click.
    assert [r.url for r in responses if r.status_code == 200] == [
        "https://book.test/api/odds"
    ]
    # And the miss is itself evidence, naming the text that failed.
    misses = [r for r in responses if r.method == "CLICK"]
    assert len(misses) == 1
    assert "Baseball" in misses[0].response_body
    assert misses[0].status_code == 0
    assert sockets == []


def test_failed_requests_and_console_errors_reach_the_manifest() -> None:
    """A preloader that stalls on a request that never completed left no trace.

    The ``response`` handler only sees exchanges that finished, so a stalled
    session and a session with nothing to fetch produced identical manifests.
    """
    page = _FakePage(
        script=[
            (
                "requestfailed",
                _FakeRequest(
                    "https://pod.book.test/subscribe",
                    resource_type="fetch",
                    failure="net::ERR_CONNECTION_TIMED_OUT",
                ),
            ),
            ("console", _FakeConsoleMessage("error", "catalog subscribe failed")),
            ("console", _FakeConsoleMessage("log", "telemetry heartbeat")),
        ]
    )
    responses, _ = _session(page).observe_page("https://book.test/", wait_ms=100)

    failed = [r for r in responses if r.method == "FAILED"]
    assert len(failed) == 1
    assert failed[0].url == "https://pod.book.test/subscribe"
    assert "ERR_CONNECTION_TIMED_OUT" in failed[0].response_body
    assert failed[0].status_code == 0

    console = [r for r in responses if r.method == "CONSOLE"]
    # Errors are kept; ordinary telemetry logs are not, or the manifest is noise.
    assert [c.response_body for c in console] == ["catalog subscribe failed"]


def test_console_and_failure_diagnostics_are_sanitized() -> None:
    """These are bodies like any other, so they go through the same redactors."""
    page = _FakePage(
        script=[
            (
                "console",
                _FakeConsoleMessage("error", "boot failed for session_id=LIVE-SID-7"),
            ),
            (
                "requestfailed",
                _FakeRequest(
                    "https://book.test/auth?token=LIVE-TOKEN-9", failure="aborted"
                ),
            ),
        ]
    )
    responses, _ = _session(page).observe_page("https://book.test/", wait_ms=100)
    rendered = " ".join(f"{r.url} {r.response_body}" for r in responses)
    assert "LIVE-SID-7" not in rendered
    assert "LIVE-TOKEN-9" not in rendered


def test_observing_twice_does_not_double_count_the_second_pass() -> None:
    """Handlers were registered per call and never removed.

    A warm-then-observe flow — exactly what the persistent-profile rung needs —
    reported the second pass's traffic once per previous call.
    """
    response = ("response", _FakeResponse(_FakeRequest("https://book.test/a"), 200, "{}"))
    page = _FakePage(script=[response])
    session = _session(page)

    first, _ = session.observe_page("https://book.test/", wait_ms=10)
    assert len(first) == 1
    assert page.handlers.get("response", []) == []

    page._script = [response]
    second, _ = session.observe_page("https://book.test/", wait_ms=10)
    assert len(second) == 1


def test_a_hash_route_is_applied_after_boot_and_its_traffic_is_captured() -> None:
    """A hash-routed app ignores a deep link supplied in the initial URL.

    Its router reads ``location.hash`` once, during a boot that has not happened
    when ``goto`` resolves — so bet365's Illinois board answered a deep-linked
    MLB route with the home page and issued no league request at all, and a
    click on the nav text did not route either.  Setting the hash *after* the
    app is up is the same same-document navigation its own menu performs.
    """
    page = _FakePage(
        script=[
            (
                "response",
                _FakeResponse(
                    _FakeRequest("https://book.test/api/home"), 200, '{"home":1}'
                ),
            )
        ],
        after_route=[
            (
                "response",
                _FakeResponse(
                    _FakeRequest("https://book.test/api/league"), 200, '{"league":1}'
                ),
            )
        ],
    )
    responses, _sockets = _session(page).observe_page(
        "https://book.test/", wait_ms=900, then_hash="#/AC/B16/"
    )

    # Booted first, routed second — the order is the whole point.
    assert page.order == ["goto", "evaluate"]
    assert page.evaluate_calls[0][1] == "#/AC/B16/"
    # Traffic from *both* sides of the route survives.
    assert [r.url for r in responses] == [
        "https://book.test/api/home",
        "https://book.test/api/league",
    ]


def test_a_route_that_cannot_be_applied_is_recorded_rather_than_fatal() -> None:
    """Same rule as a missed click: the traffic before it is still evidence."""
    page = _FakePage(
        evaluate_raises=True,
        script=[
            (
                "response",
                _FakeResponse(
                    _FakeRequest("https://book.test/api/home"), 200, '{"home":1}'
                ),
            )
        ],
    )
    responses, _sockets = _session(page).observe_page(
        "https://book.test/", wait_ms=900, then_hash="#/AC/B16/"
    )

    assert [r.url for r in responses if r.status_code == 200] == [
        "https://book.test/api/home"
    ]
    routed = [r for r in responses if r.method == "ROUTE"]
    assert len(routed) == 1
    assert "#/AC/B16/" in routed[0].response_body
    assert "RuntimeError" in routed[0].response_body


def test_no_hash_means_no_evaluation_at_all() -> None:
    """The default path must not touch the page's JavaScript context."""
    page = _FakePage()
    _session(page).observe_page("https://book.test/", wait_ms=100)
    assert page.evaluate_calls == []
    assert page.order == ["goto"]
