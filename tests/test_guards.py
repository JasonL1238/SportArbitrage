"""Soft-failure detection.

Each of these responses would otherwise produce "0 quotes collected" with no
indication that anything was wrong.
"""
from __future__ import annotations

import pytest

from src.sources.guards import (
    BlockedError,
    CaptchaError,
    EmptyResponseError,
    FormatChangeError,
    GeoRestrictedError,
    HttpStatusError,
    LoginRequiredError,
    NotJsonError,
    RateLimitedError,
    ServerError,
    check_http_response,
    parse_retry_after,
    require_keys,
    require_list,
    require_mapping,
    require_nonempty,
)


def _check(
    body: str,
    status: int = 200,
    content_type: str | None = "application/json",
    retry_after=None,
):
    return check_http_response(
        source="testbook",
        endpoint="odds",
        status_code=status,
        body=body,
        content_type=content_type,
        url="https://example.invalid/odds",
        retry_after=retry_after,
    )


def test_empty_body_is_detected() -> None:
    with pytest.raises(EmptyResponseError):
        _check("")
    with pytest.raises(EmptyResponseError):
        _check("   \n  ")


def test_captcha_page_is_detected() -> None:
    body = '<html><head><title>Just a moment...</title></head><body><div class="g-recaptcha"></div></body></html>'
    with pytest.raises(CaptchaError):
        _check(body, content_type="text/html")


def test_bot_challenge_is_detected() -> None:
    body = "<html><body><script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script></body></html>"
    with pytest.raises(CaptchaError):
        _check(body, content_type="text/html")


def test_login_page_is_detected() -> None:
    body = '<html><body><form><input type="password" name="pw"></form></body></html>'
    with pytest.raises(LoginRequiredError):
        _check(body, content_type="text/html")


def test_block_page_is_detected() -> None:
    body = "<html><body><h1>Access denied</h1><p>Error 1020</p></body></html>"
    with pytest.raises(BlockedError):
        _check(body, content_type="text/html")


def test_geo_restriction_is_its_own_refusal_not_a_generic_block() -> None:
    """Where we are is a permanent fact about this host; a bot challenge is not.

    Reported as one class, the collector either retries a restriction it can
    never satisfy or gives up on a challenge that a pause would clear.
    """
    body = "<html><body>This content is not available in your region.</body></html>"
    with pytest.raises(GeoRestrictedError):
        _check(body, content_type="text/html")
    with pytest.raises(GeoRestrictedError):
        _check('{"detail":"not available in your region"}')
    assert not GeoRestrictedError("x").retryable


def test_rate_limiting_is_retryable_and_carries_the_servers_own_delay() -> None:
    """Kambi refuses an unknown tenant with a literal ``429 "No access"`` and
    Kalshi rate-limits an unpaced probe, so this is a live case."""
    with pytest.raises(RateLimitedError) as excinfo:
        _check('{"error":"slow down"}', status=429, retry_after="12")
    assert excinfo.value.retry_after == 12.0
    assert excinfo.value.retryable
    # An unreadable or absent Retry-After leaves the caller's own backoff to it,
    # rather than guessing a number the server never stated.
    with pytest.raises(RateLimitedError) as excinfo:
        _check('{"error":"slow down"}', status=429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
    assert excinfo.value.retry_after is None


def test_a_denial_is_still_a_block() -> None:
    with pytest.raises(BlockedError):
        _check("nope", status=403, content_type="text/plain")
    with pytest.raises(BlockedError):
        _check("nope", status=401, content_type="text/plain")


def test_server_error_is_a_retryable_refinement_of_http_status() -> None:
    with pytest.raises(HttpStatusError):
        _check('{"error":"boom"}', status=500)
    with pytest.raises(ServerError) as excinfo:
        _check('{"error":"boom"}', status=503)
    assert excinfo.value.retryable


def test_a_json_shaped_block_at_http_200_is_not_a_format_change() -> None:
    """The failure this closes: a refusal that decodes as JSON passes every
    status check, and the shape checks downstream then report a renamed field
    that was never there."""
    with pytest.raises(BlockedError):
        _check('{"error":"Request blocked","ref":"18.4560d017"}')


def test_a_large_json_payload_is_not_marker_scanned() -> None:
    """The scan is bounded so that a real slate is never searched for trouble.

    A market really can be named "Access denied" — that is a string a book
    controls — so the bound is what keeps a 2 MB payload from being refused on
    the strength of one label.
    """
    filler = "x" * 70_000
    payload = _check('{"marketName": "access denied", "filler": "%s"}' % filler)
    assert payload["marketName"] == "access denied"


def test_retry_after_parsing_rejects_nonsense() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("  2.5 ") == 2.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("-1") is None
    assert parse_retry_after("soon") is None


def test_html_without_markers_is_reported_as_not_json() -> None:
    with pytest.raises(NotJsonError) as excinfo:
        _check("<!doctype html><html><body>Hello</body></html>", content_type="text/html")
    assert "HTML" in str(excinfo.value)


def test_valid_json_passes_through() -> None:
    assert _check('{"events": [1]}') == {"events": [1]}


def test_json_body_is_not_scanned_for_the_loose_markers() -> None:
    """A market legitimately named "Sign in to continue" must not read as a
    login page.  A JSON body is scanned only for full phrases no odds payload
    can contain — never for bare words like "login" or "cloudflare"."""
    payload = _check('{"marketName": "please log in", "captcha": "recaptcha", "cdn": "cloudflare"}')
    assert payload["marketName"] == "please log in"


def test_shape_checks_report_format_change() -> None:
    with pytest.raises(FormatChangeError):
        require_mapping([1, 2], source="testbook", endpoint="odds")
    with pytest.raises(FormatChangeError):
        require_list({"a": 1}, source="testbook", endpoint="odds")
    with pytest.raises(FormatChangeError) as excinfo:
        require_keys({"attachments": {}}, ("events", "markets"), source="testbook", endpoint="odds")
    assert "events" in str(excinfo.value)


def test_empty_collection_is_reported_as_empty_response() -> None:
    with pytest.raises(EmptyResponseError):
        require_nonempty([], source="testbook", endpoint="odds", what="events")
    with pytest.raises(EmptyResponseError):
        require_nonempty({}, source="testbook", endpoint="odds", what="markets")


def test_error_kinds_are_stable_labels() -> None:
    """Health reporting keys off these, so they must not drift silently."""
    assert BlockedError.kind == "blocked"
    assert CaptchaError.kind == "captcha"
    assert LoginRequiredError.kind == "login_required"
    assert EmptyResponseError.kind == "empty_response"
    assert FormatChangeError.kind == "format_change"
    assert NotJsonError.kind == "not_json"
    assert HttpStatusError.kind == "http_status"
    assert RateLimitedError.kind == "rate_limited"
    assert GeoRestrictedError.kind == "geo_restricted"
    assert ServerError.kind == "server_error"


def test_rate_limiting_and_geo_restriction_are_not_blocks() -> None:
    """Deliberately not subclasses: an ``except BlockedError`` that caught them
    would put the three back into one bucket, which is the fault being fixed."""
    assert not issubclass(RateLimitedError, BlockedError)
    assert not issubclass(GeoRestrictedError, BlockedError)
