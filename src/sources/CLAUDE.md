# Source adapter boundary

Root rules: `../../AGENTS.md`. Depth: `../../docs/architecture.md`,
`../../docs/testing.md`, and — for what a venue has already answered — the file that
`../../docs/SOURCE_FEASIBILITY.md` names for it.

Adding a venue: `../../docs/INPUT_CONTRACT.md` states what the adapter must deliver and
what makes a venue a distinct counterparty rather than a second name for one already
registered. Read it before writing the adapter, not after the tests fail.

Scrape a state's odds from that state's own feed, pin every first-party route to the
exact state, and give each bettable book one direct route or two same-licence
third-party feeds — the three Scrape rules in `../../AGENTS.md` are the full statement.

Keep venue vocabulary and payload parsing in the venue adapter. Put only shared
transport, pacing, retry, capture, and response-guard behavior in `_common.py`,
`transport.py`, or `guards.py`. Reuse `_common.py`'s shared fixture type and quote
builder rather than restating them per venue. Parsing must be deterministic and free of
I/O. Start with the venue's focused tests and `tests/test_source_contract.py`.
