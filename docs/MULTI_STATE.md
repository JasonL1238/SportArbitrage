# Multi-state adaptation runbook

The collector supports batches for **IL**, **PA**, **NJ**, and **DC**. Illinois
is the live baseline; other exact-state routes remain templates until their
registered adapters pass from matching detected egresses.

## Goals

1. **Detect** the effective egress automatically at every live scrape start.
2. **Select** optional additional states with repeatable `--state` flags or the dashboard.
3. **Probe** every registered retail book against that state.
4. **Adapt** host / operator / segment / promo URLs so stakeable books answer.
5. **Keep** every previously validated state in the map — never delete IL (or PA,
   etc.) just because the primary moved.
6. **Skip** re-probing a `(source, state)` pair that already passed under the
   same egress fingerprint.

Offshore books, exchanges, and prediction markets are mostly jurisdiction-
agnostic. The work is concentrated on US retail: FanDuel, BetRivers (Kambi),
BetMGM, DraftKings, Caesars, Hard Rock, and their promo landings.

## Non-goals / hard rules

- **Do not register Kambi state mirrors as extra sources.** `rsiusil`,
  `rsiuspa`, `rsiusnj`, … are usually one counterparty wearing different
  licences. Swap the *active* operator for the user’s stakeable state; do not
  add `betrivers_pa` beside `betrivers_kambi` unless `src/distinctness.py`
  proves they disagree. See [`SOURCE_FEASIBILITY.md`](SOURCE_FEASIBILITY.md).
- **Do not remove a working state template** when adding another.
- **One state per child run.** Each route must be tagged for that exact state;
  a failure is never replaced by another state's route.
- Direct global venues remain actionable and are fetched once per batch.
  Republished global observations remain diagnostic-only.
- A geo wall is a transport problem (`ODDS_HTTP_PROXY`), not a reason to delete
  an adapter.

## Implemented baseline

`src/jurisdictions.py` owns typed routes and `src/state_selection.py` owns
detection/selection. Registries retain stable source keys. Every odds and promo
run persists jurisdiction, batch id, and route scope.

| Venue | IL | PA | NJ | DC |
|---|---|---|---|---|
| FanDuel | validated | template | template | template |
| BetRivers | validated | template | template | unavailable |
| BetMGM | validated | template | template | template |
| DraftKings | validated | template | template | template |
| Caesars | exact-state template | template | template | template |
| Hard Rock | exact-state template | unavailable | template | unavailable |
| Promos (FD / BR) | IL / IL | PA / PA | NJ / NJ | DC / unavailable |

State-agnostic feeds remain unchanged. Pennsylvania's Hard Rock absence is an
availability fact, not a reason to unregister the adapter or secondary feeds.

---

## Implemented architecture

### 1. Jurisdiction map (keep forever)

`src/jurisdictions.py` holds per-state templates for every state-sensitive book:

```text
STATE → {
  fanduel:        subdomain / region
  betrivers:      kambi operator + market   # one active tenant only
  betmgm:         host + subdivision + access id if needed
  draftkings:     site code (US-XX-SB)
  caesars:        locations/{xx}
  hardrock:       segment
  promos:         region header + landing hosts
}
```

- Seed with **IL** and keep any known NJ routes under NJ only.
- When PA / MD / … is validated, **append** rows; never overwrite IL out of
  existence.
- Unknown state → fail loudly with “add template + run probe,” do not guess
  hostnames.

### 2. Active state + detection

Settings and operational records:

| Knob | Role |
|---|---|
| `ODDS_STATE` | Compatibility/probe state; explicit values join a collection batch. |
| `ODDS_EGRESS_STATE_PATH` | Privacy-reduced detection record under `data/` by default. |
| `ODDS_PROBE_CACHE_PATH` | Separate SQLite live-probe cache. |
| `ODDS_PROBE_TTL_DAYS` | Fresh-`ok` TTL; default 60 days. |

Detection answers “where are we *effectively*?” — physical presence or proxy
exit. Every live scrape performs one lookup. `ipapi.co` is tried first and
`ipwho.is` is the fallback.
The public exit IP is necessarily disclosed to the provider but is reduced to a
SHA-256 fingerprint before local persistence.

```bash
python -m src.collector collect --tier core --state PA --state NJ
```

Collection accepts only IL/PA/NJ/DC and fails closed otherwise. Watch mode
detects once at startup. Replay, report, and read-only commands never detect.

### 3. Validation cache (skip if already proven)

Persist under `data/` (SQLite or JSON), keyed by:

```text
(source_key, state, egress_fingerprint) → status, probed_at, notes
```

- `egress_fingerprint`: SHA-256 of the resolved public exit IP. The raw IP,
  proxy credentials, and proxy URL are never persisted.
- Status: `ok` | `geo_restricted` | `blocked` | `parse_fail` | `untested`.
- **Skip rule:** if status is `ok` and `probed_at` is newer than a configured
  TTL (default: 60 days), do **not** re-run the live probe for
  that pair. Parser / schema unit tests still run; only live reachability is
  skipped.
- Force re-probe: `python scripts/probe_sources.py --state PA --force` (or
  equivalent).

When you move back to a state that already has `ok` rows under the current
egress fingerprint, the cache short-circuits the expensive live pass.

### 4. Wire adapters through the map

- Constructors take jurisdiction fields from the map / registry `config`, not
  module-level `DEFAULT_STATE = "il"` alone.
- Registry builds one exact-state retail config for every selected batch state.
- Promos use the same map (`x-sportsbook-region`, `.il.` / `.pa.` landings).
- `ODDS_HTTP_PROXY_IL`, `ODDS_HTTP_PROXY_PA`, `ODDS_HTTP_PROXY_NJ`, and
  `ODDS_HTTP_PROXY_DC` select exact-state exits in one process. They take
  precedence over the retained global `ODDS_HTTP_PROXY` fallback.

---

## Runbook: arriving in a new state (or new proxy exit)

Do this once per new `(physical state or exit IP)`. Re-running is only needed
when the cache misses or you pass `--force`.

### Step 0 — Record intent

```bash
export ODDS_STATE=PA          # or IL
# If books still refuse CA egress:
export ODDS_HTTP_PROXY_PA=…   # residential PA exit for PA routes
```

### Step 1 — Detect

```bash
python scripts/detect_state.py
# prints: configured=PA  detected_egress=PA  cache_hit_states=[IL]
```

Confirm configured and detected agree (or that the proxy exit is the licensed
state you intend). Mismatch → fix proxy / `ODDS_STATE` before probing.

### Step 2 — Probe only what is untested

```bash
python scripts/probe_sources.py --state PA
# skips (source, PA, egress) rows already status=ok
# writes new rows for the rest
```

Expect three buckets:

| Result | Action |
|---|---|
| `ok` | Cache it. No code change. |
| `geo_restricted` / CDN 403 | Proxy / exit problem first; only then host template. |
| Answers but parse empty / schema drift | Adapter edit for that state’s payload shape. |

Offshore / exchange / PM sources: probe once; if they already work from this
egress under any state, mark them state-agnostic and stop re-checking per move.

### Step 3 — Adapt (only failures)

For each failing **retail** source:

1. Look up the correct public host / operator for that state (browser Network
   tab on the licensed site, or existing peer templates in the jurisdiction
   map).
2. **Add** a state row in `src/jurisdictions.py` — do not delete IL/NJ.
3. Point the adapter at the map (if not already).
4. For BetRivers: change the **single** registered Kambi operator/market to the
   new state’s tenant if that is the stakeable book; keep IL in the map for
   later.
5. Re-probe that source only until `ok`.
6. Update promos landings / region headers for the same state.
7. Note the change in [`SOURCE_FEASIBILITY.md`](SOURCE_FEASIBILITY.md) (date,
   state, egress, result).

### Step 4 — Collector smoke

```bash
python -m src.collector collect --tier core
```

Confirm retail books produce rows, redundancy/distinctness still sane, and
reports do not hardcode `.il.` when primary is PA.

### Step 5 — Mark validated

Cache write is automatic on probe success. Promote a route from `template` to
`validated` in `src/jurisdictions.py` only after its real adapter returns
parser-clean quotes through matching egress.

Returning later to IL with the same egress fingerprint: Step 2 should report
cache hits and exit without a full live re-sweep.

---

## Per-book checklist (new state)

Use this when Step 2 fails for a venue.

| Book | What usually changes | Mirror / distinctness note |
|---|---|---|
| FanDuel | `sbapi.{st}.…`, promo `x-sportsbook-region` | Per-state hosts are normal; one source key |
| BetRivers | `rsius{st}` + `US-{ST}` | **Do not** dual-register with IL |
| BetMGM | `www.{st}.betmgm.com`, subdivision, access id | Access id may be state-specific |
| DraftKings | `US-{ST}-SB` | Often needs licensed-state proxy from CA |
| Caesars | `locations/{st}` | Same |
| Hard Rock | Add a segment only when licensed in that state | PA is explicitly unavailable; never invent `segment=pa` |
| Bovada / Cloudbet / 1xBet / Pinnacle / exchanges / Kalshi / Polymarket | Usually none | Treat as state-agnostic once ok |
| Action Network / VegasInsider / VSiN observations | None | Diagnostic only; not a jurisdiction substitute or executable leg |

---

## Delivery status

- Implemented: typed IL/PA/NJ/DC map, detected multi-state batches, exact-state
  odds/promo registries, global-fetch reuse, strict promo eligibility, batch/run
  persistence, state-matched promo planning, cache TTL/force behavior, and
  template-only probes.
- Still required: a legitimate PA egress run producing parser-clean quotes
  before PA route statuses are promoted from `template` to `validated`.
- Existing independent blocker: three retained Action Network source keys need
  real non-empty committed captures before the full offline contract suite can
  be green.

---

## Decision summary

| Question | Answer |
|---|---|
| New state? | Detect → probe untested → add templates → smoke → cache |
| Been here before (cache `ok`)? | Skip live probe |
| Keep old state? | Always — map and cache grow, they do not shrink |
| Two Kambi state URLs? | One registered source; swap active tenant |
| Still on CA Wi‑Fi? | Licensed-state `ODDS_HTTP_PROXY` as today |

When in doubt: **add a state row, prove it with a probe, leave every prior
validated state alone.**
