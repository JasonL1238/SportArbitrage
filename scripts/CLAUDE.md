# Operational scripts boundary

Root rules: `../AGENTS.md`. Depth: `../docs/architecture.md` and `../docs/testing.md`.

Keep scripts as thin CLIs over reusable `src/` behavior; `src/` never imports from here.
Live probes and egress detection are explicit diagnostics, never routine test
substitutes. Do not print or persist raw public IPs, proxy credentials, full proxy URLs,
or response bodies containing them. Repository checks must be deterministic, concise,
and dependency-light.
