"""Resolve which states a run collects: the operator's choice, then detection.

Detection is **advisory**.  It exists to pre-fill a choice nobody made, and to
give the dashboard something to compare a run against — not to overrule an
operator who has said where they are.  It used to be a gate: ``select_states``
validated the detected state before it looked at the requested ones, so
``--state IL`` could not rescue a run whose detection had answered ``CA``, and a
rate-limited HTTP lookup outranked a human typing a state.  See
``egress.DEFAULT_DETECTION_URLS`` for the measurement that prompted the change.

What replaces it is a labelling duty, not a gate.  A run may collect a state the
egress does not match — that is the operator's call, and it is how an
out-of-state board gets looked at deliberately — but the disagreement has to
reach the reader.  ``report._payload`` does that: it compares the stored
detection against the run's own jurisdiction and appends a jurisdiction warning
when they differ, which is why a fresh reading is persisted here even when it
names a state collection does not support.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Sequence

from src import settings
from src.egress import (
    DEFAULT_DETECTION_URLS,
    EgressDetection,
    detect_egress,
    is_recent,
    load_detection,
    save_detection,
)
from src.jurisdictions import JURISDICTIONS, normalize_state
from src.sources.transport import build_default_client


class StateSelectionError(RuntimeError):
    """No state was chosen and none could be inferred."""


@dataclass(frozen=True)
class StateSelection:
    #: ``None`` when the lookup failed and no usable record was stored.  Callers
    #: read ``detected_state`` rather than this, so an absent reading is an empty
    #: string at the boundary instead of an ``AttributeError``.
    detected: EgressDetection | None
    states: tuple[str, ...]
    provider: str
    #: Human-readable reason worth surfacing, empty when detection answered
    #: fresh.  The CLI prints it; the dashboard shows it beside the scrape.
    note: str = ""

    @property
    def detected_state(self) -> str:
        return self.detected.state if self.detected is not None else ""


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
    """Explicitly chosen states win; detection only fills an empty choice.

    An unsupported or missing ``detected_state`` is not an error on its own —
    it is only an error when nothing else names a state, because then there is
    nothing to collect.
    """
    chosen = list(requested_states(requested))
    detected = normalize_state(detected_state)
    if not chosen and detected in JURISDICTIONS:
        chosen.append(detected)
    # ODDS_STATE remains a compatibility input, and stays an *addition* rather
    # than a choice: the default IL must not make every PA/NJ/DC scrape collect
    # Illinois accidentally, and equally must not become the only state a run
    # collects when detection has already named the one the operator is in.
    if include_configured and "ODDS_STATE" in os.environ:
        configured = normalize_state(settings.STATE)
        if configured not in chosen:
            chosen.append(configured)
    if not chosen:
        supported = ", ".join(JURISDICTIONS)
        if detected:
            raise StateSelectionError(
                f"detected {detected}, which collection does not support, and no "
                f"state was chosen; choose one of {supported} explicitly "
                "(--state, or a state box in the dashboard)"
            )
        raise StateSelectionError(
            "automatic state detection is unavailable and no state was chosen; "
            f"choose one of {supported} explicitly (--state, or a state box in "
            "the dashboard)"
        )
    return tuple(chosen)


def detect_and_select(
    requested: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., object] = build_default_client,
) -> StateSelection:
    """Detect if we can, persist the reduced record, and resolve the states.

    A failed lookup is survivable in two ways, in order: the operator's own
    choice needs no detection at all, and a *recent* stored record can pre-fill
    one that was never made.  A stale record is deliberately not used — it is
    old enough to predate a move, and the honest outcome then is the refusal
    that asks for a state rather than a confident guess.
    """
    client = None
    detection: EgressDetection | None = None
    provider = ""
    note = ""
    try:
        client = client_factory(timeout=settings.HTTP_TIMEOUT)
        detection, provider = detect_egress(client, urls=DEFAULT_DETECTION_URLS)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        note = f"automatic state detection unavailable ({type(exc).__name__}: {exc})"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - a close failure must not mask the detection
                pass

    if detection is not None:
        # Persisted before the states are resolved, and persisted even when it
        # names an unsupported state: the reading is a fact about this machine,
        # and it is what the dashboard's jurisdiction warning compares a run
        # against.  Recording it only on the success path is what used to leave
        # a "detected CA" refusal with nothing written down.
        save_detection(settings.EGRESS_STATE_PATH, detection)
    else:
        stored = load_detection(settings.EGRESS_STATE_PATH)
        if stored is not None and is_recent(stored):
            detection = stored
            provider = "the stored egress record"
            note = (
                f"{note}; using the last detected state {stored.state} "
                f"from {stored.detected_at}"
            )

    states = select_states(detection.state if detection is not None else "", requested)
    return StateSelection(
        detected=detection, states=states, provider=provider, note=note
    )


__all__ = [
    "StateSelection",
    "StateSelectionError",
    "detect_and_select",
    "requested_states",
    "select_states",
]
