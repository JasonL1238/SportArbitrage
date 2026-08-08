"""Privacy-safe primitives for first-party application traffic research.

This module is intentionally separate from venue parsers.  Its artifacts help
discover a public endpoint, but collection adopts an endpoint only after the
payload is understood, captured as a normal :class:`RawResponse`, and replayed
offline.  Research output is ignored runtime data, never a test fixture.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_BODY_CHARS = 2_000_000
_REDACTED = "[redacted]"
#: Field names whose value never reaches a manifest.
#:
#: ``access_key``, ``secret_key`` and ``client_id`` are here because the suffix
#: rules below miss them by one character: ``"secret_key".endswith("_secret")``
#: is ``False``, and ``access_key`` matches neither ``_api_key`` nor
#: ``_access_token``.  Those three are the exact field names the credentialed
#: exchanges use — ProphetX issues an ``access_key`` / ``secret_key`` pair,
#: OAuth venues a ``client_id`` / ``client_secret`` one — so a recon session
#: against them would have written live keys to ``data/research/`` in cleartext.
#: Add the literal name whenever a venue coins one; a near-miss on a suffix rule
#: fails open, and failing open here means a secret on disk.
_SENSITIVE_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "atsgeotoken",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "api_secret",
        "bearer",
        "consumer_key",
        "hmac",
        "jsessionid",
        "jwt",
        "phpsessid",
        "set_cookie",
        "sid",
        "signature",
        "client_id",
        "client_secret",
        "cookie",
        "credential",
        "deviceid",
        "_h",
        "ip",
        "license_key",
        "password",
        "private_key",
        "proxy",
        "refresh_token",
        "secret",
        "secret_key",
        "session",
        "session_id",
        "sessionid",
        "sessiontoken",
        "session_key",
        "sf_token",
        "signing_key",
        "sst",
        "subscription_key",
        "token",
        "trader_session_token",
        "uid",
    }
)
#: Every sensitive name with its separators removed, so ``secret_key``,
#: ``secretKey``, ``SECRET-KEY`` and ``secretkey`` are one rule rather than four.
#:
#: A four-character floor, because ``_h`` squashes to ``h`` and ``h`` is how
#: compact odds JSON spells *home* — deriving this set without one redacted every
#: ``h`` in ten captured payloads.
_MIN_SQUASHED = 4
_SQUASHED_SENSITIVE_KEYS = frozenset(
    squashed
    for squashed in (key.replace("_", "") for key in _SENSITIVE_KEYS)
    if len(squashed) >= _MIN_SQUASHED
)

#: Separators a field name can wear, for splitting one into words.
_KEY_SEPARATORS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _key_words(key: str) -> list[str]:
    """Split a field name into words, whatever convention it is written in.

    ``secret_key``, ``secretKey``, ``SECRET-KEY`` and ``X-Session-Id`` all reduce
    to the same word list, which is what lets one rule cover every spelling.
    """
    return [word.casefold() for word in _KEY_SEPARATORS.split(key) if word]


_SAFE_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "origin",
        "referer",
        "user-agent",
        "x-requested-with",
    }
)
_SECRET_TEXT = re.compile(
    r'(?i)("?(?:_h|access_key|access_token|api_key|apikey|atsgeotoken|'
    r'authorization|client_id|client_secret|cookie|deviceid|password|private_key|'
    # Longest alternative first, for readability rather than correctness: the
    # engine backtracks when the following ``\s*[:=]`` fails, so ``secret`` before
    # ``secret_key`` produces identical output.  Ordering them anyway keeps the
    # next reader from having to work that out.
    r'refresh_token|secret_key|secret|session_id|sessionid|sessiontoken|sst|token|'
    r'uid)"?\s*[:=]\s*)' r'("[^"\r\n]*"|[^&\s,}]+)'
)
_SECRET_QUERY_TEXT = re.compile(
    r"(?i)([?&](?:_h|access_key|api_key|apikey|client_id|client_secret|"
    r"secret_key|token|uid)=)[^&\"'\s]+"
)
_FRAME_TOPIC_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:A_|S_)[A-Za-z0-9+/=_-]{16,}")
_LONG_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{96,}={0,2}")

#: A bearer credential and the token after it, as a *scheme plus value* pair.
#:
#: ``_SECRET_TEXT`` cannot reach this one: its value group stops at whitespace, so
#: on ``Authorization: Bearer eyJ…`` it matches the word ``Bearer`` and leaves the
#: token — redacting the scheme and stamping ``[redacted]`` beside the secret,
#: which reads as sanitized and is worse than leaving it plainly alone.
_BEARER_TOKEN = re.compile(r"(?i)\b((?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=-]{8,}")

#: A JSON Web Token in any of its three encodings.
#:
#: ``_LONG_BASE64`` misses every real one: it requires 96 unbroken characters of
#: standard base64, and a JWT is three shorter segments joined by dots — in
#: base64**url**, where ``-`` and ``_`` replace ``+`` and ``/`` and break the run.
_JWT = re.compile(r"\beyJ[A-Za-z0-9._~+/=-]{16,}")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


def _is_sensitive_key(key: str) -> bool:
    """Would this field's value be a credential?

    The name is split into words first, so one rule covers every convention:
    ``secret_key``, ``secretKey``, ``SECRET-KEY`` and ``X-Session-Id`` all reduce
    to the same word list.  A name is sensitive when its **trailing words** spell
    a sensitive name — which catches a venue-prefixed field, since a client that
    stores ProphetX's ``access_key`` as ``prophetx_secret_key`` must not fail
    open.

    **The word split is what keeps this from eating ordinary vocabulary, and it
    was learned the hard way.**  Matching the squashed name's *characters* rather
    than its words made every field ending in the letters of a sensitive name
    sensitive: ``possession`` ends in ``session`` and was redacted in 576 places
    across the captured Action Network and Pinnacle boards, and a Pinnacle
    participant key for a player named Schrauth ends in ``auth``.  A word
    boundary is the difference between a namespace and a coincidence.

    Two things deliberately not covered.  A sensitive name used as a *prefix* —
    ``secret_key_prophetx`` — passes, because a leading-word rule would swallow
    ``token_count`` and friends.  And there is no bare ``key`` or ``id`` word:
    ``source_key``, ``book_key``, ``market_key`` and ``event_id`` are this
    repository's own vocabulary.  Add the literal when a venue coins a name; both
    sides of the boundary are pinned in ``tests/test_source_research.py``.
    """
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE_KEYS:
        return True
    words = _key_words(key)
    # Every trailing run of words, longest first: ``x_session_id`` offers
    # ``sessionid`` and ``id``, and only the first is a credential.
    for start in range(len(words)):
        if "".join(words[start:]) in _SQUASHED_SENSITIVE_KEYS:
            return True
    return normalized.startswith(("authorization", "authorisation", "credential"))


@dataclass(frozen=True)
class ResearchProfile:
    source: str
    states: frozenset[str]
    app_url: str
    api_hosts: tuple[str, ...]
    unavailable_states: frozenset[str] = frozenset()


PROFILES: Mapping[str, ResearchProfile] = {
    "caesars": ResearchProfile(
        source="caesars",
        states=frozenset({"IL", "PA", "NJ", "DC"}),
        app_url="https://sportsbook.caesars.com/us/{state}/bet/",
        api_hosts=("caesars.com", "americanwagering.com"),
    ),
    "hardrock": ResearchProfile(
        source="hardrock",
        states=frozenset({"IL", "NJ"}),
        app_url="https://app.hardrock.bet/en-us/sports/",
        api_hosts=("hardrock.bet", "hardrocksportsbook.com"),
        unavailable_states=frozenset({"PA", "DC"}),
    ),
    "fanatics": ResearchProfile(
        source="fanatics",
        states=frozenset({"IL", "PA", "NJ", "DC"}),
        app_url="https://sportsbook.fanatics.com/",
        api_hosts=("fanatics.com",),
    ),
    "bet365": ResearchProfile(
        source="bet365",
        states=frozenset({"IL", "PA", "NJ"}),
        app_url="https://www.bet365.com/",
        api_hosts=("bet365.com", "bet365.us"),
        unavailable_states=frozenset({"DC"}),
    ),
}


def profile(source: str, state: str) -> ResearchProfile:
    """Resolve a supported source/state pair or refuse an invented route."""
    key = source.strip().lower()
    normalized = state.strip().upper()
    try:
        configured = PROFILES[key]
    except KeyError:
        raise KeyError(f"unknown research source {source!r}") from None
    if normalized in configured.unavailable_states:
        raise ValueError(f"{key} is not an online sportsbook in {normalized}")
    if normalized not in configured.states:
        raise ValueError(f"{key} has no configured research route for {normalized}")
    if "{state}" in configured.app_url:
        return ResearchProfile(
            source=configured.source,
            states=configured.states,
            app_url=configured.app_url.format(state=normalized.lower()),
            api_hosts=configured.api_hosts,
            unavailable_states=configured.unavailable_states,
        )
    return configured


def sanitize_url(url: str) -> str:
    """Redact credentials and secret-looking query parameters from a URL."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    query = urlencode(
        [
            (key, _REDACTED if _is_sensitive_key(key) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    path = parsed.path
    if "/cdn-cgi/challenge-platform/" in path:
        prefix = path.split("/cdn-cgi/challenge-platform/", 1)[0]
        path = f"{prefix}/cdn-cgi/challenge-platform/[redacted]"
    return urlunsplit((parsed.scheme, netloc, path, query, parsed.fragment))


def safe_request_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Keep only request headers useful for reproducing public application calls."""
    safe: dict[str, str] = {}
    for key, value in headers.items():
        normalized = str(key).lower()
        if normalized not in _SAFE_REQUEST_HEADERS:
            continue
        rendered = str(value)
        if normalized in {"origin", "referer"}:
            rendered = sanitize_url(rendered)
        safe[normalized] = rendered
    return safe


def safe_response_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Keep cache/content diagnostics while excluding identity and credentials."""
    allowed = {
        "age",
        "cache-control",
        "cf-cache-status",
        "content-encoding",
        "content-length",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "server",
        "vary",
        "x-cache",
    }
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in allowed
    }


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                _REDACTED
                if _is_sensitive_key(str(key))
                else _sanitize_json(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json(child) for child in value]
    if isinstance(value, str):
        # The shape-based rules, applied to the string *value* rather than to the
        # serialized document.  They must run here and not over the finished JSON:
        # ``_SECRET_TEXT`` matches up to a delimiter and is not quote-aware, so run
        # over ``{"wsUrl":"…?token=X","book_id":74}`` it eats the closing quote and
        # leaves a manifest that no longer parses.  Inside one value the boundaries
        # are the value's own.
        #
        # Without this the structured path was strictly weaker than the free-text
        # one: a secret reachable by shape rather than by key — a JWT under
        # ``"jwt"``, a socket URL carrying ``?token=…`` under ``"wsUrl"`` —
        # survived parsing and would not have survived being unparseable.
        return _redact_value(value)
    return value


def _redact_value(text: str) -> str:
    """Shape-based redaction of one JSON string value.

    Bearer and JWT run **before** the delimiter-terminated rules, which would
    otherwise consume the scheme word and leave the token beside a ``[redacted]``
    that makes the line look handled.
    """
    redacted = _IPV4.sub(_REDACTED, text)
    redacted = _BEARER_TOKEN.sub(rf"\1{_REDACTED}", redacted)
    redacted = _JWT.sub(_REDACTED, redacted)
    redacted = _SECRET_QUERY_TEXT.sub(rf"\1{_REDACTED}", redacted)
    redacted = _SECRET_TEXT.sub(rf"\1{_REDACTED}", redacted)
    return _LONG_BASE64.sub(_REDACTED, redacted)


def sanitize_body(body: str | None) -> str | None:
    """Redact common secrets and cap an observed request/response body."""
    if body is None:
        return None
    limited = str(body)[:MAX_BODY_CHARS]
    try:
        payload = json.loads(limited)
    except (TypeError, ValueError):
        pairs = parse_qsl(limited, keep_blank_values=True)
        if pairs and urlencode(pairs) == limited:
            return urlencode(
                [
                    (key, _REDACTED if _is_sensitive_key(key) else value)
                    for key, value in pairs
                ]
            )
        # One redactor for free text and for JSON string values, so the two paths
        # cannot drift apart again — the structured one was weaker for a while,
        # and the structured one is what every venue answers with.
        return _redact_value(limited)
    return json.dumps(_sanitize_json(payload), separators=(",", ":"), ensure_ascii=False)


def safe_websocket_frame(payload: Any) -> str:
    """Keep text protocol evidence, but only a digest for opaque binary frames."""
    if isinstance(payload, bytes):
        digest = hashlib.sha256(payload).hexdigest()
        return f"[binary bytes={len(payload)} sha256={digest}]"
    else:
        decoded = str(payload)
    if decoded.startswith(("100\x02", "101\x02")):
        return f"{decoded[:3]}[redacted-session]\x00"
    safe = sanitize_body(decoded) or ""
    safe = _FRAME_TOPIC_TOKEN.sub(lambda match: match.group(0)[:2] + _REDACTED, safe)
    return _LONG_BASE64.sub(_REDACTED, safe)


def body_summary(body: str) -> dict[str, Any]:
    """Small, stable diagnostic summary used in handoff-safe manifests."""
    encoded = body.encode("utf-8")
    summary: dict[str, Any] = {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    try:
        payload = json.loads(body)
    except ValueError:
        summary["json"] = False
        return summary
    summary["json"] = True
    if isinstance(payload, dict):
        summary["keys"] = sorted(str(key) for key in payload)[:40]
    elif isinstance(payload, list):
        summary["items"] = len(payload)
    return summary


def write_observations(
    root: Path,
    *,
    source: str,
    state: str,
    transport: str,
    egress_fingerprint: str,
    responses: Sequence[Any],
    websockets: Sequence[Any],
) -> Path:
    """Write sanitized ignored research artifacts and return the manifest path."""
    if egress_fingerprint != "UNVALIDATED" and re.fullmatch(
        r"[0-9a-f]{64}", egress_fingerprint
    ) is None:
        raise ValueError("egress_fingerprint must be a SHA-256 digest")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(root) / source / state / stamp
    directory.mkdir(parents=True, exist_ok=True)
    response_rows: list[dict[str, Any]] = []
    for index, observation in enumerate(responses, start=1):
        row = asdict(observation)
        row["url"] = sanitize_url(str(row.get("url", "")))
        row["request_headers"] = safe_request_headers(
            row.get("request_headers", {})
        )
        row["response_headers"] = safe_response_headers(
            row.get("response_headers", {})
        )
        row["request_body"] = sanitize_body(row.get("request_body"))
        body = sanitize_body(str(row.pop("response_body", ""))) or ""
        suffix = "json" if body.lstrip().startswith(("{", "[")) else "txt"
        filename = f"response-{index:03d}.{suffix}"
        (directory / filename).write_text(body, encoding="utf-8")
        row["body_file"] = filename
        row["body"] = body_summary(body)
        response_rows.append(row)
    manifest = {
        "source": source,
        "state": state,
        "transport": transport,
        "captured_at": datetime.now(UTC).isoformat(),
        "egress_fingerprint": egress_fingerprint,
        "responses": response_rows,
        "websockets": [
            {
                "url": sanitize_url(str(socket.url)),
                "sent": [safe_websocket_frame(frame) for frame in socket.sent],
                "received": [
                    safe_websocket_frame(frame) for frame in socket.received
                ],
            }
            for socket in websockets
        ],
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


__all__ = [
    "PROFILES",
    "ResearchProfile",
    "body_summary",
    "profile",
    "safe_request_headers",
    "safe_response_headers",
    "sanitize_body",
    "sanitize_url",
    "safe_websocket_frame",
    "write_observations",
]
