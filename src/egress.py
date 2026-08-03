"""Privacy-preserving public-egress detection and persistence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


# Two independent, credentials-free providers keep automatic selection from
# depending on one rate limiter.  Callers may inject a different sequence for
# tests or an internal lookup service.
DEFAULT_DETECTION_URLS: tuple[str, ...] = (
    "https://ipapi.co/json/",
    "https://ipwho.is/",
)


@dataclass(frozen=True)
class EgressDetection:
    state: str
    detected_at: str
    egress_fingerprint: str


class EgressDetectionError(RuntimeError):
    """Every configured lookup provider failed or returned an invalid payload."""


def detection_from_payload(
    payload: Mapping[str, Any], *, detected_at: datetime | None = None
) -> EgressDetection:
    """Reduce an ipapi-style response to state, timestamp, and an IP hash."""
    state = str(
        payload.get("region_code")
        or payload.get("regionCode")
        or payload.get("state_code")
        or ""
    ).strip().upper()
    ip = str(payload.get("ip") or payload.get("query") or "").strip()
    if len(state) != 2 or not state.isalpha():
        raise ValueError("detection response did not contain a two-letter state code")
    if not ip:
        raise ValueError("detection response did not contain a public IP")
    when = detected_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return EgressDetection(
        state=state,
        detected_at=when.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        egress_fingerprint=hashlib.sha256(ip.encode("utf-8")).hexdigest(),
    )


def detect_egress(
    client: Any,
    *,
    urls: tuple[str, ...] = DEFAULT_DETECTION_URLS,
) -> tuple[EgressDetection, str]:
    """Detect through the first usable provider and return its URL.

    The response body is reduced immediately; it is never returned or stored.
    Provider failures are summarized without including response bodies, which
    may contain the public IP we deliberately avoid persisting.
    """
    if not urls:
        raise EgressDetectionError("no egress detection providers configured")
    failures: list[str] = []
    for url in urls:
        try:
            response = client.get(url, headers={"Accept": "application/json"})
            status = int(response.status_code)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            payload = json.loads(response.text)
            if not isinstance(payload, Mapping):
                raise ValueError("JSON response was not an object")
            return detection_from_payload(payload), url
        except Exception as exc:  # noqa: BLE001 - try the next independent provider
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    raise EgressDetectionError("; ".join(failures))


def save_detection(path: Path, detection: EgressDetection) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(detection), sort_keys=True) + "\n")
    temporary.replace(path)


def load_detection(path: Path) -> EgressDetection | None:
    try:
        payload = json.loads(Path(path).read_text())
        return EgressDetection(
            state=str(payload["state"]).upper(),
            detected_at=str(payload["detected_at"]),
            egress_fingerprint=str(payload["egress_fingerprint"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_recent(
    detection: EgressDetection,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(days=1),
) -> bool:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    try:
        recorded = datetime.fromisoformat(detection.detected_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = current.astimezone(UTC) - recorded.astimezone(UTC)
    return timedelta(0) <= age <= max_age


__all__ = [
    "DEFAULT_DETECTION_URLS",
    "EgressDetection",
    "EgressDetectionError",
    "detect_egress",
    "detection_from_payload",
    "is_recent",
    "load_detection",
    "save_detection",
]
