# Operational scripts boundary

Follow `../docs/agent-guidelines.md`, `../docs/architecture.md`, `../docs/repository-map.md`, and `../docs/testing.md`.

Keep scripts as thin CLIs over reusable `src/` behavior. Live probes and egress detection are explicit diagnostics, never routine test substitutes. Do not print or persist raw public IPs, proxy credentials, full proxy URLs, or response bodies containing them. Repository checks must be deterministic, concise, and dependency-light.
