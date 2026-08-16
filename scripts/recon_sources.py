#!/usr/bin/env python3
"""Capture sanitized anonymous first-party sportsbook application traffic.

This is a manual discovery tool.  Output goes under ignored ``data/research``
and does not validate or register a source.  A discovered payload must still be
implemented in an adapter, stored as a normal RawResponse, and replay-tested.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings
from src.egress import (
    EgressDetectionError,
    confirm_unchanged_egress,
    detect_egress,
    load_detection,
)
from src.sources.browser import BrowserSession, PageObservation
from src.sources.research import (
    PROFILES,
    profile,
    safe_request_headers,
    safe_response_headers,
    sanitize_body,
    sanitize_url,
    write_observations,
)
from src.sources.transport import ImpersonatedSession, proxy_url


@dataclass(frozen=True)
class _RequestShape:
    url: str
    method: str = "GET"


def _detect(state: str, proxy: str | None) -> str:
    client = ImpersonatedSession(timeout=settings.HTTP_TIMEOUT, proxy=proxy)
    try:
        try:
            detected, _provider = detect_egress(client)
        except EgressDetectionError as exc:
            # The provider is down, not the egress.  Measured 2026-08-15:
            # ipapi.co answers this client with a Cloudflare "Just a moment..."
            # interstitial (HTTP 403), and passing that is defeating a challenge
            # — out of scope permanently.  With no fallback that failure blocked
            # every recon capture outright.
            #
            # So fall back to *continuity*, which is a weaker question with a
            # stronger answer: if the public IP still hashes to the fingerprint
            # of a stored detection, we are on the address that detection
            # verified, and its state still describes us.  It cannot invent a
            # state — an unrecognized address returns None and we re-raise.
            stored = load_detection(settings.EGRESS_STATE_PATH)
            confirmed = (
                confirm_unchanged_egress(client, stored) if stored else None
            )
            if confirmed is None:
                raise
            print(
                f"note: {type(exc).__name__} from the detection provider; "
                f"egress confirmed unchanged since {stored.detected_at} "
                f"instead (fingerprint {stored.egress_fingerprint[:12]}…)",
                file=sys.stderr,
            )
            detected = confirmed
    finally:
        client.close()
    if detected.state != state:
        raise RuntimeError(
            f"requested {state} but selected egress is {detected.state}; "
            "refusing first-party validation"
        )
    return detected.egress_fingerprint


def _single_observation(response, request: _RequestShape) -> PageObservation:
    return PageObservation(
        method=request.method,
        url=sanitize_url(str(response.url)),
        status_code=int(response.status_code),
        resource_type="document",
        request_headers=safe_request_headers({}),
        response_headers=safe_response_headers(response.headers),
        request_body=None,
        response_body=sanitize_body(response.text) or "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/recon_sources.py")
    parser.add_argument("source", choices=sorted(PROFILES))
    parser.add_argument("--state", default=settings.STATE)
    parser.add_argument(
        "--mode",
        choices=(
            "http",
            "browser-request",
            "page",
            "headed",
            "persistent",
            "mobile",
            "chrome",
            "chrome-persistent",
            "adapter",
        ),
        default="page",
    )
    parser.add_argument("--url", help="override the configured application entry URL")
    parser.add_argument("--wait-ms", type=int, default=10_000)
    parser.add_argument("--template-only", action="store_true")
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument(
        "--capture-dom",
        action="store_true",
        help="append the sanitized rendered DOM after page observation",
    )
    parser.add_argument(
        "--click-text",
        action="append",
        help=(
            "after bootstrap, click the first visible element containing this "
            "text; repeatable, and the clicks are made in order, so a sport "
            "and then a game on it are one capture rather than two page boots"
        ),
    )
    parser.add_argument(
        "--then-hash",
        help=(
            "after bootstrap, set location.hash to this route — the only way "
            "into a hash-routed app, whose router reads the fragment once "
            "during a boot that has not happened when the page load resolves"
        ),
    )
    parser.add_argument("--output", type=Path, default=Path("data/research"))
    parser.add_argument(
        "--raw-output",
        type=Path,
        help="adapter mode only: additionally write genuine RawResponse envelopes",
    )
    args = parser.parse_args(argv)
    if args.raw_output is not None and args.mode != "adapter":
        parser.error("--raw-output requires --mode adapter")

    state = args.state.strip().upper()
    try:
        configured = profile(args.source, state)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    selected_proxy = proxy_url(state)
    fingerprint = "UNVALIDATED"
    if not args.template_only:
        try:
            fingerprint = _detect(state, selected_proxy)
        except Exception as exc:  # noqa: BLE001 - one concise diagnostic line
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    target = args.url or configured.app_url
    responses = []
    websockets = []
    captured_raws = []
    adapter_detail = ""
    session = None
    failure: str | None = None
    try:
        if args.mode == "adapter":
            if args.source not in {"bet365", "caesars", "hardrock", "thescore"}:
                raise ValueError(f"{args.source} has no first-party adapter yet")
            from src.sources._common import Tier
            from src.sources.registry import descriptor_for_state

            source = descriptor_for_state(state, args.source).build(
                leagues=("MLB",), timeout=settings.HTTP_TIMEOUT
            )
            session = source
            raws = source.fetch_raw(tier=Tier.CORE)
            captured_raws = raws
            outcome = source.parse(raws)
            responses = [
                PageObservation(
                    method="CAPTURED",
                    url=sanitize_url(raw.url),
                    status_code=raw.status_code,
                    resource_type=raw.endpoint,
                    request_headers={},
                    response_headers=safe_response_headers(raw.headers),
                    request_body=None,
                    response_body=sanitize_body(raw.body) or "",
                )
                for raw in raws
            ]
            adapter_detail = (
                f"; parser={len(outcome.quotes)} quote(s), "
                f"{len(outcome.rejections)} rejection(s)"
            )
        elif args.mode == "http":
            session = ImpersonatedSession(
                timeout=settings.HTTP_TIMEOUT,
                proxy=selected_proxy,
            )
            response = session.get(target)
            responses = [_single_observation(response, _RequestShape(target))]
        else:
            persistent_dir = None
            if args.mode in {"persistent", "chrome-persistent"}:
                persistent_dir = Path("data/browser_profiles") / args.source / state
            session = BrowserSession(
                timeout_ms=settings.HTTP_TIMEOUT * 1000,
                proxy=selected_proxy,
                headless=args.mode not in {"headed", "chrome", "chrome-persistent"},
                persistent_dir=persistent_dir,
                device_name="iPhone 15" if args.mode == "mobile" else None,
                channel="chrome" if args.mode.startswith("chrome") else None,
            )
            if args.mode == "browser-request":
                response = session.get(target)
                responses = [_single_observation(response, _RequestShape(target))]
            else:
                def include(url: str, resource_type: str) -> bool:
                    if args.include_all:
                        return resource_type in {"xhr", "fetch", "document", "script"}
                    return resource_type in {"xhr", "fetch"} and any(
                        host in url for host in configured.api_hosts
                    )

                responses, websockets = session.observe_page(
                    target,
                    wait_ms=args.wait_ms,
                    include=include,
                    click_text=args.click_text,
                    then_hash=args.then_hash,
                )
                if args.capture_dom:
                    responses.append(session.dom_snapshot())
    except Exception as exc:  # noqa: BLE001 - evidence is the error class/message
        # Recorded, then written out below rather than returned on immediately.
        # This used to `return 1` here, above `write_observations`, so a failure
        # anywhere in the capture threw away every response already collected —
        # and against a metered egress those responses are the expensive part.
        # The failure is itself a finding and rides in the manifest as a row.
        failure = f"{type(exc).__name__}: {exc}"
        print(f"error: {failure}", file=sys.stderr)
        responses.append(
            PageObservation(
                method="ERROR",
                url=sanitize_url(target),
                status_code=0,
                resource_type="capture",
                request_headers={},
                response_headers={},
                request_body=None,
                response_body=sanitize_body(failure) or "",
            )
        )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - research output is already written
                pass

    manifest = write_observations(
        args.output,
        source=args.source,
        state=state,
        transport=args.mode,
        egress_fingerprint=fingerprint,
        responses=responses,
        websockets=websockets,
    )
    raw_detail = ""
    if args.raw_output is not None:
        from src.raw_store import RawStore

        raw_store = RawStore(args.raw_output)
        written = [raw_store.write(raw) for raw in captured_raws]
        raw_detail = f"; raw_envelopes={len(written)} under {args.raw_output}"
    # An exception is no longer the only way a capture fails.  ``observe_page``
    # deliberately turns a dead navigation, a missed click and a crashed page
    # into ``status_code=0`` diagnostic rows instead of letting them propagate,
    # so that the traffic already paid for survives — but that also means the
    # only signal left here was the exception that no longer arrives.  A run
    # whose navigation never completed was printing "EGRESS VERIFIED" and
    # exiting 0 with a manifest full of nothing.  Judge the rows instead.
    answered = [r for r in responses if r.status_code > 0]
    # ``ERROR`` is deliberately not in this set: the only row carrying it is
    # appended by the ``except`` above, which has already assigned *failure*, so
    # the guard below never reaches this list for that case.
    blocking = sorted(
        {r.method for r in responses if r.method in {"NAVIGATE", "CAPTURE"}}
    )
    if failure is None and (blocking or not answered):
        failure = (
            f"{', '.join(blocking)} diagnostic recorded"
            if blocking
            else "no response was answered"
        )
    label = "UNVALIDATED" if args.template_only else "EGRESS VERIFIED"
    if failure is not None:
        label = f"INCOMPLETE ({label})"
    print(
        f"{label}: {args.source} {state}; "
        f"{len(answered)}/{len(responses)} response(s) answered, "
        f"{len(websockets)} websocket(s); "
        f"manifest={manifest}{adapter_detail}{raw_detail}"
    )
    if failure is not None:
        print(f"  incomplete because: {failure}", file=sys.stderr)
    # Still a failed run for the shell, but the evidence is on disk either way.
    return 1 if failure is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
