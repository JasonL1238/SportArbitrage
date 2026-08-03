# Test boundary

Follow `../docs/agent-guidelines.md`, `../docs/architecture.md`, `../docs/repository-map.md`, and `../docs/testing.md`.

Search for the production symbol and its closest focused test before opening broad regression files. Keep tests offline and deterministic: use tracked raw envelopes, never live sportsbook requests. Do not hand-edit or synthesize captures to make a source contract pass. Run the smallest relevant node or file first; use the documented full suite only after focused checks.
