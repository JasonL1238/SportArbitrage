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
from src.egress import detect_egress
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
        detected, _provider = detect_egress(client)
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
        help="after bootstrap, click the first visible element containing this text",
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
    try:
        if args.mode == "adapter":
            if args.source not in {"caesars", "hardrock"}:
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
                )
                if args.capture_dom:
                    responses.append(session.dom_snapshot())
    except Exception as exc:  # noqa: BLE001 - evidence is the error class/message
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
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
    label = "UNVALIDATED" if args.template_only else "EGRESS VERIFIED"
    print(
        f"{label}: {args.source} {state}; "
        f"{len(responses)} response(s), {len(websockets)} websocket(s); "
        f"manifest={manifest}{adapter_detail}{raw_detail}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
