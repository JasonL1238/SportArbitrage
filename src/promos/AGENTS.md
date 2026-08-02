# Promotions boundary

Follow `../../docs/agent-guidelines.md`, `../../docs/architecture.md`, `../../docs/repository-map.md`, and `../../docs/testing.md`.

Keep promo schemas, adapters, enrichment, storage, and planning inside this package. The promo database and lifecycle remain separate from odds persistence. Plans are computed from current offers and quotes rather than stored. Start with `tests/test_promos.py` or `tests/test_promo_planner.py`.
