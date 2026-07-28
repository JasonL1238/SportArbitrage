"""Raw-response storage and offline replay.

Every byte a source returns is written to disk before anything tries to
interpret it.  Parsing is then a pure function of a stored raw response, which
makes three things possible: a live failure can be reproduced offline without
touching the network, a parser change can be diffed against yesterday's
payloads, and a regression test can assert on real captured data instead of a
hand-written approximation of it.

An envelope stores the body verbatim as text alongside the request metadata, so
replay sees exactly what the collector saw — including the status code and
content type that the guards key off.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

#: Version written by this build.
ENVELOPE_VERSION = 2

#: Versions this build can still read.  Old captures stay replayable: a parser
#: upgrade must not orphan last month's raw data.  Version 1 predates response
#: header capture, so those envelopes replay with no headers.
SUPPORTED_ENVELOPE_VERSIONS = (1, 2)

_SLUG = re.compile(r"[^a-z0-9]+")

#: Headers worth keeping for auditing freshness and CDN behaviour.  Anything
#: resembling a credential is deliberately not stored.
_HEADER_DENYLIST = frozenset({"set-cookie", "authorization", "proxy-authorization"})


def _slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-") or "response"


@dataclass(frozen=True)
class RawResponse:
    """One captured HTTP response, sufficient to replay parsing offline."""

    source: str
    endpoint: str
    """Logical label for which call this was, e.g. ``"listview"`` or
    ``"betoffer:1028545498"``.  Part of the stored filename."""
    url: str
    status_code: int
    body: str
    fetched_at: datetime
    content_type: str | None = None
    request_params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    """Response headers, minus anything credential-like.  Kept so that
    ``age``/``x-cache`` can be audited when a payload comes back unchanged."""

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")

    @staticmethod
    def clean_headers(headers: Any) -> dict[str, str]:
        try:
            items = headers.items()
        except AttributeError:
            return {}
        return {
            str(key).lower(): str(value)
            for key, value in items
            if str(key).lower() not in _HEADER_DENYLIST
        }

    @property
    def cache_hints(self) -> dict[str, str]:
        """Whatever the response said about caching, for freshness auditing."""
        keys = ("age", "cache-control", "x-cache", "cf-cache-status", "date", "expires", "etag")
        return {key: self.headers[key] for key in keys if key in self.headers}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def ref(self) -> str:
        """Stable, human-readable reference recorded on every parsed row."""
        stamp = self.fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{self.source}/{stamp}/{_slug(self.endpoint)}/{self.sha256[:12]}"

    @property
    def byte_size(self) -> int:
        return len(self.body.encode("utf-8"))

    def to_envelope(self) -> dict[str, Any]:
        return {
            "envelope_version": ENVELOPE_VERSION,
            "source": self.source,
            "endpoint": self.endpoint,
            "url": self.url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "fetched_at": self.fetched_at.astimezone(UTC).isoformat(),
            "request_params": self.request_params,
            "headers": self.headers,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "body": self.body,
        }

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> RawResponse:
        version = envelope.get("envelope_version")
        if version not in SUPPORTED_ENVELOPE_VERSIONS:
            raise ValueError(
                f"unsupported raw envelope version: {version!r} "
                f"(supported: {list(SUPPORTED_ENVELOPE_VERSIONS)})"
            )
        raw = cls(
            source=envelope["source"],
            endpoint=envelope["endpoint"],
            url=envelope["url"],
            status_code=envelope["status_code"],
            body=envelope["body"],
            fetched_at=datetime.fromisoformat(envelope["fetched_at"]),
            content_type=envelope.get("content_type"),
            request_params=envelope.get("request_params") or {},
            headers=envelope.get("headers") or {},
        )
        recorded = envelope.get("sha256")
        if recorded and recorded != raw.sha256:
            raise ValueError(
                f"raw body does not match recorded sha256 for {raw.ref}: "
                f"stored {recorded[:12]}, computed {raw.sha256[:12]}"
            )
        return raw

    def json(self) -> Any:
        """Decode the body, or raise ``ValueError``."""
        return json.loads(self.body)


class RawStore:
    """Filesystem store for raw responses, partitioned by source and date."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, raw: RawResponse) -> Path:
        fetched = raw.fetched_at.astimezone(UTC)
        stamp = fetched.strftime("%Y%m%dT%H%M%SZ")
        filename = f"{stamp}_{_slug(raw.endpoint)}_{raw.sha256[:12]}.json"
        return self.root / raw.source / fetched.strftime("%Y-%m-%d") / filename

    def write(self, raw: RawResponse) -> Path:
        path = self.path_for(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Content-addressed by sha256, so an identical body re-fetched within
        # the same second is simply the same file.
        path.write_text(json.dumps(raw.to_envelope(), indent=1), encoding="utf-8")
        return path

    def read(self, path: str | Path) -> RawResponse:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        return RawResponse.from_envelope(envelope)

    def paths(self, source: str | None = None) -> list[Path]:
        base = self.root / source if source else self.root
        if not base.exists():
            return []
        return sorted(base.rglob("*.json"))

    def iter_responses(self, source: str | None = None) -> Iterator[RawResponse]:
        for path in self.paths(source):
            yield self.read(path)

    def latest(self, source: str, endpoint_prefix: str | None = None) -> RawResponse | None:
        candidates = [
            raw
            for raw in self.iter_responses(source)
            if endpoint_prefix is None or raw.endpoint.startswith(endpoint_prefix)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda raw: raw.fetched_at)
