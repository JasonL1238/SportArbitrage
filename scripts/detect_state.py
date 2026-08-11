#!/usr/bin/env python3
"""Detect and record the current public egress jurisdiction.

Every live odds and promo scrape invokes the same detector automatically.  This
command is the explicit preflight used before jurisdiction probes.  The
persisted record contains only a two-letter state, a timestamp, and SHA-256 of
the public IP.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Keep the documented ``python scripts/detect_state.py`` invocation working.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings
from src.egress import DEFAULT_DETECTION_URLS, detect_egress, save_detection
from src.sources.transport import build_default_client


def main(argv: list[str] | None = None) -> int:
    refused = settings.refuse_bad_settings()
    if refused is not None:
        return refused
    parser = argparse.ArgumentParser(prog="python scripts/detect_state.py")
    parser.add_argument(
        "--url",
        action="append",
        help=(
            "injectable ipapi-style JSON lookup; repeat for fallback order "
            "(default: ipapi.co, then ipwho.is)"
        ),
    )
    parser.add_argument("--no-store", action="store_true", help="print but do not persist")
    args = parser.parse_args(argv)

    client = None
    try:
        client = build_default_client(timeout=settings.HTTP_TIMEOUT)
        detection, provider = detect_egress(
            client, urls=tuple(args.url or DEFAULT_DETECTION_URLS)
        )
    except Exception as exc:  # noqa: BLE001 - command should fail as one clean line
        print(f"error: egress detection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - a close failure must not mask the reading
                pass

    if not args.no_store:
        save_detection(settings.EGRESS_STATE_PATH, detection)
    match = detection.state == settings.STATE
    print(
        f"configured={settings.STATE} detected_egress={detection.state} "
        f"fingerprint={detection.egress_fingerprint[:12]} "
        f"provider={provider} "
        f"stored={'no' if args.no_store else settings.EGRESS_STATE_PATH}"
    )
    if not match:
        print(
            "error: configured jurisdiction and detected egress differ; "
            "do not validate sportsbook routes from this connection",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
