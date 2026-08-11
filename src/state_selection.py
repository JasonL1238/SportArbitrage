"""Resolve one detected state plus optional additional collection states."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Sequence

from src import settings
from src.egress import DEFAULT_DETECTION_URLS, EgressDetection, detect_egress, save_detection
from src.jurisdictions import JURISDICTIONS, normalize_state
from src.sources.transport import build_default_client


class StateSelectionError(RuntimeError):
    """The current egress could not be mapped to a supported state."""


@dataclass(frozen=True)
class StateSelection:
    detected: EgressDetection
    states: tuple[str, ...]
    provider: str


def requested_states(values: Sequence[str] | None) -> tuple[str, ...]:
    """Normalize/deduplicate explicit states while preserving their order."""
    result: list[str] = []
    for raw in values or ():
        state = normalize_state(raw)
        if state not in JURISDICTIONS:
            raise StateSelectionError(
                f"unknown state {raw!r}; choose from {', '.join(JURISDICTIONS)}"
            )
        if state not in result:
            result.append(state)
    return tuple(result)


def select_states(
    detected_state: str,
    requested: Sequence[str] | None = None,
    *,
    include_configured: bool = True,
) -> tuple[str, ...]:
    """Put the detected state first, then explicit/configured additions."""
    detected = normalize_state(detected_state)
    if detected not in JURISDICTIONS:
        raise StateSelectionError(
            f"detected {detected or 'no state'}, but collection supports only "
            f"{', '.join(JURISDICTIONS)}"
        )
    additions = list(requested_states(requested))
    # ODDS_STATE remains a compatibility input.  Only an explicitly supplied
    # value is an additional request; the default IL must not make every PA/NJ/DC
    # scrape collect Illinois accidentally.
    if include_configured and "ODDS_STATE" in os.environ:
        configured = normalize_state(settings.STATE)
        if configured not in additions:
            additions.append(configured)
    return (detected, *(state for state in additions if state != detected))


def detect_and_select(
    requested: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., object] = build_default_client,
) -> StateSelection:
    """Detect once, persist the reduced record, and resolve the batch states."""
    client = None
    try:
        client = client_factory(timeout=settings.HTTP_TIMEOUT)
        detection, provider = detect_egress(client, urls=DEFAULT_DETECTION_URLS)
    except Exception as exc:  # converted to one CLI/UI refusal
        raise StateSelectionError(
            f"automatic state detection failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - a close failure must not mask the detection
                pass
    states = select_states(detection.state, requested)
    save_detection(settings.EGRESS_STATE_PATH, detection)
    return StateSelection(detected=detection, states=states, provider=provider)


__all__ = [
    "StateSelection",
    "StateSelectionError",
    "detect_and_select",
    "requested_states",
    "select_states",
]
