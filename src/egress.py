"""Privacy-preserving public-egress detection and persistence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


# One provider, deliberately.  ``ipwho.is`` used to be the fallback here, on the
# reasoning that two credentials-free providers keep detection off one rate
# limiter — but ``detect_egress`` returns the FIRST answer, so a second provider
# is only a safety net if both agree, and these two did not.  Measured
# 2026-08-14 on the operator's own residential egress (fingerprint 34e56847c7ec,
# independently confirmed as Illinois by theScore's server-side region code):
# ipapi.co said IL, ipwho.is said CA / Los Angeles.  ipapi.co's free tier caps
# requests, so a busy afternoon exhausted it, the fallback answered, and
# collection refused with "detected CA".  A provider that is wrong about the IP
# is worse than no provider: it fails toward a confident wrong state rather than
# toward an honest gap.  Detection is now advisory (see
# ``state_selection.select_states``), so losing the net costs nothing that the
# operator's own state choice does not already cover.
#
# Callers may inject a different sequence for tests or an internal lookup
# service; a second URL is a genuine fallback only for a caller who has checked
# that it agrees.
DEFAULT_DETECTION_URLS: tuple[str, ...] = ("https://ipapi.co/json/",)


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
