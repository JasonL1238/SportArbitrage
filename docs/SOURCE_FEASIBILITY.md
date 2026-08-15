# Which sources are reachable, which are not, and why — the index

This is the record of what happened when every candidate source was actually asked. It
exists so that a wall is documented once rather than rediscovered every few months by
somebody writing an adapter for a host that has never answered.

The record lives in `docs/evidence/`, split so you read the one file your question is
about instead of the whole history. **Find your question here, open that one file.**

The split is by *question*, so a venue that turns up in several — Polymarket is measured
as two venues sharing one brand, as an adapter whose settlement rule had to be
corrected, and as an Illinois routing decision — is cheaper to find with
`grep -rni polymarket docs/evidence/` than through this table. History
before the split is under `git log --follow docs/SOURCE_FEASIBILITY.md`: the five files
below are new paths, so their own histories start at the split.

| Your question | File |
| --- | --- |
| Which Action Network book ids answer, on which endpoint version, with which markets? Did theScore Bet's Illinois id ever answer? Which parser changes explain a replay diff? | [`evidence/action-network.md`](evidence/action-network.md) |
| What did a state's licensed host return from matching egress? Why is this route `VALIDATED`, `TEMPLATE`, or `UNAVAILABLE`? Which feeds cover which book in PA/IL? | [`evidence/state-routing.md`](evidence/state-routing.md) |
| Is this exchange or prediction market reachable, and does it need credentials? Is this venue really a separate counterparty, or a front-end of one already registered? | [`evidence/exchanges-and-mirrors.md`](evidence/exchanges-and-mirrors.md) |
| What does a registered venue actually serve, what blocked it, and what does the whole set still fail to cover? | [`evidence/venues.md`](evidence/venues.md) |
| Why does this adapter compute charges/settlement the way it does, and which traps produced a plausible wrong number rather than an error? | [`evidence/adapter-lessons.md`](evidence/adapter-lessons.md) |

## Recording a new measurement

Every probe — including refusals — is written down with **date, state, egress, and the
literal result**. Append it under the file above that matches the subject, newest
section first, with the date in the heading. A refusal is evidence and is recorded in
exactly the same detail as a success; "it didn't work" without the status code, host,
and date is what causes the rediscovery this record exists to prevent.

Reproduce the current picture with:

```bash
python scripts/detect_state.py
python scripts/probe_sources.py --state IL
```

## Sections, and where each one now lives

- **`evidence/action-network.md`** — Action Network publishes its book catalogue
  (2026-08-08) · Three registered ids carry no odds on either endpoint (2026-08-08) ·
  theScore Bet's Illinois id answers (2026-08-10) · Deliberate parser changes and the
  replay verdict (2026-08-09, population corrected 2026-08-10) · One stale board retired
  for vintage coherence (2026-08-09) · The two endpoint versions are different
  catalogues (2026-08-08) · Two things to fix *before* the Pennsylvania recapture
  (2026-08-08) · v2 returns full-game only unless asked (2026-08-08)
- **`evidence/state-routing.md`** — Caesars: the session that closed it never
  reached a board, and a 7th stakeable book would have added nothing
  (2026-08-15) · The value question, asked before spending anything · Five
  things the 2026-08-12 section got wrong · Two detection
  providers disagreed about one
  egress, and the wrong one was winning (2026-08-14) · bet365 Illinois: the home page is HTTP, the
  board is not, and the egress is now blocked (2026-08-14) · What is true ·
  Three things the first version of this section got wrong · The routing grammar,
  decoded — and the one lead left · Fanatics has no anonymous board on the
  web, and the DNS map was not a route (2026-08-14) · theScore Bet is registered:
  Illinois' first direct route (2026-08-13) · theScore Bet: the anonymous surface, measured
  end to end (2026-08-13) · The anonymous flow, reproduced over plain HTTP
  (2026-08-13) · Three first-party host maps, measured by DNS
  with negative controls (2026-08-13) · Caesars is gated by an AWS WAF token, not by
  egress — and its odds are not on REST at all (2026-08-12) · Fanatics: closed — the
  odds board is behind a login (2026-08-12) · Pennsylvania first-party routes promoted
  (2026-08-08) · The links the promotion made wrong, and the suite that texted
  (2026-08-08) · The Pennsylvania recapture (2026-08-08T16:38Z) · Multi-state routing
  update (2026-08-03) · Pennsylvania routing status (2026-08-03) · Illinois re-probe
  (2026-08-03) · First-party acquisition follow-up (2026-08-04) · Redundant feed
  coverage · Pennsylvania: no book reaches the two-feed bar · First-party endpoint
  findings
- **`evidence/exchanges-and-mirrors.md`** — The offshore Polymarket is deregistered; the
  brand now names one venue (2026-08-13) · Polymarket US registered: four traps, and the
  two that would have priced a favourite at 20:1 (2026-08-13) · Polymarket US is not
  credential-gated — the
  2026-08-09 probe asked the wrong host (2026-08-12) · ProphetX and Novig: credentialed
  exchanges,
  API read (2026-08-09), adapters removed unregistered (2026-08-11) · Registration
  checklist: the nine places a new key has to appear · Polymarket US and Crypto.com Sports: both walls,
  measured (2026-08-09) · Polymarket: two venues share the brand (2026-08-08) · Mirrors:
  the trap that looks like progress · Front-ends of an order book already registered
  (2026-08-08)
- **`evidence/venues.md`** — Original California baseline: registered sources · Rate
  limits, honoured rather than routed around · Original California blocks · Previously
  blocked under plain `httpx`: reopen candidates · What this leaves · DraftKings
  Illinois: three route generations, 2026-08-14
- **`evidence/adapter-lessons.md`** — Two corrections to the original plan · Notes on
  charges · Notes on settlement · Traps the adapters had to be corrected for
  (newest first: BetMGM's `sourceName` convention, and its substring league table,
  both 2026-08-14)
