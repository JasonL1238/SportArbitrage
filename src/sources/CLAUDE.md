# Source adapter boundary

Follow `../../docs/agent-guidelines.md`, `../../docs/architecture.md`, `../../docs/repository-map.md`, and `../../docs/testing.md`.

Keep venue vocabulary and payload parsing in the venue adapter. Put only shared transport, pacing, retry, capture, and response-guard behavior in `_common.py`, `transport.py`, or `guards.py`. Parsing must be deterministic and free of I/O. Start with the venue's focused tests and the shared source contract.
