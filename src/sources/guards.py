"""Detection of soft failures that would otherwise look like "no data".

A scraper that returns zero rows because it was served a Cloudflare
interstitial is indistinguishable, downstream, from a scraper that returned
zero rows because there are no games today — unless something checks.  These
guards make that difference explicit and loud.

Marker scanning only runs on bodies that are *not* valid JSON.  Searching a
legitimate odds payload for the word "login" would produce false positives
(markets and team names are arbitrary text), so shape checks handle the JSON
case instead.
"""
from __future__ import annotations

import json
import re
from typing import Any


class SourceError(Exception):
    """Base class for source fetch/parse failures."""

    #: Short stable label used in health reports and stored run records.
    kind = "source_error"


class HttpStatusError(SourceError):
    kind = "http_status"


class BlockedError(SourceError):
    """Access denied, rate limited, geo-blocked, or bot-challenged."""

    kind = "blocked"


class CaptchaError(SourceError):
    kind = "captcha"


class LoginRequiredError(SourceError):
    kind = "login_required"


class NotJsonError(SourceError):
    kind = "not_json"


class EmptyResponseError(SourceError):
    """The response was well formed but carried no usable data."""

    kind = "empty_response"


class FormatChangeError(SourceError):
    """The payload parsed as JSON but no longer has the expected shape."""

    kind = "format_change"


_CAPTCHA_MARKERS = (
    "recaptcha",
    "hcaptcha",
    "g-recaptcha",
    "px-captcha",
    "captcha",
    "are you a human",
    "verify you are human",
    "challenge-platform",
    "cf-challenge",
    "__cf_chl",
)

_BLOCK_MARKERS = (
    "access denied",
    "access to this page has been denied",
    "attention required",
    "request blocked",
    "you have been blocked",
    "cloudflare",
    "akamai reference",
    "reference #",
    "not available in your region",
    "unavailable in your location",
    "restricted territory",
    "perimeterx",
    "datadome",
)

_LOGIN_MARKERS = (
    "sign in to continue",
    "please log in",
    "please sign in",
    "log in to your account",
    "authentication required",
    "session expired",
    'type="password"',
    "name=\"password\"",
)

_BLOCK_STATUSES = frozenset({401, 403, 407, 429, 451})

_HTML_HINT = re.compile(r"^\s*(<!doctype|<html|<\?xml)", re.IGNORECASE)


def _describe(source: str, endpoint: str) -> str:
    return f"{source}:{endpoint}"


def check_http_response(
    *,
    source: str,
    endpoint: str,
    status_code: int,
    body: str,
    content_type: str | None = None,
    url: str | None = None,
) -> Any:
    """Validate a raw HTTP response and return its decoded JSON payload.

    Raises the most specific :class:`SourceError` subclass that applies, so
    health reporting can distinguish "blocked" from "format changed" from
    "genuinely empty".
    """
    where = _describe(source, endpoint)
    stripped = body.strip()

    if not stripped:
        raise EmptyResponseError(f"{where}: empty response body (HTTP {status_code})")

    # Decode first: a JSON error envelope is more informative than a status code.
    payload: Any = None
    is_json = False
    try:
        payload = json.loads(stripped)
        is_json = True
    except ValueError:
        is_json = False

    if not is_json:
        lowered = stripped[:20_000].lower()
        for marker in _CAPTCHA_MARKERS:
            if marker in lowered:
                raise CaptchaError(f"{where}: CAPTCHA/bot challenge served (marker {marker!r})")
        for marker in _LOGIN_MARKERS:
            if marker in lowered:
                raise LoginRequiredError(f"{where}: login page served (marker {marker!r})")
        for marker in _BLOCK_MARKERS:
            if marker in lowered:
                raise BlockedError(f"{where}: access blocked (marker {marker!r})")
        if status_code in _BLOCK_STATUSES:
            raise BlockedError(f"{where}: HTTP {status_code} without a parseable body")
        if status_code >= 400:
            raise HttpStatusError(f"{where}: HTTP {status_code}")
        kind = "HTML" if _HTML_HINT.match(stripped) else f"content-type {content_type!r}"
        raise NotJsonError(
            f"{where}: expected JSON, got {kind} ({len(body)} bytes) from {url or 'unknown url'}"
        )

    if status_code in _BLOCK_STATUSES:
        raise BlockedError(f"{where}: HTTP {status_code}: {stripped[:200]}")
    if status_code >= 400:
        raise HttpStatusError(f"{where}: HTTP {status_code}: {stripped[:200]}")

    return payload


def require_mapping(payload: Any, *, source: str, endpoint: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FormatChangeError(
            f"{_describe(source, endpoint)}: expected a JSON object, got {type(payload).__name__}"
        )
    return payload


def require_list(payload: Any, *, source: str, endpoint: str) -> list[Any]:
    if not isinstance(payload, list):
        raise FormatChangeError(
            f"{_describe(source, endpoint)}: expected a JSON array, got {type(payload).__name__}"
        )
    return payload


def require_keys(
    payload: dict[str, Any], keys: tuple[str, ...], *, source: str, endpoint: str
) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise FormatChangeError(
            f"{_describe(source, endpoint)}: payload missing expected key(s) {missing}; "
            f"present: {sorted(payload)[:15]}"
        )


def require_nonempty(items: Any, *, source: str, endpoint: str, what: str) -> None:
    if not items:
        raise EmptyResponseError(f"{_describe(source, endpoint)}: no {what} in response")
