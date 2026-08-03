# Multi-state adaptation runbook

The collector has one jurisdiction-aware process for **IL** and **PA**. Illinois
is the live baseline; Pennsylvania is a routed template until the registered
adapters pass from a detected PA egress. This document is the operating runbook
and extension checklist; do not invent a second state-specific process.

## Goals

1. **Select** the intended state with `ODDS_STATE` (default `IL`) or use the
   live collector's `--auto-state` launcher.
2. **Detect** the effective egress manually with `scripts/detect_state.py`, or
   let that launcher perform the same check before collection.
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
- **One primary stakeable state per run** for retail books that are
  licence-bound. Extra validated states stay in the registry for travel /
  proxy / future moves; they are not all scraped in parallel unless each has
  its own exit IP.
- A geo wall is a transport problem (`ODDS_HTTP_PROXY`), not a reason to delete
  an adapter.

## Implemented baseline

`src/jurisdictions.py` owns the typed routes. `ODDS_STATE` is normalized to
uppercase, defaults to `IL`, and refuses unknown states through the ordinary
bad-settings path. The odds and promo registries retain stable keys while
binding active-state constructor values. Every odds and promo run persists the
selected jurisdiction.

| Venue | IL | PA |
|---|---|
| FanDuel | validated `il` | template `pa` |
| BetRivers | validated `rsiusil` / `US-IL` | template `rsiuspa` / `US-PA` |
| BetMGM | validated IL host/subdivision | template PA host/subdivision |
| DraftKings | validated `US-IL-SB` | template `US-PA-SB` |
| Caesars | legacy NJ route, warned | template `locations/pa` |
| Hard Rock | legacy NJ route, warned | unavailable; no PA route is invented |
| Promos (FD / BR) | one IL region/landing | one PA region/landing |

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

- Seed with **IL** (and **NJ** for Caesars/Hard Rock if that is what answers).
- When PA / MD / … is validated, **append** rows; never overwrite IL out of
  existence.
- Unknown state → fail loudly with “add template + run probe,” do not guess
  hostnames.

### 2. Active state + detection

Settings and operational records:

| Knob | Role |
|---|---|
| `ODDS_STATE` | Explicit primary state (`IL` or `PA`); default `IL`. |
| `ODDS_EGRESS_STATE_PATH` | Privacy-reduced detection record under `data/` by default. |
| `ODDS_PROBE_CACHE_PATH` | Separate SQLite live-probe cache. |
| `ODDS_PROBE_TTL_DAYS` | Fresh-`ok` TTL; default 60 days. |

Detection answers “where are we *effectively*?” — physical presence or proxy
exit. Ordinary collection remains deterministic from `ODDS_STATE`; passing
`--auto-state` opts into a fresh lookup and relaunches the collector with the
detected IL/PA state. `ipapi.co` is tried first and `ipwho.is` is the fallback.
The public exit IP is necessarily disclosed to the provider but is reduced to a
SHA-256 fingerprint before local persistence.

```bash
python -m src.collector collect --auto-state --tier core
```

The launcher accepts only configured IL/PA results and fails closed on lookup
failure or any other state. It does not change states inside a running watch
loop and it does not run for replay, report, or read-only commands.

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
- Registry builds one stakeable retail config for `ODDS_STATE`.
- Promos use the same map (`x-sportsbook-region`, `.il.` / `.pa.` landings).
- Optional later: `ODDS_HTTP_PROXY_IL`, `ODDS_HTTP_PROXY_PA` if one process must
  hit two exits. Until then, one global proxy + one primary state is enough.

---

## Runbook: arriving in a new state (or new proxy exit)

Do this once per new `(physical state or exit IP)`. Re-running is only needed
when the cache misses or you pass `--force`.

### Step 0 — Record intent

```bash
export ODDS_STATE=PA          # or IL
# If books still refuse CA egress:
export ODDS_HTTP_PROXY=…      # residential exit in that licensed state
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
python -m src.collector --tier core   # or the project’s usual smoke entry
```

Confirm retail books produce rows, redundancy/distinctness still sane, and
reports do not hardcode `.il.` when primary is PA.

### Step 5 — Mark validated

Cache write is automatic on probe success. Optionally stamp
`ODDS_STATES=IL,PA` so tooling knows both are first-class.

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
| Action Network failovers | None | Not a jurisdiction substitute; watch parser mismatch |

---

## Delivery status

- Implemented: typed IL/PA map, validated `ODDS_STATE`, active-state odds and
  promo registries, run-state persistence, report hosts/warnings, provider-
  fallback detection, fail-closed `--auto-state` collection relaunch, cache
  TTL/force behavior, and template-only probes.
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
