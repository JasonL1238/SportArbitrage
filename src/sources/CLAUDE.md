# Source adapter boundary

Follow `../../docs/agent-guidelines.md`, `../../docs/architecture.md`, `../../docs/repository-map.md`, and `../../docs/testing.md`.

Scrape state odds from that state's own feed, pin every first-party route to the exact state, and give each bettable book one direct route or two same-licence third-party feeds — see the Scrape rules in `../../docs/agent-guidelines.md`.

Keep venue vocabulary and payload parsing in the venue adapter. Put only shared transport, pacing, retry, capture, and response-guard behavior in `_common.py`, `transport.py`, or `guards.py`. Parsing must be deterministic and free of I/O. Start with the venue's focused tests and the shared source contract.
