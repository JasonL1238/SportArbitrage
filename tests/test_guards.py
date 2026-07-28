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
    HttpStatusError,
    LoginRequiredError,
    NotJsonError,
    check_http_response,
    require_keys,
    require_list,
    require_mapping,
    require_nonempty,
)


def _check(body: str, status: int = 200, content_type: str | None = "application/json"):
    return check_http_response(
        source="testbook",
        endpoint="odds",
        status_code=status,
        body=body,
        content_type=content_type,
        url="https://example.invalid/odds",
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


def test_geo_restriction_is_detected() -> None:
    body = "<html><body>This content is not available in your region.</body></html>"
    with pytest.raises(BlockedError):
        _check(body, content_type="text/html")


def test_rate_limit_status_is_blocked_not_generic() -> None:
    with pytest.raises(BlockedError):
        _check('{"error":"slow down"}', status=429)
    with pytest.raises(BlockedError):
        _check("nope", status=403, content_type="text/plain")


def test_server_error_with_json_body_is_reported_as_status() -> None:
    with pytest.raises(HttpStatusError):
        _check('{"error":"boom"}', status=500)


def test_html_without_markers_is_reported_as_not_json() -> None:
    with pytest.raises(NotJsonError) as excinfo:
        _check("<!doctype html><html><body>Hello</body></html>", content_type="text/html")
    assert "HTML" in str(excinfo.value)


def test_valid_json_passes_through() -> None:
    assert _check('{"events": [1]}') == {"events": [1]}


def test_json_body_is_not_marker_scanned() -> None:
    """A market legitimately named "Sign in to continue" must not read as a
    login page — only non-JSON bodies are scanned for markers."""
    payload = _check('{"marketName": "please log in", "captcha": "recaptcha"}')
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
