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
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "atsgeotoken",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "deviceid",
        "_h",
        "ip",
        "license_key",
        "password",
        "proxy",
        "refresh_token",
        "session",
        "session_id",
        "sessionid",
        "sessiontoken",
        "sf_token",
        "sst",
        "token",
        "trader_session_token",
        "uid",
    }
)
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
    r'(?i)("?(?:_h|access_token|api_key|apikey|atsgeotoken|authorization|cookie|'
    r'deviceid|password|refresh_token|session_id|sessionid|sessiontoken|sst|token|'
    r'uid)"?\s*[:=]\s*)' r'("[^"\r\n]*"|[^&\s,}]+)'
)
_SECRET_QUERY_TEXT = re.compile(r"(?i)([?&](?:_h|token|uid)=)[^&\"'\s]+")
_FRAME_TOPIC_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:A_|S_)[A-Za-z0-9+/=_-]{16,}")
_LONG_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{96,}={0,2}")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith(("_api_key", "_access_token", "_license_key"))
        or normalized.endswith(("_password", "_secret", "_token"))
        or normalized.startswith(("authorization_", "credential_"))
    )


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
        return _IPV4.sub(_REDACTED, value)
    return value


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
        redacted = _SECRET_TEXT.sub(rf"\1{_REDACTED}", limited)
        redacted = _IPV4.sub(_REDACTED, redacted)
        return _SECRET_QUERY_TEXT.sub(rf"\1{_REDACTED}", redacted)
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
