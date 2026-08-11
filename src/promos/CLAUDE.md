# Promotions boundary

Root rules: `../../AGENTS.md`. Depth: `../../docs/architecture.md` and
`../../docs/testing.md`.

Keep promo schemas, adapters, enrichment, storage, and planning inside this package.
The promo database and lifecycle stay separate from odds persistence. Plans are
computed from current offers and quotes rather than stored, because prices move.
Start with `tests/test_promos.py` or `tests/test_promo_planner.py`.
