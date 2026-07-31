# Multi-state adaptation runbook

When you move to **IL, PA, MD**, or any other licensed US state, the collector
must keep working **there** without throwing away support for states already
proven. This doc is the plan and the checklist. Implementation follows it;
do not invent a second process.

## Goals

1. **Detect** which US state the current egress / session is in (or is configured for).
2. **Probe** every registered retail book against that state.
3. **Adapt** host / operator / segment / promo URLs so stakeable books answer.
4. **Keep** every previously validated state in the map — never delete IL (or PA,
   etc.) just because the primary moved.
5. **Skip** re-probing a `(source, state)` pair that already passed under the
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

## Current baseline (before this work)

There is no first-class location setting today. Jurisdiction is hard-coded per
adapter (mostly **IL**; Caesars / Hard Rock often **NJ**). Egress has been
Davis, CA. The only geo lever is `ODDS_HTTP_PROXY` / `HTTPS_PROXY` /
`ALL_PROXY`.

| Venue | Today’s jurisdiction encoding |
|---|---|
| FanDuel | `il` subdomain (`sbapi.il.…`) |
| BetRivers | Kambi `rsiusil` / `US-IL` |
| BetMGM | `www.il.betmgm.com`, `US-Illinois` |
| DraftKings | `US-IL-SB` |
| Caesars | `locations/nj` |
| Hard Rock | `segment=nj` |
| Promos (FD / MGM / BR) | IL landings / region headers |

That baseline stays as the **IL** (and **NJ** where used) entries in the
jurisdiction map after centralization.

---

## Architecture to add

### 1. Jurisdiction map (keep forever)

New module, e.g. `src/jurisdictions.py` (or equivalent), holding per-state
templates for every state-sensitive book:

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

Settings (names illustrative):

| Knob | Role |
|---|---|
| `ODDS_STATE` | Explicit primary state (`IL`, `PA`, `MD`, …). Wins when set. |
| `ODDS_STATES` | Optional comma list of *known* states kept in the map / cache. |
| Detection | If unset: derive from egress (proxy geo, or a cheap `ipinfo`-style
  lookup, or a dry probe against a known geo-gated host). Persist the result
  for the process. |

Detection answers “where are we *effectively*?” — physical presence or proxy
exit. The collector then builds retail adapters from that state’s templates.

### 3. Validation cache (skip if already proven)

Persist under `data/` (SQLite or JSON), keyed by:

```text
(source_key, state, egress_fingerprint) → status, probed_at, notes
```

- `egress_fingerprint`: hash of resolved exit IP and/or proxy URL (not the raw
  secret if avoidable — store a stable hash).
- Status: `ok` | `geo_restricted` | `blocked` | `parse_fail` | `untested`.
- **Skip rule:** if status is `ok` and `probed_at` is newer than a configured
  TTL (default: long, e.g. 30–90 days), do **not** re-run the live probe for
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
export ODDS_STATE=PA          # or IL, MD, …
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
| Hard Rock | `segment={st}` | Tree may answer while GraphQL needs proxy |
| Bovada / Cloudbet / 1xBet / Pinnacle / exchanges / Kalshi / Polymarket | Usually none | Treat as state-agnostic once ok |
| Action Network failovers | None | Not a jurisdiction substitute; watch parser mismatch |

---

## Suggested implementation order

1. **`src/jurisdictions.py`** — IL (+ current NJ Caesars/HR) templates only; no
   behavior change yet.
2. **`ODDS_STATE` + detection script** — print configured vs egress; no auto-edit
   of git.
3. **Wire FanDuel / BetRivers / BetMGM / DK / Caesars / Hard Rock + promos** to
   the map.
4. **Validation cache** in `data/` + `probe_sources.py --state` / `--force` /
   skip-if-ok.
5. **First real move** (PA or MD): run the runbook; append templates only where
   probes fail; document in `SOURCE_FEASIBILITY.md`.
6. **Tests** — template resolution for IL→PA/MD; distinctness still rejects dual
   RSIUS*; guards unchanged; no live network in CI.

Auto-editing adapters in-place for a new state is optional glue on top of
steps 1–4. Prefer: probe → human/agent applies map patches → re-probe → cache.
Do not have a silent process rewrite registry keys into mirror duplicates.

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
