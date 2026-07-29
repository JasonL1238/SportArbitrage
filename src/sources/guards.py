"""Detection of soft failures that would otherwise look like "no data".

A scraper that returns zero rows because it was served a Cloudflare
interstitial is indistinguishable, downstream, from a scraper that returned
zero rows because there are no games today — unless something checks.  These
guards make that difference explicit and loud.

Marker scanning on a *non-JSON* body may use the full marker set: a page that
is not JSON at all is already not the odds payload that was asked for, so a
false positive costs nothing.  A **JSON** body is scanned too, but only for
unambiguous full phrases and only when it is small enough to be an error
envelope rather than a slate — see :data:`_JSON_BLOCK_MARKERS`.  Searching a
legitimate 2 MB odds payload for the word "login" would produce false positives,
because markets and team names are arbitrary text; searching a 400-byte body for
``"not available in your region"`` cannot.

**Three refusals, not one.**  "Blocked", "rate limited" and "geo-restricted" were
previously one class, and the three call for opposite responses: a 429 should be
retried after a pause, a 451 should stop the source being asked at all, and a 403
bot challenge is neither.  Kambi answers an unknown tenant with a literal
``429 "No access"`` and Kalshi rate-limits an unpaced probe, so this is a live
distinction rather than a tidy one.
"""
from __future__ import annotations

import json
import re
from typing import Any


class SourceError(Exception):
    """Base class for source fetch/parse failures."""

    #: Short stable label used in health reports and stored run records.
    kind = "source_error"

    #: Whether asking again, after a pause, could plausibly succeed.  Retrying a
    #: permanent refusal is not persistence, it is a small denial of service
    #: aimed at a source that has already said no.
    retryable = False

    #: The captured response that produced this failure, when there was one.
    #:
    #: Set by :class:`src.sources._common.SourceClient` so the collector can
    #: persist the bytes that *explain* a failure — which are the bytes most
    #: worth having kept, and which were previously built and then dropped on the
    #: floor as the exception propagated.  ``None`` for a failure with no
    #: response at all (DNS, TLS, timeout).
    raw: Any = None


class HttpStatusError(SourceError):
    kind = "http_status"


class BlockedError(SourceError):
    """Access denied or bot-challenged.

    Deliberately *not* used for rate limiting or geo-restriction any more; those
    have their own classes, because the right response to each differs.
    """

    kind = "blocked"


class RateLimitedError(SourceError):
    """The source asked us to slow down.  Temporary, and our own fault.

    Carries the server's own ``Retry-After`` when it supplied one, because
    guessing a backoff when the source has stated one is both ruder and slower
    than honouring it.
    """

    kind = "rate_limited"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GeoRestrictedError(SourceError):
    """This machine's location is not one the source serves.

    Permanent for as long as the run is on this host, so a source that raises it
    is recorded as unreachable rather than retried.  Never a licence to work
    around the restriction — see the project's non-negotiables.
    """

    kind = "geo_restricted"


class ServerError(HttpStatusError):
    """The source failed on its own side (HTTP 5xx).  Worth one more try.

    A refinement of :class:`HttpStatusError` rather than a sibling: a 5xx really
    is a status failure, and the only thing being added is that asking again may
    work.  :class:`RateLimitedError` and :class:`GeoRestrictedError` are
    deliberately *not* subclasses of :class:`BlockedError` for the opposite
    reason — there, treating the three as one is the mistake being corrected, and
    a subclass would let ``except BlockedError`` quietly re-merge them.
    """

    kind = "server_error"
    retryable = True


class TransportError(SourceError):
    """The request never completed: DNS, TLS, connection reset, timeout."""

    kind = "transport"
    retryable = True


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


class CoverageCappedError(SourceError):
    """Scopes this adapter chose not to fetch, not scopes the venue refused.

    A bound on request volume is politeness and stays; reporting the run as
    complete afterwards is not.  Carried as a refusal because that is what it is
    from the operator's side — fixtures the venue has and this run does not —
    while keeping the cause distinguishable from a 403.
    """

    kind = "coverage_capped"


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
    "perimeterx",
    "datadome",
)

#: Markers that mean specifically "not from where you are", which is a different
#: fact from "we do not like your client" and calls for a different response.
_GEO_MARKERS = (
    "not available in your region",
    "not available in your country",
    "unavailable in your location",
    "restricted territory",
    "geo-restricted",
    "geo restricted",
)

#: Markers scanned inside a **JSON** body.  Full phrases only: a market label or
#: a competitor name can contain the word "blocked" or "cloudflare", so the loose
#: markers above would fire on real data.  None of these can occur in an odds
#: payload by accident.
_JSON_BLOCK_MARKERS = (
    "access denied",
    "request blocked",
    "you have been blocked",
    "access to this page has been denied",
)

#: A JSON body larger than this is a slate, not an error envelope, and is not
#: scanned.  Stated as a bound rather than left implicit because "we searched the
#: payload for trouble" and "we searched the first part of it" are different
#: claims.  The largest refusal envelope observed is under 2 KB.
MAX_JSON_SCAN_BYTES = 65_536

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

#: Refusals that are about *us* rather than about the data: the request was
#: understood and declined.  Split three ways because the responses differ.
_BLOCKED_STATUSES = frozenset({401, 403, 407})
RATE_LIMIT_STATUS = 429
GEO_RESTRICTED_STATUS = 451

_HTML_HINT = re.compile(r"^\s*(<!doctype|<html|<\?xml)", re.IGNORECASE)


def _describe(source: str, endpoint: str) -> str:
    return f"{source}:{endpoint}"


def parse_retry_after(value: Any) -> float | None:
    """Seconds to wait, from a ``Retry-After`` header stating a delay.

    Only the delta-seconds form is honoured.  The HTTP-date form is legal and
    rarer, and turning one into a delay needs a clock the guards deliberately do
    not have — a wrong reading there would sleep for hours or not at all, so it
    is reported as "unstated" and the caller's own backoff applies.
    """
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _refusal_for_status(
    status_code: int, where: str, detail: str, *, retry_after: float | None
) -> SourceError | None:
    if status_code == RATE_LIMIT_STATUS:
        return RateLimitedError(
            f"{where}: HTTP 429 rate limited{detail}", retry_after=retry_after
        )
    if status_code == GEO_RESTRICTED_STATUS:
        return GeoRestrictedError(f"{where}: HTTP 451 unavailable for legal reasons{detail}")
    if status_code in _BLOCKED_STATUSES:
        return BlockedError(f"{where}: HTTP {status_code}{detail}")
    return None


def _marker_refusal(where: str, lowered: str, *, json_body: bool) -> SourceError | None:
    """The most specific refusal the body's own text supports, if any."""
    if not json_body:
        for marker in _CAPTCHA_MARKERS:
            if marker in lowered:
                return CaptchaError(f"{where}: CAPTCHA/bot challenge served (marker {marker!r})")
        for marker in _LOGIN_MARKERS:
            if marker in lowered:
                return LoginRequiredError(f"{where}: login page served (marker {marker!r})")
    # Geo before generic: "not available in your region" is also "blocked", and
    # only the narrower reading tells the collector to stop asking.
    for marker in _GEO_MARKERS:
        if marker in lowered:
            return GeoRestrictedError(
                f"{where}: this location is not served (marker {marker!r})"
            )
    markers = _JSON_BLOCK_MARKERS if json_body else _BLOCK_MARKERS
    for marker in markers:
        if marker in lowered:
            return BlockedError(f"{where}: access blocked (marker {marker!r})")
    return None


class _NonFiniteJson(ValueError):
    """A body carrying ``NaN``/``Infinity``, which no JSON specification allows."""


def _refuse_non_finite(name: str) -> float:
    raise _NonFiniteJson(
        f"response contains the bare literal {name}, which is not valid JSON and "
        "decodes to a float no downstream bound can catch"
    )


def check_http_response(
    *,
    source: str,
    endpoint: str,
    status_code: int,
    body: str,
    content_type: str | None = None,
    url: str | None = None,
    retry_after: Any = None,
) -> Any:
    """Validate a raw HTTP response and return its decoded JSON payload.

    Raises the most specific :class:`SourceError` subclass that applies, so
    health reporting can distinguish "rate limited" from "geo-restricted" from
    "blocked" from "format changed" from "genuinely empty" — and so the fetch
    layer knows which of those is worth asking again about.
    """
    where = _describe(source, endpoint)
    stripped = body.strip()
    delay = parse_retry_after(retry_after)

    if not stripped:
        refusal = _refusal_for_status(status_code, where, " with an empty body", retry_after=delay)
        if refusal is not None:
            raise refusal
        if status_code >= 500:
            # A bodiless 502/503 from a CDN is ordinary, and it is an outage
            # rather than an off day: ``EmptyResponseError`` is documented as
            # "well formed but carried no usable data" and is **not retryable**,
            # so the one attempt was all the source got.
            raise ServerError(f"{where}: HTTP {status_code} with an empty body")
        if status_code >= 400:
            raise HttpStatusError(f"{where}: HTTP {status_code} with an empty body")
        raise EmptyResponseError(f"{where}: empty response body (HTTP {status_code})")

    # Decode first: a JSON error envelope is more informative than a status code.
    payload: Any = None
    is_json = False
    try:
        # Non-finite literals are refused here as they are in
        # ``RawResponse.json``: this decode is what decides whether a body is a
        # JSON error envelope, and a bare ``NaN`` clearing it means the source
        # dies later as an unexplained ``parse_error`` rather than being refused
        # here with its bytes in hand.
        payload = json.loads(stripped, parse_constant=_refuse_non_finite)
        is_json = True
    except _NonFiniteJson as exc:
        # It *is* JSON, and unusable.  Left to fall through, the body would meet
        # the loose block/login marker scan below — the one this module's own
        # docstring says must never be applied to a real payload — and a data
        # fault would be reported as ``blocked``, sending an operator to look at
        # their IP address instead of at the response.
        #
        # The status still decides first, though.  A 429 whose body happens to
        # carry a non-finite literal is a rate limit: classifying it as a format
        # change loses both the retry and the ``Retry-After`` the server sent.
        refusal = _refusal_for_status(status_code, where, "", retry_after=delay)
        if refusal is not None:
            raise refusal from exc
        if status_code >= 500:
            # Retryable for the same reason any 5xx is: the venue said it could
            # not answer, and what it put in the body does not change that.
            raise ServerError(f"{where}: HTTP {status_code}: {exc}") from exc
        raise FormatChangeError(f"{where}: {exc}") from exc
    except ValueError:
        is_json = False

    if not is_json:
        lowered = stripped[:20_000].lower()
        refusal = _marker_refusal(where, lowered, json_body=False)
        if refusal is not None:
            raise refusal
        refusal = _refusal_for_status(
            status_code, where, " without a parseable body", retry_after=delay
        )
        if refusal is not None:
            raise refusal
        if status_code >= 500:
            raise ServerError(f"{where}: HTTP {status_code}")
        if status_code >= 400:
            raise HttpStatusError(f"{where}: HTTP {status_code}")
        kind = "HTML" if _HTML_HINT.match(stripped) else f"content-type {content_type!r}"
        raise NotJsonError(
            f"{where}: expected JSON, got {kind} ({len(body)} bytes) from {url or 'unknown url'}"
        )

    # A refusal served *as JSON at HTTP 200* is otherwise invisible: it decodes,
    # so nothing above fires, and the shape checks downstream report it as a
    # format change — which sends someone looking for a renamed field that was
    # never there.  Scanned only while the body is small enough to be an error
    # envelope; the bound is stated so "we checked" stays a precise claim.
    if len(stripped) <= MAX_JSON_SCAN_BYTES:
        refusal = _marker_refusal(where, stripped.lower(), json_body=True)
        if refusal is not None:
            raise refusal

    refusal = _refusal_for_status(status_code, where, f": {stripped[:200]}", retry_after=delay)
    if refusal is not None:
        raise refusal
    if status_code >= 500:
        raise ServerError(f"{where}: HTTP {status_code}: {stripped[:200]}")
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
