# State routing: what each licence actually answered

Per-state route evidence: what a licensed host returned from matching egress,
which routes were promoted on it, and which feeds cover which book.

## What bet365's pods supply by the hour, and the two leagues that never arrive — 2026-08-16

Before spending any request on widening this venue, the six stored bet365 runs were asked what
the existing surface already supplies and when. **Cost: zero requests** — this is the stored
database, read only. It changes what the next probe should aim at.

Distinct pregame events per league, per run. `CT` is Illinois local (UTC−5 in August), which is
the clock the carousel is keyed to:

| run | UTC | CT | MLB | MLS | NBA | WNBA | NFL | NHL | events | quotes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 18 | 23:25 | 18:25 | 2 | 0 | 6 | 2 | 0 | 0 | 10 | 60 |
| 20 | 23:39 | 18:39 | 2 | 0 | 6 | 1 | 0 | 0 | 9 | 54 |
| 21 | 01:06 | 20:06 | 2 | 8 | 6 | 0 | 0 | 0 | 16 | 72 |
| 23 | 01:18 | 20:18 | 2 | 8 | 6 | 0 | 0 | 0 | 16 | 72 |
| 24 | 02:37 | 21:37 | 14 | 6 | 6 | 3 | 0 | 0 | 29 | 156 |
| 25 | 04:24 | 23:24 | 15 | 4 | 6 | 3 | 0 | 0 | 28 | 156 |

Four readings, and the last is the one that matters:

* **MLB's 2 → 15 is the next day's card arriving, not more of today's.** It climbs as the
  evening's games start and the strip rolls over. The surface is same-day, so its useful hour
  is late.
* **MLS occupies a window**, 8 / 8 / 6 / 4 across 20:06 → 23:24 CT, and is absent entirely at
  18:25.
* **NBA is a constant 6 and is not inventory.** Those are October opening-night fixtures that
  no other book prices, so they can never pair into an arbitrage. bet365's *arbitrageable*
  supply is 22 events at its best hour and 4 at its worst.
* **NFL and NHL are 0 in every run, at every hour.** Not thin — absent. NFL is 137 events
  priced by 11 books on the same slate; NHL is 33 across 8. No collection schedule reaches a
  league the surface never carries.

**A gap in the existing comparison, stated plainly.** Both multi-source runs (21 and 23) landed
at 20:0x CT — bet365's *worst* MLB hour. Every published "bet365 vs the field" number therefore
compares this venue at its trough against everyone else at an arbitrary point. bet365 has never
been measured against the field at its own best hour, and any future comparison must be
hour-matched to ~21:30–23:30 CT or it is measuring the clock.

**Decision this supports:** the per-sport probe is still worth its budget, and for a reason
independent of everything above. Scheduling can recover MLB and MLS, because the pods do carry
them. Nothing recovers NFL or NHL, because in six runs across five and a half hours they never
appeared once — which is precisely what a sport-addressed surface would fix.

### The per-sport shelf does not answer either — measured with a closing control

`/splashcontentapi/getsplashpods` was the best remaining HTTP candidate and it returned
nothing. **Cost: 3 requests of an authorised 6**; the stop rule fired on the first empty body
and no token variant was tried.

| | request | result |
| --- | --- | --- |
| R1 | `/pullpodapi/gethomepagepods` `pd=#HO#COL1#` | 200, **70,852 B**, coupon present |
| R2 | `/splashcontentapi/getsplashpods` `pd=#AS#B16#` | **200, 0 bytes**, sha256 `e3b0c442…b855` |
| R6 | R1 repeated, same session, ~3 s later | 200, **70,852 B**, sha256 identical to R1 |

Reproduce with a single `SourceClient("bet365", host_interval=2.0, proxy_state="IL")`, the
seven-key content group `lid=32 zid=0 cid=198 cgid=3 ctid=198` plus `pd` and `csid=28`, and
`expect_json=False`. Bytes under `data/research/bet365_probe/`.

**The closing control is the point of the exercise.** It converts an ambiguous empty into
"empty *while this host was still answering us normally on a known-good route, in the same
session, three seconds later*". This is not a block, not a dead egress, and not the
2026-08-14 wall returning.

What this does **not** establish: that the endpoint does not exist. A path that does not exist
on this host returns the identical signature — 200, `content-length: 0`, no content-type — so
the honest statement is that `getsplashpods` **was not made to answer with the parameter
groups we can observe**. Its manifest entry declares `q:"tzo"`, no capture anywhere on disk
contains a literal parameter for that group, and inventing a name is the fuzzing that cost
this egress 40 hours. So it was not invented, and the question stays open rather than closed.

**The `c` correlation now stands at 0 bodies from 3 distinct `c:1` endpoints**
(`splashcontentapi/competitions`, `matchmarketscontentapi/markets`, `splashcontentapi/getsplashpods`),
against 5 of 5 for every endpoint that carries no `c` or is absent from the manifest
(`gethomepagepods`, `gethomepageadditionalpods`, `validcompetitions`, `allsportsmenu`,
`footerapi/sitecontent`). The mechanism is still unknown and the `/contentdata` prefix theory
stays withdrawn — but as a *predictor* the field is now 8 for 8, and any future candidate
should be checked against it before it is spent on.

The routing argument that justified this request was sound and is worth keeping: `o:760
/splashcontentapi/competitions` requires `K^:5`, so a bare `#AS#B16#` cannot match it and
falls to `o:765 getsplashpods` under first-match-wins. That reasoning was right about *where*
the token routes; it simply does not follow that the route serves anonymous callers.

### The operator's own browser sees no board either — the reproduction gap was not one

The browser branch of this work existed because the board was believed to render for a human
where it did not for our automation. **It does not render for a human either.** Checked
2026-08-16 by the operator, in their ordinary Chrome, on their own Illinois address, signed
out, navigating by clicking (All Sports → Baseball → MLB) rather than by URL:

* the content area is **empty** — homepage material only, no games and no prices;
* **zero** requests to any `*contentapi/*` endpoint, out of 203 requests in the page;
* the two `pullpodapi` shelves answered normally in the same session — 10.8 kB and 18.5 kB
  transferred, gzipped, which is the ~70 kB body the adapter reads. That control is what
  makes the negative interpretable: the session was healthy and the tooling was recording.

That eliminates the entire browser-fingerprint class at once, without spending a page boot on
it. Not the proxy (the operator used none), not the persistent profile, not `navigator.webdriver`,
not `Accept-Language`, and not the viewport — the operator's window was *narrower* than the
1280 our capture ran at, and widening it changed nothing.

**What this corrects.** The premise for the browser work was an operator report of seeing a full
league board with many games. That was a misreading of the homepage pods, which do carry a
respectable slate and are what this adapter already collects. `docs/evidence/` records a
near-identical misread once before — an unpriced coupon, every row `SU=1`, taken for a board.
Reproducing the observation *before* building against it is what this section is for, and it
cost one browser session.

**Consequence: the browser-backed collection path is closed, not deferred.** There is no
anonymous HTTP board to transport, so browser-as-transport has nothing to fetch, and
DOM-as-payload has nothing to read — the blank content area is blank in a real browser too.
What remains unexamined is the Diffusion push channel and a credentialed session; neither is
in scope here, and the first would be a large build against a protocol that has published
nothing across every socket lifetime captured.

### Tennis was in the pods all along, behind a third separator

The shelf that was being declined as `fixture_without_a_stated_host` carries `FD` after all.
It uses a **third** separator the parser was not reconstructing:

| separator | count in the additional pod | means |
| --- | --- | --- |
| `" v "` | 19 | `"<home> v <away>"` — soccer, host stated |
| `" @ "` | 8 | `"<away> @ <home>"` — US boards, host stated |
| `" vs "` | **10** | two names and **no host** — tennis |

The old reading was that "tennis rows carry no FD at all, correctly — there is no home side".
Half of that is true and the conclusion was not: the field is present, and the count of
declined rows matched the tennis shelf exactly, so the wrong diagnosis looked like a
confirmed one. This is the second time this venue's `FD` has cost a whole competition.

Four changes were needed, and only the first is the separator:

1. A `" vs "` branch meaning *no host stated*, refused where the league actually has one —
   `orient` would otherwise impose an order by participant key that the book never agreed to.
2. `book_home_key` set to the competitor bet365 listed first, because its coupon numbers the
   columns `1` and `2` against **its** ordering while `orient` sorts a hostless league by key.
   In the captured coupon the two happen to agree on all four fixtures, so nothing exercised
   the translation until a test flipped the listing.
3. **The group header is wrong about half its rows.** The shelf is headed `WTA1-R2` and
   carries the combined Cincinnati draw — Paul, Hurkacz, Lehecka, Berrettini and Shapovalov
   all sit under it with `L3=ATP1-R2` on their own price rows. Reading the header would file
   men's matches in the WTA. The league now comes from each price row's own `L3`.
4. A past bound on the start time. One captured fixture carries `FS=0` with a start 97
   minutes before the capture — a match on a court running late, which tennis produces
   routinely. "Not started" and a start time that has passed cannot both be published, and
   `tests/test_source_contract.py` catches it: settlement reads the start time, not the flag.

**Measured gain.** Re-parsing every stored run's own bytes with the widened parser:

| run | bet365 rows stored | re-parsed | lost | gained |
| --- | --- | --- | --- | --- |
| 18 | 60 | 97 | **0** | 37 |
| 20 | 54 | 76 | **0** | 22 |
| 21 | 72 | 84 | **0** | 12 |
| 23 | 72 | 86 | **0** | 14 |
| 24 | 156 | 170 | **0** | 14 |
| 25 | 156 | 174 | **0** | 18 |

Zero lost in every run: the widening is purely additive for this venue. Live run 26 collected
**37 events / 174 quotes** against run 25's 28 / 156, and the entire difference is tennis —
ATP 3 events and WTA 6, with MLB, NBA, WNBA and MLS unchanged. `replay --run 26: PASS`.

Tennis is worth having because it has counterparties: 7–11 books priced ATP/WTA/ITF on the
same slate, and their participant keys already agree with ours (`TENNIS-annablinkova` is
identical across eight books).

**Expected, and not a regression:** runs 18–25 now replay with invented rows — 12 to 37 each,
all tennis, all from bytes that verified against their recorded sha256. Runs 20 and 23
additionally show 49 lost rows apiece, and those are **not** from this change: they are
`cloudbet` first-five-innings spreads written as `0` where the parser reads `-0`, a
pre-existing defect that breaks whole-run replay for any run containing that venue.

### A price could be published on the wrong side once one fixture sat in two pods

Found while planning that widening, and fixed ahead of it. The side of a spread or two-way
moneyline is positional — the first price under a row index belongs to the competitor `FD`
names first — and the parser established that position by counting matching entries across
the *whole capture*.

That is correct for exactly as long as no event appears twice. The two homepage pods happen to
carry disjoint event ids (13 and 37), so it never fired. Add any second shelf carrying tonight's
game and the second pod's first spread sees a count of two, becomes `HOME`, and reads its line
from its own row — publishing **`HOME` at the away line, carrying the away price**. The dedup
key differs, so nothing culled it: the market went three-legged with a wrong-side leg in it.
That is a phantom-arbitrage shape, not a lost row.

Reproduced by refiling the committed homepage pod under a second endpoint with its spread lines
moved, then parsing both: one market came back
`[AWAY, HOME, HOME, HOME]`. The fix scopes the ordinal to the market column and clears it with
the column, which is the only scope the payload states — and counts a row's position before the
suspended/unreadable declines, so a suspended away leg no longer promotes the home leg into its
place. Guarded by `test_one_event_in_two_pods_keeps_every_spread_on_its_own_side`.

Identical cross-pod re-listings are now `skipped["same_price_in_two_pods"]` rather than
`duplicate_dedup_key` rejections — one row seen from two shelves is not something the adapter
failed to represent. The collapse is deliberately scoped to *different* endpoints, so two
identical prices inside one pod remain a rejection.

## bet365's anonymous app crashes before it ever requests a league board — 2026-08-16

The per-league board was the last open coverage question for this venue: bet365 collects
from two homepage pods while DraftKings collects a full slate. The question is **not
settled**, and this section is deliberately narrower than its first draft, which claimed a
negative the capture does not support. What is settled is *where* the wall is, and it is one
step earlier than anyone had looked.

**Cost: one browser capture and three composed GETs.** Nothing further was spent, and no
parameter was fuzzed — that is what cost this egress on 2026-08-14.

### The app never requested the board; it died on the menu that leads to it

Capture `data/research/bet365/IL/20260816T015741Z/` — `chrome-persistent`, Illinois egress
confirmed by continuity against fingerprint `34e56847c7ec`, 28 responses, 12 websockets,
anonymous throughout (the rendered nav still offers "Join / Log In"). Navigation was by
**click** — "Baseball", then "MLB" — because the 2026-08-15 attempt set `location.hash`
instead. That attempt reached the router (its final URL is the MLB coupon's) and issued four
content requests, but all four are the ordinary boot set every capture makes — both homepage
pods, `allsportsmenu`, `validcompetitions` — with nothing specific to the route it had
navigated to. Clicking was an attempt to get past that; it produced one additional request,
below.

The click added exactly one content request, and it is *not* the board:

```
record 24  GET /splashcontentapi/competitions?lid=32&zid=0&pd=%23AS%23B16%23K%5E5%23&…&csid=28
           → 200, 0 bytes, sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
record 25  CONSOLE error
           TypeError: Cannot read properties of undefined (reading 'getChildren')
               at getMarketButtons … at useStemButtonBarLinks … at t.SplashHeader …
record 28  DOM  #/AC/B16/C20525425/D48/E1096/F10/   → "Unable to display this content"
```

Read it in order. `/splashcontentapi/competitions` is the **baseball splash's competition
menu** — routing entry `o:760`, token type `AS`, renderer `cm`. The board is a different
service on a different token type: `/matchmarketscontentapi/markets`, `o:250`, token type
`AC`, renderer `mms`. The app asked for the menu, received an empty body, and **its own
renderer threw** inside `SplashHeader`. The rendered page then reads "Unable to display this
content" — most plausibly that crash's error boundary, given it follows the `TypeError` by
three records, though the capture does not tie the string to the exception directly. What is
certain either way: a `TypeError` in the venue's own render code is not a licence refusal.

**Requests to `/matchmarketscontentapi/markets` in this capture: zero.** The only request
ever made to that service from this repo is our own composed probe of 2026-08-15
(`20260815T225823Z`). The board's parameter set is therefore still unobserved — the app
never got far enough to send it.

### What that corrects, including in the first draft of this section

- **"The app sends the board request and receives zero bytes" is false**, and so is "no
  parameter set was missing." The app sent a *different* request, for a different service,
  on a different token type. Nothing here tests the board's parameters.
- **"A zero-byte 200 from a `c: 1` content API is its normal anonymous answer" is false.**
  bet365's own code crashes on that body. An application does not throw a `TypeError` on the
  answer it expects — an empty competitions payload is an abnormal response the client does
  not handle, which is evidence *against* treating the empty body as routine.
- The blockquote added to the section below, saying the app "was observed sending it," is
  withdrawn there too. The observation that paragraph asked for was **not** obtained.
- The earlier warning it leaned on — "200 with zero bytes means nothing" — survives intact
  and is the reason this draft is narrower than the last.

The verdict this supports is **blocked, not disproved**: an anonymous session cannot reach
its own league board because an upstream call returns empty and the client crashes on it.
Whether the board would answer a correctly-formed request is unknown, and this capture
cannot say.

### A second delivery channel exists, and it is not HTTP

The 12 websockets are worth recording. The subscribe frame on
`wss://premws-pt1.us.365lpodds.com/zap/` lists content as push topics, beside the homepage
pods this adapter already reads (`POD199470570C7HPC`, `POD199470574C7HPC`) — so at least
some board content is delivered over Diffusion rather than fetched.

The topic names appear to carry the same sport-class numbering the navigation tokens use —
`15175C16A_32`, `1535C16A_32`, `15235C16A_32` alongside `151995805415C12A_32`, where the
nav tokens spell baseball `B16` and football `B12`. That correlation is an inference from
the naming, not a decoded protocol: nothing here was parsed as Diffusion, and the frames
that arrived were session acknowledgements and `__time` heartbeats rather than content.
This does **not** establish that the league board is push-only — the app crashed before it
could ask for it either way. It establishes that a second channel exists and that a future
attempt has two places to look, not one.

The pull-pod endpoint does not generalize, and this part *is* a clean measurement. Three
paced requests, **control first so an empty answer would be a fact about the token rather
than about the session**:

| `pd` token | Result |
| --- | --- |
| `#HO#COL1#` — the adapter's own homepage token | **200, 50,172 bytes** |
| `#AS#B16#` — baseball splash, observed in this session's traffic | 200, **0 bytes** |
| `#AC#B16#C20525425#D48#E1096#F10#` — MLB board, from the app's address bar | 200, **0 bytes** |

Same endpoint, same session, seconds apart. `pullpodapi/gethomepagepods` is bound to `#HO#`.

**This one has no manifest under `data/research/`,** unlike every other measurement here: it
was a direct script rather than a `recon_sources.py` capture, deliberately, because that tool
builds a fresh session per invocation and re-detects egress each time — three invocations
would have been three different sessions plus three extra lookups, destroying the single
property the experiment was built on and doubling the request count against the host whose
egress was blocked on 2026-08-14. To reproduce: one `ImpersonatedSession` over
`proxy_url("IL")`, three GETs of `/pullpodapi/gethomepagepods` with
`bet365.BASE_PARAMS | {"pd": token, "csid": "28"}`, `time.sleep(1.5)` between, control first.
The control's body begins `F|PS;IT=#HO#COL1#PDCOL1#;CO=1;AD=1;|PD;CN=1;PT=996;…` — a real
pull-pod payload. Body sizes track the live slate, so the control's 50,172 bytes is not
expected to reproduce exactly; the 0 / non-0 split is the measurement.

### The routing manifest, decoded — and the prefix theory withdrawn

The manifest (`data/research/bet365/IL/20260815T225722Z/response-001.json`) decodes usefully
and is worth keeping:

| key | reading | confidence |
| --- | --- | --- |
| `t` | the `pd` token's **first segment** — `#AS#…` → `t:"AS"`, `#AC#…` → `AC`, `#FO#` → `FO` | measured against four tokens |
| `m` | a rule over the token's remaining segments (`B:` class, `D:` template, `K^:` tab) | measured |
| `e` | the endpoint path | measured — served requests are at exactly this path |
| `p` | a short renderer/page code (`cm` competition menu, `mms` markets, `lhs` left nav) | inferred from the names |
| `o` | priority; first match wins | inferred |
| `q` | a **named parameter group**, not literal params: the served competitions request carries `csid=28` and no parameter called `csidex` at all | not decoded |
| `c` | present on 118 of 135 entries; the 17 without it include both endpoints that ever returned a body | meaning unknown |

`t` and `e` are what this section leans on, and they are the measured ones: they are why the
app's request is identifiably the `AS` competition menu and not the `AC` markets board.

The **prefix theory built on `c` is withdrawn.** Both endpoints that had ever returned a
real body (`leftnavcontentapi/allsportsmenu`, `footerapi/sitecontent`) are among the 17
without `c`, which made "`c: 1` endpoints take a path prefix we have not seen" the obvious
reading. `/splashcontentapi/competitions` is a `c: 1` entry, the app called it at its plain
path with **no** prefix, and it was routed and answered — with an empty body, but answered.
So `c` does not mark a prefix. What it does mark is still unknown, and the empty body it
returned is not evidence about the path in either direction.

### What this leaves, and what the next attempt should do

bet365 stays a real Illinois direct route at pull-pod scale, and its Illinois route was
promoted to `VALIDATED` on this evidence plus two clean runs. The homepage pods remain the
only anonymous HTTP surface **that has been made to answer** — which is not the same as the
only one that exists, and the difference is the whole point of this rewrite.

Three places a future attempt can look, cheapest first:

1. **A splash whose competition menu is not empty.** The crash was in `SplashHeader` on an
   empty `o:760` payload. Another sport's splash, or the same one at a different hour, may
   return a body and let the app proceed to the board — at which point the board request is
   observed for free.
2. **The `AC` route without going through a splash.** Every attempt so far reached the
   board route via the menu. The crash happens upstream of it.
3. **The push channel**, which needs the Diffusion protocol rather than a request.

What should *not* happen is another round of parameter variants against
`/matchmarketscontentapi/markets`. Nothing learned here narrows its parameter set, and that
is the fuzzing that cost this egress on 2026-08-14.

### bet365 gets no event deep link, and that is a measurement

`src/betlinks.py` deliberately holds no `EVENT_URL` entry for this book. The address bar
mirrors the app's navigation token segment for segment —
`#/AC/B16/C20525425/D48/E1096/F10/` — and a fixture row's own token has the same shape,
`#AC#B16#C20525425#D19#E26475424#F19#I0#P951933#H1#`. The event id is one segment among
class, competition, template and group, and a `Quote` preserves none of the others. No
`{id}` template can address a bet365 event; reaching event precision is a schema change,
not a link-table addition. `#E` also means two different things in that grammar — the event
on a fixture row, a layout template on the league link (`E1096`) — so the obvious
pattern-match is wrong twice over.

### Link tables became state-aware, which is what a deep link needed first

Independent of bet365, `src/betlinks.py` now spells per-state hosts rather than banning
per-state grammars. `_Event` takes a `{host}` filled from `STATE_SITE`, verification for a
state-partitioned book is `verified_states` rather than one flag, and `bet_link` skips event
and league precision outright when no jurisdiction can fill the host — an ungoverned run
falls to `SITE` instead of naming a licence it cannot bet on. `betrivers_kambi`'s template
stopped hardcoding `il.betrivers.com`, which was the host its 2026-08-04 probe happened to
run from rather than a property of the grammar. `scripts/verify_betlinks.py` now takes the
probed state from the stored run and says so, so a `CONFIRMED` authorizes exactly one state.
The two guard tests that used to forbid these entries check their shape instead, and
`_check_link_tables` enforces those shape rules at import — a subset of what the tests
assert, since a test can also exercise `bet_link`'s behavior, which an import check cannot.

**A trap this nearly shipped, caught in review.** `{host}` is a *netloc*, and not every
state-partitioned book is partitioned by netloc: Caesars' four doors are one host with four
paths (`sportsbook.caesars.com/us/il/bet` … `/us/dc/bet`). A `{host}` template for Caesars
therefore resolves all four states to the same URL — the run-32 defect exactly — and a
netloc-to-netloc assertion *passes* it, because the netloc genuinely matches in every state.
Both the import check and the guard tests now require the resolved URLs to be as distinct as
the doors are, and to sit under each state's own door, so a path-partitioned book is refused
rather than silently collapsed.

## bet365 is registered: Illinois' second direct route, and its first over plain HTTP — 2026-08-15

Built on the reachability finding below. `src/sources/bet365.py` reads the pull-pod board
from `www.il.bet365.com` with the ordinary client — no browser, no cookie, no token — and
`_required("bet365", "bet365", …)` now names it Illinois' direct route
(`src/coverage.py:300`). PA is deliberately **not** promoted: there is no PA egress, and
`_check_direct_route` refuses a direct route the state cannot reach.

**Cost of the whole build: 2 plain GETs and 1 page load of reconnaissance, plus a
3-request adapter capture.** No page load was needed for the board itself.

### The per-league endpoint, and why the adapter does not use it

`/websiteroutingdatacontentapi/routingdata?v=3274933210` — referenced in every homepage
body, never previously captured — answers **200, 16,880 bytes, 135 routing entries**. Each
maps a navigation-token rule to a service, and the rule matching baseball's coupon
(`B:…16…` × `D:…48…`) resolves to **`/matchmarketscontentapi/markets`**. So the per-league
service is now named rather than guessed.

It is not what this adapter reads, for a measured reason. Composed from the manifest's own
endpoint, the app's own MLB token and the parameter set every other call carries, it
answered **200 with a zero-byte body**. This file already records that reading as a trap —
"200 with zero bytes means the endpoint is real and the parameters are incomplete" *"means
nothing"* — so no parameter variants were tried; that is the fuzzing that cost this egress
on 2026-08-14. A page load navigating to the same coupon by hash route reached the router
(the final URL confirms `#/AC/B16/C20525425/D48/E1096/F10/`) and the app **issued no
content request at all**, exactly as `src/sources/browser.py` warns for this venue:
reaching the router is not the same as the route producing traffic.

**Left open, deliberately:** the per-league board is addressable and its service is known;
what is missing is the parameter set the app sends, and the only sanctioned way to learn it
is to observe the app sending it. The homepage pods carry a real coupon meanwhile.

> **Still open 2026-08-16, and now located.** A click-driven capture that day did *not*
> obtain this observation: the app never issued the board request at all. It requested the
> baseball splash's competition menu, received an empty body, and crashed in its own
> renderer before reaching the coupon. So the parameter set remains unobserved and this
> paragraph's plan is unchanged — but the thing to observe past is now the splash, not the
> board. See the topmost section.

### What the adapter collects

Both homepage pods (`#HO#COL1#` and `#HO#COL1#1#`). Against the committed fixtures that is
**114 quotes over 19 events across MLB, NBA, WNBA and NFL**, three markets each, **zero
rejections**.

The board is column-oriented and has no parent ids: a market group is followed by a fixture
column and one column per market, and `PZ` — the row index — is the only thing joining a
price to a game. The parser reports a price whose row the fixture column never emitted
under its own counter, separately from one belonging to an event it declined, because the
first is what a shifted index looks like and the second is routine. On the committed
capture the first is **zero**.

Three fields will produce plausible wrong rows if misread, and each has a test:

| Field | The trap |
| --- | --- |
| `OD` | A true fraction. `10/13` is 1.769, not 0.769 — and `11/10` would be 1.1, which is a perfectly plausible price. `src/normalize.py` had no fractional converter; `fractional_to_decimal` was added, and its docstring names the neighbouring helper that divides without the `1 +` |
| `HA` | Signed per selection on a spread, but carrying **no side at all** on a total, where over/under exists only in `HD`'s prefix |
| `OI` | Looks like an event id and is not one — it is replaced when a fixture goes in-play, so a run spanning first pitch would file one game twice. Identity comes from the `PD` token's `E<digits>` group |

Orientation is cross-checked against `FD`, a *different* field stating the same fixture as
`"<away> @ <home>"`. That check exists because this adapter shipped the bug once:
`src.events.orient` returns `(away, home)`, and unpacking it the other way round swapped
every fixture and inverted every spread while leaving the payload entirely valid. Caught
against the rendered page — MIA Marlins @ CIN Reds, spread +1.5 −210 / −1.5 +175, total
O 9.0 −110 / U 9.0 −110, money −102 / −108 — which the parser now reproduces exactly.

### Not committed, and why

The shell (`/`) is fetched to read `STATE_LOCALE:"USIL"`, which is the only place any of
this states its own licence — the pull-pod bodies carry no state marker at all, so the
route cannot otherwise verify itself. Its body carries an `SST` config value that could not
be shown to be per-deploy rather than per-session, so it is **redacted before the
`RawResponse` is constructed** and **excluded from the committed fixtures**. Only the two
pods are committed, and both were scanned for account, partner, bearer and email-shaped
strings before commit; the long base64 they contain is bet365's own `G+G/G2G3…` encoding
alphabet.

### bet365's clock disagrees on Pacific fixtures, and that is its own answer

The full run surfaced `start_time_disagreement`: three of nine fixtures **exactly +60
minutes** against a consensus of thirty-two feeds.

| fixture | bet365 | everyone else |
| --- | --- | --- |
| KC @ LAA | 2026-08-16T02:38Z | 01:38Z |
| TEX @ ATH | 2026-08-16T02:40Z | 01:40Z |
| MIN @ LVA (WNBA) | 2026-08-16T01:00Z | 00:00Z |

All three are **Pacific-timezone venues**; the six that agreed are not. Checked against the
stored bytes rather than inferred: bet365's own `BC` for KC @ LAA is `20260816023800`, and
`an_bet365` — Action Network's view *of bet365* — says 01:38. So the venue disagrees with
the republished view of itself.

**Nothing is subtracted in the adapter.** Correcting a venue towards consensus is inventing
data and would mask the next genuine change; the cross-source clock check owns this and
reports it as a warning. Recorded because the same finding will otherwise be re-diagnosed
as a parser fault. Every affected fixture still keyed to the same scheduling date and
reconciled normally, so event identity held — but an hour can cross that boundary, which is
the thing to watch.

### Acceptance

`collect --state IL --source bet365 --tier core --no-alert` → **60 quotes, 30 markets, 10
events**, no source-level error. `replay --run 18` → **PASS**.

> **Stale from 2026-08-16, deliberately.** `replay --run 18` is now **FAIL**, with 33
> invented rows and a row count of 60 → 93 — every one of them an MLS moneyline. Run 18 was
> collected before soccer's three-way was recovered, so today's parser reads three legs out
> of bytes it used to discard. That is a recorded parser change rather than a regression, and
> it is what the replay tool's own message describes: the stored raw bytes still verify
> against their sha256s, and the difference lies between the stored rows and the current
> reading of those verified bytes.

Full Illinois run: **`bet365: OK (54 quotes, 9 events, 3 requests, 316 KiB, 1500 ms)`**,
counted among the cross-book feeds for baseball and basketball, and **bet365's
`required_book_single_source` warning is gone** — only Caesars and Fanatics still carry it
in IL. Full suite green: 4,879 passed, 1 skipped.

The single-source run's three ERRORs (`insufficient_sources`,
`state_native_sources_below_two`, `required_book_missing: Circa`) are properties of
collecting one source alone, not of this one.

## bet365, second pass: a whole league recovered, and an inverted fixture caught by the slate rather than by the parser — 2026-08-15

**Zero live requests for the findings below; the acceptance run is separate.** All of it
came out of the two committed pod fixtures.

### The trap worth recording: bet365 orders fixtures two different ways

`FD` is the row's own sentence about the fixture, and **its separator is the only
statement of which competitor is at home**:

```
MLB   FD='BAL Orioles @ TB Rays'              → "<away> @ <home>"
MLS   FD='Orlando City v FC Cincinnati'       → "<home> v <away>"
```

The adapter assumed the US order everywhere. Every MLS fixture was therefore **inverted** —
and nothing inside the adapter noticed, because the rows were well formed, mirrored, priced
in range and summed above 1.0. What caught it was `tests/test_fixture_replay.py`'s
validation over the real slate:

> `home_away_disagreement: bet365 disagrees with the other sources about which participant
> is home on 4 of 6 shared fixture(s) (67%) — that is systematic rather than a neutral-venue
> judgement`

That is the cross-source check doing exactly what it exists for. The fix matches the whole
string (`f"{first} @ {second}"` / `f"{first} v {second}"`) rather than searching for a
separator, so a club whose name contains " v " cannot decide it, and a row that states no
host at all is declined under `fixture_without_a_stated_host` instead of defaulting to
whichever order the previous sport used.

**Generalizable:** row order is not a statement of anything. Two of this venue's three
identity facts — the host, and which of a pair a price belongs to — are stated in fields,
and only the third (the two-sided column order) is positional.

### MLS recovered: 33 prices from bytes already on disk

Soccer's coupon prices a moneyline as **three one-sided columns** headed `Home`/`Tie`/`Away`
rather than one two-sided `Money` column, so the two-sided reader discarded all of it. Eleven
fixtures were being parsed, oriented and keyed, then thrown away.

Now collected, with two guards:

- A **draw leg is mandatory**: a three-way that lost its `Tie` column is byte-identical to a
  genuine two-way market, so the survivors are refused as
  `three_way_moneyline_without_a_draw` rather than published leaving a tenth of the
  probability unbacked.
- `Fixture.our_side` translates the book's home/away onto ours and resolves **anything that
  is not HOME to the away side** — so a `DRAW` passed through it comes back as a competitor.
  Draws now bypass it.

Measured overrounds on the committed board: 1.0503, 1.0635, 1.0505, 1.0549 — ordinary soccer
three-way margins.

`MARKETS_BY_LEAGUE` also had to stop declaring spread and total for MLS. The coupon carries
neither, and validation correctly read a declared-but-never-arriving market as a renamed
label (`core_market_absent`).

### Scopes are leagues now, not pods

`ScopeTally` counted the two pod requests, so **NHL — configured, carried by no pod — fired
no counter at all**: a silent zero inside a run that graded healthy. Scopes are the
configured leagues, and the acceptance run below names WNBA, NFL and NHL as returning
nothing rather than omitting them.

### The rest of the integration gap

- **PA and NJ routes provisioned** as `TEMPLATE` (`www.pa.bet365.com` / `csid=56`,
  `www.nj.bet365.com` / `csid=3`). Neither has been asked from its own egress and this
  machine has none, so both are structurally known and unproven — the same standing as PA
  `thescore`. PA's coverage entry now names `bet365` as its direct route, which cannot lift
  the grade on its own: `DIRECT` is earned on `direct_rows > 0`.
- **The state check now refuses an absent marker.** It used to return silently when the
  shell carried no `STATE_LOCALE`, on the reasoning that a missing field is not a
  disagreement. It is here — every shell this host serves carries one, so a body without it
  is a redirect or a splash, and being permissive there is how an unreachable route fetches
  a board and labels it with the state that was *asked for*.
- **Eleven skip reasons had no reader-facing explanation** and rendered as "Not one of the
  four kinds of game bet collected here" — affirmatively false for a suspended selection,
  and worst for `price_without_row_index`, the counter that means the column join broke.
  All eighteen are now noted.
- **bet365's three doors are pinned by test**, and its stateless fallback is now argued
  rather than assumed: `www.bet365.com` serves no board, and unlike `unibet` bet365 cannot
  buy the exemption with view-only inventory because it is stakeable. Its protection is that
  a retail key is excluded from `global_sources()`, so no ungoverned run can hold a bet365
  leg — asserted, so that if it ever stops being true the book needs a real door first.
- **The distinctness gate was run** — never discharged at registration — over run 23, a full
  Illinois slate containing bet365 rather than a stored run predating it. **16 sources
  compared, 0 blocking**, 12 reaching `DISTINCT` on real overlap:

  | worst pairs | shared | identical | rate | verdict |
  | --- | --- | --- | --- | --- |
  | `betrivers_kambi` | 22 | 3 | 0.136 | DISTINCT |
  | `fanduel` | 22 | 3 | 0.136 | DISTINCT |
  | `betmgm` / `leovegas_kambi` / `thescore` | 22 | 2 | 0.091 | DISTINCT |
  | `hardrock` / `draftkings` / `kalshi` / `sxbet` | 4 | ≤1 | — | UNDECIDED |

  The four `UNDECIDED` pairs are below the 20-selection floor because bet365's board is 16
  events, not because they agree — `UNDECIDED` is a third verdict and not a pass, and it
  resolves on its own as coverage grows.

### Acceptance

Run 23, full Illinois slate, 74,446 quotes across 1,887 events: **bet365 healthy — 72
quotes, 16 events, 3 requests, 0 rejections**, and `replay --run 21` PASS on the
single-source pass that preceded it. No `required_book_*` finding for bet365, and — the
one that matters — **no `home_away_disagreement`**, so the orientation fix holds against
thirty-odd other feeds on a live slate rather than only against the committed bytes.

Two warnings stand and are correct: `start_time_disagreement` on Pacific fixtures (already
recorded above as this venue's own clock, not a parse fault) and
`declared_sport_produced_nothing` for football and hockey — NFL and NHL are configured, out
of season, and now *say so* instead of vanishing, which is the per-league scope change
working.

## bet365 Illinois is REACHABLE: the block lapsed, and the odds are on plain HTTP — 2026-08-15

**This reverses the standing verdict.** The section below, written 2026-08-14, says the
Illinois egress is hard-blocked for bet365 and that "anyone continuing this work needs a
different IL egress." That was true when measured and is **no longer true**. The block
expired on its own in roughly forty hours, from the same address that earned it
(`34e56847c7ec…`, unchanged). Nothing was done to lift it and nothing was worked around —
it was re-checked once, politely, and it had gone.

Record the correction rather than the embarrassment: **an IP-scoped Cloudflare block from
rate limiting is temporary, and "there is no recovery" was wrong.** Re-check before
declaring a venue lost.

**Total cost this session: ~28 requests** — one status check, one page load (bet365's app
is light: 26 recorded responses against Caesars' 331), one endpoint A/B.

### Rung 1 — is the block still on? One request.

`GET https://www.il.bet365.com/` → **200**, 41,842 B, `server: cloudflare`,
`cf-cache-status: DYNAMIC`. Against 2026-08-14's **403**, 4,547 B, "Sorry, you have been
blocked". Same egress, same host.

### Rung 2 — the board renders under automation

`recon_sources.py bet365 --state IL --mode chrome-persistent --include-all --capture-dom
--wait-ms 90000 --then-hash "/HO/"`. The hash route was applied after boot, which is what
`then_hash` exists for — a fragment supplied in the URL is present before the router and
this app ignores it.

**DOM: 24,210 characters carrying 632 American-odds tokens**, with real fixtures — `HOU
Astros (v SEA Mariners)`, `ATL Braves (v ARI Diamondbacks)`, `Money Line` — and an MLB
node reporting 330 markets. Compare Caesars' 1,753 characters and zero prices. **bet365
does not fingerprint automation on its board**, and the contrast with Caesars is the point:
these are two different postures, not one industry norm.

### Rung 3 — the odds are on plain HTTP, and this is the cheap branch

Prices do **not** ride the websockets. Those twelve sockets
(`premws-pt1.us.365lpodds.com`, `pshudws.us.365lpodds.com`,
`www.il.bet365.com/sportspublisher`) carried 44–186 characters each — live-update
plumbing. The board arrives over ordinary GETs:

```
https://www.il.bet365.com/pullpodapi/gethomepagepods?lid=32&zid=0&pd=%23HO%23COL1%23&cid=198&…
https://www.il.bet365.com/pullpodapi/gethomepageadditionalpods?…            (173,638 B)
```

**The decisive A/B**, the same pairing that settled Caesars, URL taken verbatim from the
capture with no parameter variation:

| client | result |
| --- | --- |
| real Chrome, warm profile | 200, 88,298 B, 254 `OD=` fields |
| `curl_cffi` via `--mode http`, no browser, no cookies, no token | **200, 88,298 B, 254 `OD=` fields** |

Byte-identical. **No WAF challenge, no browser, no session.** This is the branch every
Caesars rung was hoping for and never reached.

### The payload format

bet365's compact pull-pod encoding: `|`-delimited records, `;`-delimited `KEY=value`
fields, record types `PS`/`PD`/`XL`/`CL`/`EV`/`MA`/`PA`. Prices are **fractional** on
`OD=`, names on `NA=`, structured item ids on `IT=`, handicap on `HA=`:

```
NA=HOU Astros    OD=100/119   IT=#PB#AS#B16#-PB3167090-1910    → decimal 1.8403
NA=ATL Braves    OD=10/13     …                                 → decimal 1.7692
NA=LA Dodgers    OD=11/10     …                                 → decimal 2.1000
```

**Correction, 2026-08-15:** an earlier version of this line claimed `src/normalize.py`
already converts and that nothing new was needed. It does not. That module exports exactly
four callables — `american_to_decimal`, `decimal_to_american`, `implied_probability`,
`is_plausible_decimal_odds` — and there is no fractional converter anywhere in `src/`.
`fractional_to_decimal` was added for this venue: `decimal = 1 + numerator/denominator`.
Note the near-miss it has to avoid: `thescore._decimal_odds` returns `num/den`, because
theScore publishes decimal odds *as a rational*. bet365's `OD=` is a true fraction, so
copying that helper is wrong by exactly 1.0 on every row — and the result still lands
inside `is_plausible_decimal_odds`, so nothing downstream would catch it.

**Caveat on this specific pod:** `#HO#COL1#` is the *homepage* pod and the records sampled
above are `#PB#` parlay-builder entries ("Round Robin by 6s"), not a clean per-league
moneyline board. The transport question is answered; the *coverage* question is not. A
real adapter wants the per-league pod, and finding it is the next rung — from the page's
own observed requests, never by fuzzing `pd`, which is precisely what cost this egress on
2026-08-14.

### Why this matters more than Caesars did

bet365 is a required book in **both** IL and PA (`src/coverage.py:300`, `:327`), carries
`direct=None` in both, and grades `SINGLE_SOURCE` — a standing warning in each state. It
has no first-party adapter at all. Unlike Caesars, whose measured value was **+0
opportunities**, this one is unmeasured and the book is genuinely absent rather than
merely un-stakeable.

**Left standing:** bet365 IL is first-party reachable over plain HTTP from this egress
today. The remaining work is finding the per-league pod and writing a parser for a
delimited format — ordinary adapter work, on the model of `src/sources/thescore.py`.

## Caesars: the board route found, the board endpoint named, and the wall relocated to bot classification — 2026-08-15 (supersedes the section below)

**This corrects the section immediately below, written hours earlier the same day.**
Its verdict — "the app cannot reach its own navigation service" — was drawn from captures
that were pointed at a URL which is **not a board route at all**. The operator supplied a
working one, and it changed the answer.

**Cost: 2 further page loads on a warm profile.** Illinois egress `34e56847c7ec…`.

### `/us/{state}/bet/` is a basename, not a route — and both halves are load bearing

The app is a path-routed SPA whose routed paths are `/`, `/:sport`, `/:sport/futures`,
`/:sport/inplay`, `/:sport/events/:date`, `/allsports` and a handful of account pages.
`/us/il/bet/` matches **none** of them. Every capture before this one asked for it, so the
router mounted no board component, no board bundle was fetched, and nothing subscribed to
a price topic. That is the real reason those sessions were empty.

The full board route table is in the application bundle — recovered only after
`MAX_BODY_CHARS` was raised, because the bundle is 7.96 MB and the old 2 MB cap had
truncated it. The `<noscript>` `/odds/*` links are **client-side** React redirects, which
is also why fetching one over plain HTTP returns the shell: no JS runs, so no redirect
fires.

| League | route | competition id agrees with `caesars.py` |
| --- | --- | --- |
| NFL | `/americanfootball?id=007d7c61-07a7-4e18-bb40-15104b6eac92` | yes |
| NCAAF | `/americanfootball?id=b7eda1b3-0170-4510-9616-1bce561d7aa7` | new |
| NBA | `/basketball?id=5806c896-4eec-4de1-874f-afed93114b8c` | yes |
| NCAAM | `/basketball?id=d246a1dd-72bf-45d1-bc86-efc519fa8e90` | new |
| MLB | `/baseball?id=04f90892-3afa-4e84-acce-5b89f151063d` | yes |
| NHL | `/icehockey?id=b7b715a9-c7e8-4c47-af0a-77385b525e09` | yes |
| EPL | `/football?id=00200de2-37f3-4339-9e3d-9bf8cb5eb34b` | new |

**The state prefix is separately load bearing, and dropping it is a locality trap.**
Loaded at the bare root (`sportsbook.caesars.com/americanfootball?id=…`, the operator's own
URL) the app fetched **`configs/sportsbook/co/splash` — Colorado**. Loaded at
`sportsbook.caesars.com/us/il/bet/baseball?id=…` it fetched `configs/sportsbook/il/splash`
and issued Illinois-scoped API calls. So the correct form is
**`/us/{state}/bet/{sportSlug}?id={competitionId}`**: the basename pins the licence, the
route mounts the board. A capture taken without the prefix is another state's board.

### The board endpoint, named at last

With the route correct, the board component loaded (new chunks `Sport`, `6675`, `7021`,
`9372` — absent from every prior capture) and asked for its data at:

```
https://api.americanwagering.com/regions/us/locations/il/brands/czr/
    sb/v4/sports/{sportSlug}/competitions/{competitionId}/tabs
    sb/v4/sports/{sportSlug}/competitions/{competitionId}/quick-picks
```

State-pinned, competition-scoped, and observed being requested by the page itself. **This
is the endpoint six weeks of work were looking for**, and it retires the v3 highlights
path the adapter still asks for — the one with no provenance — for good.

### The wall is bot classification, not the token and not the egress

Every one of those v4 calls returned **403**, 919 bytes, CloudFront `Request blocked.`
And the token is not the explanation:

| | |
| --- | --- |
| `awswaf …/inputs?client=browser` | **200** — `challenge_type: ha9faaff…`, `difficulty: 1` |
| `awswaf …/mp_verify` | **200** — `{"token":"[redacted]","inputs":null}` |
| `sb/v3/sports-menu`, `sb/v3/teamMetadata` | **200**, same session, same token |
| `sb/v4/**` | **403**, same session, same token |

The challenge completes and a token is issued. v3 is served under it. v4 is refused under
it. So CloudFront applies a **stricter rule to the board API than to the catalogue**, and
this browser fails it while a human-driven one does not: the operator loads the same route
from the same connection in ordinary Chrome and sees a full board.

That difference is automation classification — `navigator.webdriver`, the automation
launch flags Playwright uses (Chrome shows its own `--no-sandbox` infobar), and CDP
attachment are all visible to a bot rule that has already proved the client runs JS.
**Closing that gap means making an automated browser misrepresent itself, which is
defeating an anti-automation control and stays out of scope permanently** — the same line
that forbids deriving the token.

### Left standing

Caesars serves its board to a human browser on this connection and refuses it to this
one. Routes stay `TEMPLATE`. What is now permanently known, and cost three page loads to
learn: the route form, the seven competition routes, the state-pinning rule, and the exact
board endpoint. If a sanctioned path ever opens — a licence, an agreement, or a
human-in-the-loop capture the operator drives — the remaining work is a parse, not a
search. The measured value of that work is still **+0 opportunities** against run 16.

## Caesars has no anonymous web board from this egress: the app cannot reach its own navigation service — 2026-08-15 (superseded above)

**Cost: 1 plain request plus 1 page load (80 recorded Caesars-host responses; the
manifest records only `xhr/fetch/document/script`, so the true figure is higher).**
Illinois egress, fingerprint `34e56847c7ec…`, unchanged since 2026-08-14. Two rungs ran;
the third and fourth were never reached because there was nothing to decode.

### The SEO odds pages are the app shell, byte for byte

The `/us/il/bet/` document's own `<noscript>` block links fifteen odds pages — `/odds/mlb`,
`/odds/nfl`, `/odds/nba`, `/odds/nhl`, `/odds/soccer`, `/odds/epl`, `/odds/live`, … — and
they are **root-relative**, so they resolve to `https://sportsbook.caesars.com/odds/mlb`, a
path root never fetched in any capture. Server-rendered odds there would have made every
Diffusion question irrelevant, so it was asked first, with one request.

| | |
| --- | --- |
| `GET https://sportsbook.caesars.com/odds/mlb` | **200**, 11,323 B, `text/html` |
| `server` / `x-cache` | `AmazonS3` / `RefreshHit from cloudfront` |
| body sha256 | `7ab4ecee34da5b4f…` |
| American-odds-shaped tokens | **0** |

That sha256 and byte count are **identical to `/us/il/bet/`** captured 2026-08-12. One
static S3 object is served for every path; the `<noscript>` links are decoration inside
the shell, not routes to rendered content. **There is no server-rendered odds surface at
Caesars, and this is now measured rather than assumed.** Note the shell is *not*
WAF-gated — it answered the repository's plain client with 200.

### The app boots, the click lands, and the board never arrives

`recon_sources.py caesars --state IL --mode chrome-persistent --include-all --capture-dom
--wait-ms 120000 --click-text "All Sports"` — real Chrome 151, first persistent profile,
two minutes, logged out. **331 responses, 38 websockets**, against 146/38 on 2026-08-12.

Four measurements, and they agree:

1. **No board chunk was ever downloaded.** 55 JS chunks both sessions, and the sets are
   **identical** — zero new chunks today. No `Sports*`, `Competition*` or `EventDetail*`
   bundle exists in either capture. The code that renders a board never loads.
2. **The click succeeded and changed nothing.** No `CLICK` diagnostic was recorded, so
   `"All Sports"` was found and clicked; the final URL is still `/us/il/bet/` and the DOM
   is **1,753 characters** of shell — smaller than 08-12's 2,289. Its only price-shaped
   tokens are once again the odds-format picker (`+275`, `-800`, `-426`, `-2537`).
3. **The v4 refusals reproduce, and the app keeps asking.** `sb/v4/navigation-items`
   **four 403s plus four aborted attempts**, `sb/v4/home` 403, `sb/v4/sports/homepage/quick-picks`
   403, `gw/growth/v4/sports/homepage/banners` 403 — while `sb/features`,
   `sb/bets/configuration`, `sb/v3/sports-menu` (×2) and `sb/v3/teamMetadata` all answer
   200 in the same session. The retry loop is new evidence: this is the application
   failing to obtain its own navigation configuration, not a single unlucky request.
4. **The Diffusion sockets carry no prices, over two minutes.** All four
   (`/diffusion`, `/livescores`, `/cashout`, `/microbetting`) exchanged 24–26 frames each
   and **not one exceeded 60 bytes** — handshake and keepalives only. They connect, they
   stay up, and nothing ever subscribes to a board topic, because nothing ever asks for
   one.

**H1 is confirmed.** The board's data path is published by `sb/v4/navigation-items`; that
service is refused at the CloudFront edge; so the app has no route into a board, never
loads the board bundle, never subscribes to a price topic, and paints no odds. Every
downstream question — the Diffusion binary grammar, the topic schema, a push-feed adapter
— is moot while the navigation service refuses. **Caesars has no anonymous web board
reachable from this egress.**

### The one variable this did not control

**GeoComply cannot run on this machine.** 33 × `net::ERR_CONNECTION_REFUSED` against
`wss.plc-gc.com:9703-9705` plus the matching `/check` requests — that hostname resolves to
loopback and exists so a page can reach a *locally installed* GeoComply client, and
nothing is listening. The operator reports seeing odds while logged out in their ordinary
browser on this same machine; if that browser has the GeoComply client available and this
one does not, the difference between the two environments is a missing local dependency
rather than automation detection.

This capture cannot separate "v4 is refused to automation" from "v4 is refused to any
client that has not completed a location check" — both look exactly like what was
measured. It is stated as an open variable rather than resolved, because resolving it
means installing a geolocation client, which is an operator decision and not a
reachability question. What is *not* open: the refusal is a generic CloudFront WAF page,
not an application-level "location required" response.

### Egress detection had to change to run this at all

`ipapi.co` now answers this repository's client with a Cloudflare **"Just a moment…"**
interstitial (HTTP **403**), not the anticipated rate limit. It is the only configured
provider, so this blocked *every* recon capture outright — and passing that interstitial
is defeating a challenge, which is out of scope permanently.

`src/egress.py` gained `confirm_unchanged_egress`, a **continuity** check that asks a
weaker question with a stronger answer: if the current public IP hashes to the fingerprint
of a stored detection, we are on the address that detection already verified and its state
still describes us. It **cannot establish a state**, only carry a verified one forward; an
unrecognized address returns `None` and the run refuses exactly as before. Identity is
asked of `checkip.amazonaws.com`, which is never asked about geography — which is why a
second provider is safe here and remains unsafe in `DEFAULT_DETECTION_URLS`.

Both rungs above ran under it, confirming `34e56847c7ec…` unchanged since
2026-08-14T01:40Z.

### Left standing

Caesars stays `TEMPLATE` in all four states and stays a required book, visibly missing. It
is not a re-pointed URL away, and it is not a Diffusion client away: the app itself cannot
get past its own navigation service from here. Combined with the measured value of the
route — **+0 opportunities** against stored run 16, best price on 2 of 106 groups — there
is no case for further spend. Reopen only if the GeoComply variable is closed and the
board renders for a client that has one.

## Caesars: the session that closed it never reached a board, and a 7th stakeable book would have added nothing — 2026-08-15

**Zero requests spent.** Both halves are offline: a re-analysis of the capture
already on disk (`data/research/caesars/IL/20260812T175756Z/` — 146 responses,
143 bodies, 38 websockets) and a counterfactual over stored run 16. No egress was
used, so the Illinois fingerprint `34e56847c7ec…` is untouched by this work.

### The value question, asked before spending anything

Caesars is a required book in IL and PA (`src/coverage.py:308`, `:336`) and its
`an_caesars`/`vi_caesars` feeds are view-only, so `find_opportunities` drops their
rows before forming any assignment (`src/arb.py:1043-1049`) — **a Caesars leg
cannot appear in an opportunity today.** Only a first-party route changes that.
Nothing in the repository measured what that would be worth, so this asks.

Method: load run 16 (2026-08-14T07:48Z, IL, `state` scope, 82,934 rows, 32
sources), remove `an_caesars` from `view_only_for_run("IL", …)`, re-measure
counterparty groups with the same set the legs are formed by, and re-grade.
`an_caesars` prices the same book on the same slate, so it is the closest
available proxy for first-party rows. The counterparty gate stays armed
throughout — `src/redundancy.py:57-59` pins all three Caesars pairs and
`_independent_source_count` unions them inside arb regardless.

| | opportunities | ROI min / median / max | `comparable_group_count` |
| --- | --- | --- | --- |
| baseline, as graded | 11 | 0.2891% / 0.6824% / 1.6571% | 3,939 |
| `an_caesars` stakeable | **11** | identical | 3,939 |

**Delta: +0.** The rows do enter — 81,144 survive the baseline view-only filter
and 81,319 survive the counterfactual one, exactly +175. The identical group
count is not an artifact: all 25 events `an_caesars` covers are already priced by
other sources, so a third observer creates no new comparable group.

`an_caesars` contributes 175 rows over 25 events — MLB 96, NFL 60, WNBA 12,
SOCCER_OTHER 7; moneyline 75, spread 50, total 50. That is thin next to what a
first-party route produces in the same run (`thescore` 593, `hardrock` 723,
`draftkings` 966, `betmgm` 2,030, `fanduel` 2,272), so "+0 opportunities" on its
own would overstate the negative. The sharper question a thin sample *can* answer
is whether Caesars is ever the outlier, because a book that never holds the best
price cannot create an arbitrage however many rows it adds:

| over the 106 two-sided groups Caesars touches | count |
| --- | --- |
| Caesars holds the best price on a side | **2** (one total, one spread) |
| Caesars improves the best implied sum | **1** |
| gross arb without Caesars | 25 |
| gross arb with Caesars | **25** |

The single improvement moves the best implied sum from **1.2200 to 1.2150** — a
spread market still **21.5% away** from an arbitrage. Caesars is the best price
on 1.9% of the groups it appears in, and that is a *rate*, which does not improve
with coverage: five times the rows gives five times the near-misses.

**Left standing:** on this evidence a 7th stakeable Illinois book at Caesars buys
no positions. The republisher caveat is real and is why this is recorded as a
rate rather than a count — but it constrains the precision of the number, not its
sign.

### Five things the 2026-08-12 section got wrong

That section is below, headed "Caesars is gated by an AWS WAF token". Its central
claim — that v3 is token-gated and `curl_cffi` cannot have it — survives intact
and is not disturbed here. What follows is everything else.

1. **The session never reached a board, so it is not evidence about one.** Zero
   price-shaped tokens across all 143 bodies, the DOM snapshot and every socket
   frame. The DOM is 2,289 characters of header, state-picker, nav rail, empty
   betslip, footer and cookie banner. The five `[+-]\d{3,4}` hits read as prices
   are the **odds-format picker samples**: `Odds Settings / Odds Format /
   American - +275 / Fraction - 11/4 / Decimal - 3.75`. The board's code never
   arrived either — no `Sports*`, `Competition*` or `EventDetail*` chunk
   downloaded.
2. **No Diffusion frame ever carried a price.** "The prices arrive over a
   Diffusion push socket" describes frames that were not seen: one 55-byte
   connection response then 2-byte pings, sent frames 3 bytes. It remains a good
   hypothesis — the IL config names four `DIFFUSION_*` sockets and no REST board
   path, and the bundle carries the type names `DiffusionCompetition`,
   `DiffusionEvent`, `DiffusionMarket`, `DiffusionTab` — but it is a hypothesis
   from configuration and type names, **not a measurement**.
3. **There are four Diffusion sockets, not one:** `/diffusion`,
   `/livescores/diffusion`, `/cashout/diffusion`, `/microbetting/diffusion`, all
   `?ty=WB&v=25&ca=10&r=0`. The microbetting topic grammar is readable offline
   and embeds `wh-il`, so a subscription would be state-pinned by construction.
4. **The v4 refusal is edge-generated, and the token-race explanation dies on a
   clock.** v3 / `features` / `bets-configuration` answer 200 with `server:
   istio-envoy` and `x-cache: Hit from cloudfront`; every v4 path answers 403
   with `server: CloudFront` and `x-cache: Error from cloudfront` — generated at
   the edge, never reaching the origin. And `mp_verify` returns 200 at
   `17:57:07` while `sb/v4/navigation-items` 403s again at `17:57:37`, thirty
   seconds *after* the token was minted. v4 is not a pre-token race and not a
   lever.
5. **`--then-hash` was never the lever.** `location.hash`, `createHashHistory`,
   `HashRouter` and `pushState` appear **zero** times in the bundle: Caesars is
   **path-routed** under basename `/us/{state}/bet`. The observed navigations are
   `/allsports` (the only sportsbook path in loaded code) and the document's own
   `<noscript>` SEO nav — `/odds/nfl`, `/odds/nba`, `/odds/mlb`, `/odds/nhl`,
   `/odds/soccer`. Both are observed, not guessed.

**Why no board rendered — hypothesis, unverified.** The board hooks
(`useGetEvent`, `useGetPromotedMarketHighlightsQuery`,
`useGetParlayBuilderHighlights`) each take a `dataPath` read from react-router
location state, and `sb/features` sets `ff_enableNavigationItemServiceV4: true`.
If that path is published by `sb/v4/navigation-items`, which is edge-blocked,
then from this egress the app has no route into a board at all. That would
explain the missing chunks, the empty DOM and the silent sockets in one stroke.
**It is unverified**, and testing it costs one page load against an observed path.

**One caveat on the corpus itself:** `main.efb1ae47.js` on disk is truncated at
`MAX_BODY_CHARS = 2_000_000` (`src/sources/research.py:20`), so absence-of-string
claims over that one file are not sound. The "no REST odds path" reading rests on
the IL config (95 keys, no board key) and on what the page actually requested —
across all bodies `sb/v3` occurs once (`TRANSACTION_HISTORY`) and `sb/v4` zero
times. Raise the cap before the next capture.

**Unchanged by all of this:** every Caesars route stays `TEMPLATE`, the book stays
required, and the first-party wall stays documented rather than worked around.
Deriving the WAF token outside a browser remains out of scope permanently.

## Two detection providers disagreed about one egress, and the wrong one was winning — 2026-08-14

The operator hit `Scrape failed: StateSelectionError: detected CA, but collection
supports only IL, PA, NJ, DC` from the dashboard — intermittently, on a machine
sitting in Illinois whose egress fingerprint had not changed (`34e56847c7ec`, the
same value recorded in every section above).

**Measured, both providers asked in one pass:**

| provider | order | answer |
| --- | --- | --- |
| `https://ipapi.co/json/` | tried first | `HTTP 429 Too Many Requests` at the time of the failure; `IL` when it answers |
| `https://ipwho.is/` | fallback | `HTTP 200`, `region_code: CA`, city Los Angeles |

The IP was never in doubt. `data/egress_state.json` held `state: IL` with that
same fingerprint, and theScore's own server-side `currentRegionCode` — a
first-party licence statement, not a geolocation guess — reads `US-IL` from this
egress. `ipwho.is` is simply wrong about this address.

**Why it was intermittent, and why the fallback made it worse.**
`detect_egress` returns the *first* provider that answers, so a second provider
is a safety net only if both agree. ipapi.co's free tier caps requests and every
scrape, probe, and `detect_state.py` run spends one, so a busy session exhausted
it — and the exhaustion handed the decision to the provider that was wrong. The
failure therefore tracked how many lookups had been spent recently, not where the
machine was.

**The `CA` refusal was the benign half.** Detection was a *gate*:
`select_states` validated the detected state before it read the requested ones,
so `--state IL` could not clear it and there was no way to state by hand where
you were. The dangerous mirror image is the same disagreement pointing the other
way — standing in PA with a provider claiming IL would have collected Illinois
routes over a Pennsylvania egress and filed the rows as Illinois, which is
exactly the wrong-state-feed failure the scrape rules exist to prevent. Nothing
downstream would have objected: the feeds answer 200 and the numbers look
plausible.

**Changed, same day.** `ipwho.is` is removed — a provider that is wrong about the
IP fails toward a confident wrong state, which is worse than an honest gap.
Detection is now advisory rather than a gate: an explicitly chosen state wins and
needs no lookup, detection only fills a choice nobody made, a *recent* stored
record fills in when the lookup fails, and only when none of those names a state
does collection refuse — naming what to pass. The dashboard's state checkboxes
pre-check the detected state instead of checking *and disabling* it, and remember
the operator's own choice across reloads.

What replaces the gate is labelling, not trust: a fresh reading is now persisted
even when it names a state collection does not support, because
`report._payload` compares it against the run's own jurisdiction and appends a
jurisdiction warning when they differ. A deliberately out-of-state run is
allowed and visible rather than blocked.

**Left standing:** a third-party IP database is a weak witness — this one was
wrong today. The strong form of the check is a venue reporting which licence it
served, which only `thescore` currently does.

## bet365 Illinois: the home page is HTTP, the board is not, and the egress is now blocked — 2026-08-14

From the operator's **Illinois** egress (fingerprint `34e56847c7ec`), **no
proxy**. Track B's ladder, rungs R1/R2/R4/R7/R8/R9, **and a correction to the
first version of this section, which was written the same day and overstated
three things.** An adversarial audit against the manifests on disk found them;
the measurements below are the ones that survived it.

**Read this first: the Illinois egress is hard-blocked for bet365.** About a
hundred probing requests over one evening — the last of them fuzzing the `pd`
parameter's encoding — ended with Cloudflare returning **`403`, 4,547 bytes,
"Sorry, you have been blocked"** across the entire estate: `www.il.bet365.com`,
`www.bet365.com`, the previously-ungated `routingdata` endpoint, and both push
hosts. Confirmed by three independent transports (the repo's `curl_cffi`,
Playwright real Chrome, plain `curl`) returning byte-identical refusals, which
makes it **IP-scoped rather than fingerprint-scoped**, and re-confirmed after it
had held for over an hour. It is a WAF block, not a JS challenge, so there is no
legitimate way through and none was attempted. **Every "200" recorded below was
real when taken and is not reproducible from this egress today.** Anyone
continuing this work needs a different IL egress, and should treat parameter
fuzzing against this host as the thing that costs it.

### What is true

**R1 — the transport was never the problem.** `www.il.bet365.com` under the
repo's ordinary `curl_cffi` transport returned **`200`, 41,842 bytes**, the real
sportsbook shell. Every "403 Cloudflare under plain httpx" reading in the older
record was taken against the *stateless* `www.bet365.com`, which is not the book.
That correction stands on its own evidence and is independent of the block above.

**R8 — the client configuration is in that body, and two of its URLs are
ungated.** `b_util.WebsiteConfig` carries `STATE_LOCALE: "USIL"`, the push hosts
`wss://premws-pt1.us.365lpodds.com` and `wss://pshudws.us.365lpodds.com`, and
three URLs: `/manifestapi/getmanifest?…` and
`/websiteroutingdatacontentapi/routingdata?v=…` with no token (both answered
`200`, 46 KB and 16.9 KB), and `/defaultapi/sports-configuration?_h=…` with one.
The routing data is the app's **own endpoint catalogue, 135 entries**.

**The socket `uid` is not a credential.** From the page's own boot code:
`` `${t}:${e}/zap/?uid=` + String(Math.random()).substring(2) ``, subprotocol
`zap-protocol-v2`. A client-generated random number, not a derivation of `_h` or
`SST`. Nothing here needs an anti-bot control defeated, which is why none was.

**The home page's prices really do arrive over plain HTTP.**
`/pullpodapi/gethomepageadditionalpods` — an ordinary XHR, no `_h`, `csid=28` in
the query — returned **192,756 bytes carrying 754 `OD=` fractional-odds tokens**,
and re-requesting it from the repo's transport with **no browser at all** gave
the same 754. That is a genuine anonymous surface.

**R9 — fails.** No `__NEXT_DATA__`, no `application/ld+json`, no
`window.__INITIAL_STATE__`.

### Three things the first version of this section got wrong

**1. "R2 renders the full Illinois board" — it renders the *home page*.** The DOM
snapshot's own URL is `https://www.il.bet365.com/#/HO/` — bet365's home route —
and none of that capture's 26 responses is a request to any league endpoint. The
app never navigated into a competition. Of its **553** price tokens (not 551),
only **179 are fixture-grid prices**; the remaining 374 are collapsed futures
lists and "$10 pays $X" boost tiles. Every priced block is one the payload itself
labels `#SPOTLIGHT…#`, and the DOM prices are not an independent surface but the
same rows: the ten-game NFL preseason block reconstructs fixture-for-fixture and
price-for-price from the pod body after fractional→American conversion
(`OD=20/21;HA=-4.5` → `-4.5`, `-105`), captions included.

**And MLB contributed zero prices.** Its pod carries three real fixtures with
starting pitchers, but **every one of its 24 price rows is `SU=1`** — suspended,
because all three games had already started against the DOM footer's own
`Server Time 9:26:10 PM CT`. The rendered rows read "Spread / Total / Money" with
no numbers under them. WNBA likewise. So the club names I cited as evidence of a
board were an *unpriced* coupon: there were six of them, not three, and the
"Giants" was the NFL New York Giants.

**2. "Supersedes the catalog subscription stalls" — the stall is confirmed, and
now localized.** The control is the `__time` clock: all three socket hosts were
asked for it, `pshudws` answered 0.9 s after the document and `sportspublisher`
1.8 s after, while **`premws-pt1` published nothing at all across 24 socket
lifetimes in four captures** — not a price, not a config topic, not the clock.
The repeated sockets are a **reconnect loop with subscription replay** (six
byte-identical subscribe frames; the topic set decaying 21 → 21 → 11 as
per-event subscribers give up), and their count tracks *page uptime* at roughly
one new socket every 10–13 s. So the "two extra websockets after routing to MLB"
were the 15.5 extra seconds that run lasted, **not** the route.

The decisive capture is the deep-linked one: the app fetched
`/defaultapi/sports-configuration` **with the MLB route in its query and a valid
token**, got `200` and 5,490 bytes, logged twice that the config preload "was not
used within a few seconds from the window's load event", issued **no** pod,
coupon or markets request, and its sockets subscribed to config topics only and
received zero frames. That is a browser holding a valid `_h`/`SST` on the correct
MLB route, stalled waiting for the odds catalogue — which is the 2026-08-04
sentence verbatim. What has changed since then is only that the home page now
paints from HTTP pods, so the stall no longer *looks* like a preloader.

**3. "200 with zero bytes means the endpoint is real and the parameters are
incomplete" — it means nothing.** A path that does not exist
(`/matchmarketscontentapi/thisdoesnotexist`) returns the **identical** `200`,
`content-length: 0`, no content-type, `cf-cache-status: DYNAMIC`. That reading
was a lever that was never there.

### The routing grammar, decoded — and the one lead left

`/matchbettingcontentapi/coupon` genuinely cannot serve MLB: both of its
catalogue entries require `D:5,8,19` and the MLB route is `D48`. That much was
right. The grammar around it:

- `~` separates **AND** terms; `KEY:v1,v2` matches a route segment.
- **The caret is part of the key, not an operator** — `K^:12` requires the route
  token `K^12` while `K:2` requires `K2`, and both keys coexist in the catalogue.
- An empty value list (`AC:`, `AS:`) constrains the route's leading type token.
- `o` sorts **numerically** (the catalogue arrives pre-sorted; lexical order
  would put `1000` before `22`).
- `S^:1` selects a *partial* endpoint over its full sibling — a pattern holding
  for all six partial/full pairs.

By that grammar `/matchmarketscontentapi/markets` (`o=250`, `B:…16…` and
`D:…47,48…`) **was** the right endpoint, and the reason it returned empty is
proposed to be a missing **`/contentdata` path prefix** — the previously
unexplained `c` field, where `c==1` means prefix and `c` absent means bare path,
a rule consistent with all six content-API requests in the captures.

**That is a hypothesis and it is unverified**, because the egress was blocked
before it could be asked. `/contentdata/matchmarketscontentapi/markets?…` is the
single next request this work needs, and it needs an IL egress that is not this
one.

Also worth carrying forward: the app is React-Router driven
(`REACT_ROUTER_PERCENTAGE = 100,100`), `SITE_ROOT_PATH` values (`sportsil`,
`sportsus`) are asset-bundle names rather than site roots and `sportsil` is dead
code, a mobile user-agent cannot change the shell (the root carries
`vary: accept-encoding` only and is byte-identical at 41,842 B across five
captures), and `/splashcontentapi/splash` excludes sport 16 outright so `#AS#B16#`
can never route there.

**Unverified, and worth stating**: that pull-pod *content* differs per state. The
host and `csid=28` pin Illinois structurally, which satisfies the routing rule,
but no second state was ever asked.

## Fanatics has no anonymous board on the web, and the DNS map was not a route — 2026-08-14

From the operator's **Illinois** egress (fingerprint `34e56847c7ec`), **no
proxy**, both transports. This closes Track D's cheapest rung and **retracts the
2026-08-13 reopening above**: the striking DNS finding was real and was not a
route, and the research profile spent a day pointing at it.

**Every per-state host serves the same nothing.** Asked over HTTPS at `/`:

| host | result |
| --- | --- |
| `sportsbook.1il.betfanatics.com` | `404`, 548 B, `server: cloudflare`, sha256 `d465172175d3` |
| `sportsbook.1pa.betfanatics.com` | `404`, **same 548 B, same sha256** |
| `sportsbook.1nj.betfanatics.com` | `404`, same sha256 |
| `sportsbook.1dc.betfanatics.com` | `404`, same sha256 |
| `sportsbook.1oh.betfanatics.com` | `404`, same sha256 |

The body is a bare nginx 404 — not a block page, not a login wall, not a geo
refusal. **Ohio answers Illinois' exact bytes**, so this is one backend serving
nothing rather than a per-state board, and the same 404 came back from `/health`,
`/status`, `/api`, `/api/v1`, `/v1`, `/graphql`, `/sportsbook`, `/sports`,
`/betting`, `/index.html` and `/robots.txt`. `api.1il.betfanatics.com` is
NXDOMAIN.

That a hostname resolves, and that the set of resolving hostnames tracks
licensure exactly, turns out to say nothing about whether anything is served
there. Worth stating plainly because the DNS evidence was genuinely strong —
`1oh` resolving while `1ca` does not is not a coincidence — and it still did not
survive one HTTP request.

**The app is Akamai-protected and lands on marketing.**

| step | result |
| --- | --- |
| `sportsbook.fanatics.com`, plain HTTP | `200`, 2,712 B — an **Akamai Bot Manager challenge page** (`sec-if-cpt-container`, "Powered and protected by Akamai"), not a shell |
| `sportsbook.fanatics.com`, real Chrome | Akamai passed, then **`301` → `betfanatics.com`** |
| `betfanatics.com` | `200`, 537 KB — the marketing site |

63 responses captured through the browser, 0 websockets. The rendered DOM is a
state-availability map, "WINNING HITS DIFFERENT HERE", and betting *guides*. Its
only odds-shaped tokens are helpline numbers. The single JSON payload that could
have held a board is an **oddschecker affiliate widget** carrying exactly one
offer — "FANATICS PARTNERSHIP 10x100% Profit Boost Tokens Instantly - NY/IL" — a
promotion, not prices; its `geolocation: {countryCode: "us", subdivisionCode:
"il"}` independently confirms the egress.

So the 2026-08-12 note's conclusion stands and its reasoning is now right: the
sportsbook web host does end at marketing. It just does so via Akamai and a 301
rather than by not existing.

**One real gated surface, named so nobody re-finds it.**
`sportsbook.betfanatics.com` is not the same nothing as the per-state hosts:
`/graphql` returns a **Jetty** 404 page (364 B, `HTTP ERROR 404 Not Found` with
a URI table) rather than the nginx one, so that path reaches an application
server; and `/sportsbook` returns **`401` with a zero-length body**. Real
infrastructure, authenticated, no anonymous board. Nothing here is a route
without credentials, and a credentialed board runs straight into the fixture gate
(`exchanges-and-mirrors.md`: a response body carrying an account identifier has
no sanctioned path to a committed fixture).

**Track D's remaining path is unchanged and now costed honestly**: D-R1, a
logged-in capture through the Track C rig, on an account the operator creates by
hand, gated on D-R2 — whether the board *response body* is personalized. Solving
the Akamai challenge in code is out of scope permanently.

## theScore Bet is registered: Illinois' first direct route — 2026-08-13

From the operator's **Illinois** egress (fingerprint `34e56847c7ec`), **no
proxy**. `src/sources/thescore.py`, registered in `RETAIL_SOURCE_KEYS` with an
IL route and a PA one. This is the first `REQUIRED_BOOKS` entry to move off
`direct=None` since the table was written.

**What the live run did.** Run 11, `collect --state IL --source thescore --tier
core --no-alert`: **573 quotes, 125 events, 21 requests, 274 KiB, 12.2 s, 0
rejections, 2 out of scope** (both in-play). `replay --run 11` → **PASS**. A
separate direct exercise over all eleven competitions produced 621 quotes / 281
markets / 133 events with **0 validation findings**. The 12-second pass is far
inside the threshold that would put it in `SLOW_SOURCES`.

**The schema, as the adapter uses it.** Three documents, all `POST /graphql`:

1. `startup` → `regionalMetadata { currentRegionCode … }` and
   `anonymousToken(connectToken:)`.
2. `page(canonicalUrl:)` → the `Section` with `archetype: "COMPETITION_LINES"`.
3. `competitionSection(id:)` → `sectionChildren` → `MarketplaceShelf.marketplaceShelfChildren`.

`competitionSection` takes **only** `id` — `first`, `limit`, `page`, `offset`,
`cursor` and `oddsFormat` are all rejected as unknown arguments, and so is every
paging argument on `marketplaceShelfChildren`. So a competition's shelf is
whatever it is; there is nothing to page.

**Corrections to the section below, which was written from the browser capture:**

| It said | Measured through the adapter's own documents |
| --- | --- |
| `MarketSelection` has a `name` string and a `fullName` | `name` is an object of type `SelectionName`; `fullName` is inside it and `shortName` does not exist |
| `StandardEvent` carries `homeTeam`/`awayTeam` | It carries `homeParticipant`/`awayParticipant`; the `homeTeam` pair is on `LiveApiBaseballEvent` and is a different type |
| — (not known) | `MarketCardInterface` does not exist, and `MarketCard` exists but has no `event`, so the fragment must spread the two concrete card types |
| 58 events / 86 markets on the MLB lines tab | The `competitionSection` shelf serves 13 MLB cards × 3 markets. The larger figures were type-occurrence counts across a whole manifest, not one payload |

**Orientation is stated twice, and that retires the whole problem.**
`MarketSelection.participant.id` carries **the same identifier** the event gave
its own `homeParticipant`/`awayParticipant` — verified across MLB and MLS — and
a `DRAW` leg carries `participant: null`. So `MarketSelection.type` and the
participant id are two independent witnesses to the same fact, and the adapter
requires them to agree rather than trusting either. This is strictly better than
what was planned: the plan's third witness was to resolve the selection *name*
against the fixture's participants, and that was implemented first and
**rejected 13 real soccer markets** — theScore writes "Monchengladbach" for
"Borussia Monchengladbach", "Mainz 05" for "1. FSV Mainz 05", "LA Galaxy" for
"Los Angeles Galaxy" — because an open-roster competition has no alias table and
normalizes each spelling to a different key. An id has no such gap.

**Soccer is safe structurally, not by rule.** The three-way lives on its own card
type — `SoccerGridMarketCard`, distinct from `GridMarketCard` — carrying
`THREE_WAY_MONEYLINE`. So a two-way `MONEYLINE` cannot arrive on a soccer fixture
through this path at all. The adapter still refuses one, and additionally drops
**all** legs of a three-way that lost its draw, because two legs of three are
byte-identical to a genuine two-way market.

**The eleven canonical paths, every one asked and answered.** A wrong path
answers `200` with `page: null`, and the organization segment follows no pattern:

| League | Path | League | Path |
| --- | --- | --- | --- |
| MLB | `/sport/baseball/organization/united-states/competition/mlb` | MLS | `/sport/soccer/organization/`**`usa`**`/competition/mls` |
| NBA | `…/basketball/…/united-states/competition/nba` | EPL | `…/soccer/…/england/competition/premier-league` |
| WNBA | `…/basketball/…/united-states/competition/wnba` | LA_LIGA | `…/soccer/…/spain/competition/la-liga` |
| NFL | `…/football/…/united-states/competition/nfl` | SERIE_A | `…/soccer/…/italy/competition/serie-a` |
| NHL | `…/hockey/…/united-states/competition/nhl` | BUNDESLIGA | `…/soccer/…/germany/competition/bundesliga` |
| | | LIGUE_1 | `…/soccer/…/france/competition/ligue-1` |

**MLS is under `usa` while every other American league is under
`united-states`** — `…/united-states/competition/mls` returns `200` with a null
page. `nfl` serves the preseason board (34 cards) and `nfl-preseason` also
resolves; the adapter uses `nfl`. Tennis and NWSL are **not** collected: neither
path has been asked, and that is a coverage gap rather than a wall.

Two failure shapes, deliberately graded apart: a **null page** is a hard failure
(the path moved), while a page **with no lines section** is an empty scope — an
out-of-season competition serves a drawer instead of a lines tab, and EPL did
exactly that on 2026-08-12 before its season opened. Reading the second as a
refusal would grade this book broken every summer.

**The anonymous token never reaches a stored byte.** The fetch needs it and the
capture must not carry it, so it is captured and redacted in one pass through the
`body_filter` seam — a single anchored substitution on `"anonymousToken"`, not a
sanitizer, because `check_http_response` is handed `raw.body` and a broad filter
there would turn a block page into an apparently-good `200`. The seven committed
fixtures carry no IPv4 and no bearer token.

**Pennsylvania is provisioned, not measured.** Its route is `TEMPLATE` on the
DNS-confirmed `sportsbook.us-pa.thescore.bet`. What makes carrying it unproven
safe is that `_require_expected_region` compares the edge's own
`currentRegionCode` to the routed state on every fetch, so from anywhere but
Pennsylvania it refuses rather than pricing Illinois' board under PA's name.

## theScore Bet: the anonymous surface, measured end to end — 2026-08-13

From the operator's **Illinois** egress (fingerprint `34e56847c7ec`), with
`python scripts/recon_sources.py thescore --state IL` in `--mode http` and then
`--mode chrome --include-all --capture-dom --wait-ms 45000`. Three manifests
under `data/research/thescore/IL/`, stamped 20260813T204836Z, 20260813T204855Z
and 20260813T205527Z — 163 responses and 3 websockets on the last. (That tree is
ignored runtime output and is wiped periodically; the findings are recorded here
because the manifests are not durable.) **This supersedes "Parser/capture work
remains" with the actual shape of the work.**

**The host chain, no longer inferred.** `sportsbook.thescore.bet` serves a
Next.js shell (200, 3,501 B, empty `__next`). `/env.js` publishes
`NEXT_PUBLIC_SPORTSBOOK_API_URL = https://sportsbook.us-default.thescore.bet/graphql`
and a websocket twin at `…/graphql/websocket`. The "edge redirect" the 2026-08-03
note described is a **302 on the API, not on the page**: `Startup` is issued to
the *default* edge and answers `302` to the `us-il` one, after which all 19
subsequent operations go to `sportsbook.us-il.thescore.bet`.

**Persisted queries are `GET`, not `POST`**, at
`/graphql/persisted_queries/{sha256Hash}?operationName=…&variables=…&extensions=…`.

**But the endpoint does not require them.** A plain
`POST {"query":"{__typename}"}` to the default edge returned
`{"data":{"__typename":"RootQueryType"}}` — **200, anonymous, no hash, no token**.
Introspection is disabled (`"GraphQL introspection is disabled."`). So the hash
map is how the *client* talks, not a gate: an adapter may send query documents
directly, which removes the rotating-hash failure mode, the bundle fetch that
precedes it, and the `connectToken` that the persisted-query URL carries in its
`variables` parameter. **The query text still has to be learned from what the
application issues** — introspection being off means the schema cannot be asked
for, and inventing a query is inventing an endpoint.

### The anonymous flow, reproduced over plain HTTP — 2026-08-13

The 2026-08-03 note said "anonymous `Startup`, persisted `CompetitionPage`, and
`CompetitionPageSectionLinesTabNode` calls all returned `200`", and prescribed
keeping "the anonymous token in memory only". Both halves are right, and the
second is **load bearing** rather than a privacy aside: without the token the
odds fields are refused. What follows is the whole handshake, measured, because
the intermediate refusals are the useful part.

**Client headers, from the bundle** (module 98655, and 48041 for the platform
constant). Not credentials — API versioning and routing:

| header | value |
| --- | --- |
| `x-platform` | `web` |
| `x-app-version` | `26.16.1` |
| `x-app` / `x-client` | `espnbet` |

`NEXT_PUBLIC_BUILD_VARIANT` is literally `"espnbet"` — theScore Bet runs on the
ESPN Bet codebase, both being Penn Entertainment. `venues.md` records ESPN BET as
discontinued, so there is no live sibling to mirror, but it is worth knowing
before `src/distinctness.py` is asked whether this venue is a platform front-end.

**The ladder of refusals, each one naming its own cause:**

| request | result |
| --- | --- |
| `startup` with no client headers | `200` with `"Invalid user agent, app_version: nil platform: nil"` |
| `startup` **with** the headers above | **`200`** — `currentRegionCode: "US-IL"`, `validRegion: true` |
| `page` with headers, no token | `403` `UNAUTHORIZED` |
| `page` with `authorization: Bearer <token>` | `401` `auth_type: "identity"` — wrong header |
| `page` with `x-anonymous-authorization: <token>` | `401` `auth_type: "anonymous"` — right header, wrong form |
| `page` with `x-anonymous-authorization: Bearer <token>` | **`200`** |
| the persisted-query `GET` without a token | `403` — so this is **not** an allowlist; the same gate applies to both forms |
| the persisted `GET` without `content-type` | `400`, a CSRF guard asking for a non-simple content type |

So the working sequence, all anonymous, all plain HTTP, no browser at collection
time:

1. `POST /graphql` + client headers →
   `startup { regionalMetadata { currentRegionCode } anonymousToken(connectToken: $ct) }`.
   `connectToken` is a client-generated id; a freshly generated 26-character one
   is accepted. **Select `regionalMetadata` and not `ipAddress`** and the exit IP
   never enters the response at all — §A3's body filter becomes defence in depth
   rather than the only control.
2. Every later call adds `x-anonymous-authorization: Bearer <anonymousToken>`.
3. `page(canonicalUrl: "/sport/…/competition/mlb")` → `pageChildren`, of which the
   `Section` with `archetype: "COMPETITION_LINES"` carries the id for
4. `competitionSection(id:)` → `sectionChildren` → `MarketplaceShelf.marketplaceShelfChildren`
   → `GridMarketCard { event { fallbackEvent { … } } markets { selections { … } } }`.

**`pagedMarkets` in a captured payload is an alias** — the field on
`GridMarketCard` is `markets`, and querying `pagedMarkets` fails validation.
A response key is not a schema field.

Verified live: 10 cards, `Boston Red Sox @ Toronto Blue Jays`,
`MONEYLINE/Moneyline`, `AWAY_MONEYLINE BOS Red Sox 26/25 (-2500)`. Two things the
adapter must handle that this one call already showed: `status: "IN_PLAY"` events
sit on the same board as pregame ones, and a selection's `odds` can be `null`.

**Twelve anonymous operations, all 200, from the home page alone**: `Startup`
(17 KB), `SportsMenu` (95 KB), `FeaturedMarketsCarouselNode` (130 KB),
`MarketplaceShelves` (93 KB), `PromotionsCarouselNode`, `ChipsCarouselNode`,
`DeconstructedMarketplace`, `Betslip`, `AccountMenu`, `LiveEventsCount`,
`CasinoMenu`, `ReferralTrackerCardNode`. `CompetitionPage` and
`CompetitionPageSectionLinesTabNode` need a click into a league and are not yet
captured. A live subscription runs on
`wss://sportsbook.us-il.thescore.bet/graphql/websocket` — 8 frames sent, 42
received — so prices also push, which no adapter here consumes today.

**The exit IP is real and its field is named.**
`data.startup.regionalMetadata.ipAddress` carries it. It reached the manifest as
`[redacted]` only because `_IPV4` catches it *by shape* — `ipAddress` is not in
`_SENSITIVE_KEYS`, so nothing keyed on the name would have saved it. Beside it:
`currentRegionCode: "US-IL"`, `ipAddressShortRegionCode: "IL"`,
`ipAddressCity: "Chicago"`, `validRegion: true`, and an `anonymousToken` (which
*was* caught by name). Those region fields are the cross-check that catches a
correct host reached from the wrong egress.

**Orientation is stated, not inferred — the highest risk in the adapter is
retired.** `MarketSelection.type` is literally `AWAY_MONEYLINE` / home twin, and
`LiveApiBaseballEvent` carries explicit `homeTeam` / `awayTeam` objects. Two
independent witnesses, so the rule is "require agreement, reject on
disagreement" rather than any separator parsing.

**The lines call, and its arguments.** A deep link straight to
`/sport/baseball/organization/united-states/competition/mlb` (read from
`SportsMenu`, which publishes 208 competition deep links) issues
`CompetitionPage` with `{"canonicalUrl": "/sport/baseball/…/mlb"}` and then
`CompetitionPageSectionLinesTabNode` (271 KB) with the `Section:` id that
returned, plus `"oddsFormat":"AMERICAN"` and a dozen feature-flag booleans.
That payload held **58 `StandardEvent`, 86 `Market`, 180 `MarketSelection`,
214 `Odds`** and 42 `Points`. Deep-linking beats `--click-text`: the click on
"Baseball" timed out, and the capture survived only because a missed selector is
now recorded rather than thrown away.

Vocabulary: `Market.type` ∈ {`MONEYLINE`, `SPREAD`, `TOTAL`, `LIST`, null} and
`MarketSelection.type` ∈ {`HOME_MONEYLINE`, `AWAY_MONEYLINE`, `HOME_SPREAD`,
`AWAY_SPREAD`, `OVER`, `UNDER`, `LIST`}. **`Segment` is not a betting period** —
it is boxscore state (`{"number":5,"shortName":"5th","homeScore":0}`), and
reading it as one would invent sub-period markets that are not there.

**The trap that would price a fabricated moneyline.** One market on the MLB board
is `type: "MONEYLINE"`, named **"Run In The 1st Inning - Enhanced Odds"**, whose
selections are typed `AWAY_MONEYLINE` and `HOME_MONEYLINE` — and whose selection
names are **"Yes" (-110) and "No" (Even)**. So the strongest orientation signal
on this venue, `MarketSelection.type`, is *wrong here in both directions at once*:
the market is not full-game, and the sides are not teams. An adapter trusting the
type pair would publish a first-inning yes/no proposition as the game moneyline
with "Yes" as the away team, at a price plausible enough to sit beside a real
line and produce a phantom arbitrage. This is hardrock's `FTEI` / `FT:RR` trap in
theScore's vocabulary, and it is why `Market.type` needs `_common.mentions_a_sub_period`
over `Market.name` as a **second witness**, and why a moneyline whose selection
names are not participants must be rejected rather than reinterpreted.

**Soccer's draw is not missing — it is a different market type.** Baseball's board
shows no `DRAW` selection, which raised the question of whether theScore drops the
draw leg from a three-way (the failure that makes a 3-way byte-identical to a
2-way). It does not. MLS, captured the same day, serves
`Market.type = "THREE_WAY_MONEYLINE"` named **"Match Result"** with selections
`HOME_MONEYLINE` / **`DRAW`** / `AWAY_MONEYLINE` — 15 of them, and nothing else on
the lines tab. So the three-way is its own type, and a *plain* two-way `MONEYLINE`
appearing on a soccer fixture would be a different product that must be skipped
rather than merged, exactly as Hard Rock refuses `FT:ML` beside `1X2`.

English Premier League, by contrast, had no fixtures posted at all — only a
`LIST` outright, "Top Goalscorer", 58 selections. Its page issues
`CompetitionDrawerContent` and `CompetitionPageSectionOtherTabsNode` instead of a
lines tab. **An out-of-season competition is not a refusal**, and an adapter must
not read one as a geo-empty board.

**The full market vocabulary, from three boards.** `MONEYLINE` ("Moneyline",
two-way, teams), `SPREAD` ("Game Spread"), `TOTAL` ("Total Points"),
`THREE_WAY_MONEYLINE` ("Match Result", soccer), and `LIST` (player props and
outrights — selections named "10+", "12+", "20+", or a player name; not a
two-sided market and out of scope). `Market.type` is `null` on some prop markets
whose name still reads as a prop, so a null type is a skip, not a default.

**Lines come signed per selection, so no sign convention is needed.**
`MarketSelection.points.decimalPoints` is already `-10.5` on the favourite and
`+10.5` on the underdog, and identical on both legs of a total (`173.5`/`173.5`).
This is simpler than Hard Rock, where the adapter must apply `abs`/`-abs` itself —
but the mirror invariant (`home.line == -away.line`) should still be asserted
rather than assumed. Note the selection's `fullName` **embeds the line**
("ATL Dream -10.5", "Over 173.5"), so a participant lookup has to strip a trailing
signed number first, as `hardrock._selection_for` already does.

**Prices: read the long pair, not the display string — but the reason is narrower
than it first looked.** `Odds` carries `formattedOdds` beside a `numeratorLong` /
`denominatorLong` pair, and the pair is the **exact decimal odds as a rational**
(`18/5 = 3.6` → American `+260`; `3/2 = 1.5` → `-200`).

An earlier draft of this section claimed `formattedOdds` is rounded and cited
`13515/3751 = 3.603039` shown as `+260`. **That sample came from the parlay
carousel, not the board**, and the generalisation was wrong: across all 85 board
selections carrying a long pair, `formattedOdds` is exact in **85 of 85**.
Rounding appears only where the venue *computes* a price — the parlay above, and
`13387/2000 = 6.6935` shown as `+569`.

The rule still stands, for two reasons that survive the correction: a display
string is a formatting decision the venue may change without notice, and the
computed cases prove it already rounds when it needs to. Use
`numeratorLong / denominatorLong`. It just is not true that every board price
today is being understated.

Vocabulary for the adapter: `Market.type` (`MONEYLINE`), `Market.status`
(`OPEN`), `Market.updatedAtTime`, `MarketSelection.points`,
`Competition.slug` (`nfl-preseason`), `Organization.slug` (`united-states`),
`Participant.abbreviation` / `fullName` / `resourceUri`, and `TsbDeepLink.webUrl`
(`/sport/baseball/organization/…`) which is what a `betlinks` event URL would be
built from.

**Pennsylvania is still unmeasured over HTTP.** Its edge exists (below), but
nothing has asked it anything, and DNS is not a route.

## Three first-party host maps, measured by DNS with negative controls — 2026-08-13

Egress-independent: DNS resolution does not depend on which state the query
comes from, so these were taken from the operator's default egress and are
reproducible anywhere. **A resolving host is not a board** — none of this says
what any of these hosts serves, only which ones exist. What makes it evidence
rather than trivia is the *controls*: each pattern was probed with a name that
should not exist, and every one of those refused.

| host | result |
| --- | --- |
| `sportsbook.thescore.bet` | resolves (Cloudflare `104.17.242-246.50`) |
| `sportsbook.us-il.thescore.bet` | resolves — confirms the 2026-08-03 redirect target |
| **`sportsbook.us-pa.thescore.bet`** | **resolves** |
| `sportsbook.us-nj.thescore.bet` | resolves |
| `sportsbook.us-zz.thescore.bet` | **NXDOMAIN** ← control |
| `nonsense-control-xyz.thescore.bet` | **NXDOMAIN** ← control |

The `us-zz` control is the load-bearing one: it rules out a wildcard *inside the
`us-XX` pattern*, so Pennsylvania's edge is a real per-state host rather than an
artifact of asking. That retires the "PA edge hostname is unmeasured, do not put
it in `jurisdictions.py`" caveat **at the DNS layer only** — what it answers from
Pennsylvania egress is still unmeasured, and that is what a route needs.

**Fanatics: the "no host exists" reading was wrong, and `venues.md` said so.**
That table recorded `NXDOMAIN` under both transports with next lever "find
current host". The current host was found:

| host | result |
| --- | --- |
| `sportsbook.fanatics.com` | resolves (Akamai) — but redirects to marketing, per 2026-08-12 |
| `sportsbook.betfanatics.com` | resolves |
| **`sportsbook.1il.betfanatics.com`** | **resolves** |
| **`sportsbook.1pa.betfanatics.com`** | **resolves** |
| `sportsbook.1nj.` / `1dc.` / `1oh.betfanatics.com` | resolve |
| `sportsbook.1ca.betfanatics.com` | **NXDOMAIN** ← control |
| `sportsbook.1zz.betfanatics.com` | **NXDOMAIN** ← control |
| `sportsbook.il.betfanatics.com` (no prefix) | **NXDOMAIN** ← control |
| `sportsbook.2il.betfanatics.com` (reindexed) | **NXDOMAIN** ← control |
| `nonsense-control-xyz.betfanatics.com` | **NXDOMAIN** ← control |
| `api.betfanatics.com` | NXDOMAIN |

The pattern is exactly `sportsbook.1{state}.betfanatics.com`, the `1` is a
constant rather than an index, and **the map tracks licensure**: `1oh` resolves
and `1ca` does not, which is the difference between a state where Fanatics is
licensed and one with no legal sports betting at all. `1dc` resolving matches
`an_fanatics`' DC id 3679.

This does **not** overturn the 2026-08-12 closure below, which rests on the
operator checking the *app* and finding the board behind a login. It overturns
one premise of it — that there is no web host to find. The `404` readings from
`sportsbook.1il.betfanatics.com` that the closure note called moot were taken
from third-party egress; from Illinois egress this host has never been asked.
`src/sources/research.py` now points there instead of at the marketing redirect.

> **Superseded the next day.** Asked from Illinois egress on 2026-08-14, all five
> per-state hosts serve a byte-identical 548-byte nginx `404`. The DNS map is a
> real licensure signal and is **not a route**, and pointing the research profile
> at it was wrong. See § "Fanatics has no anonymous board on the web" above.

**bet365**, for completeness, re-confirming the 2026-08-13 measurement already
relied on: `www.il.bet365.com`, `www.pa.bet365.com` and `www.nj.bet365.com`
resolve via Cloudflare; `www.zz.bet365.com` and `nonsense-control.bet365.com`
are NXDOMAIN. The research profile's stateless `https://www.bet365.com/` — the
origin every failed probe used — is corrected to the per-state host.

## Caesars is gated by an AWS WAF token, not by egress — and its odds are not on REST at all — 2026-08-12

Captured with `python scripts/recon_sources.py caesars --state IL --mode chrome
--include-all --capture-dom --wait-ms 45000` from the operator's **Illinois**
egress (`egress_state.json` state `IL`, fingerprint `34e56847c7ec…`), against the
page the operator confirmed the same day shows odds **while logged out**:
`sportsbook.caesars.com/us/il/bet/`. 146 responses, 38 websockets, manifest
`data/research/caesars/IL/20260812T175756Z/`.

**Real Chrome, logged out, Illinois, and the v4 endpoints still 403.** Every 403
body is the same 919-byte CloudFront page:

| path under `https://api.americanwagering.com/regions/us/locations/il/brands/czr/` | Chrome |
| --- | --- |
| `sb/features` | 200 (12,474 B) |
| `sb/bets/configuration` | 200 (579 B) |
| `sb/v3/sports-menu` | 200 (33,437 B) |
| `sb/v3/teamMetadata` | 200 (84,624 B) |
| `sb/v4/home` | **403** |
| `sb/v4/navigation-items` | **403** (twice) |
| `sb/v4/sports/homepage/quick-picks` | **403** |
| `gw/growth/v4/sports/homepage/banners` | **403** |

So **v4 is not what a browser gets either**, and the long-standing plan to
"re-point the adapter from v3 to v4" is dead on the evidence rather than
untried. It was already recorded as measured in the 2026-08-04 session and
mis-carried forward as an open variable.

**The gate is the token, and this is the decisive pair.** The same two v3 paths
were then requested through the repository's own default transport
(`build_default_client`, Chrome-impersonated `curl_cffi`) from the same egress,
minutes later:

| path | Chrome | `curl_cffi` |
| --- | --- | --- |
| `sb/features` | 200 | **200** |
| `sb/v3/sports-menu` | 200 | **403** |
| `sb/v3/teamMetadata` | 200 | **403** |

One endpoint answers both clients; two answer only the browser. The exit IP is
identical, the state is identical, the TLS fingerprint is Chrome in both cases —
`curl_cffi` already impersonates it, and has since before any of these probes.
What the browser has and the client does not is an **AWS WAF token**: the page
loads `b470c5d1aeb4.edge.sdk.awswaf.com/…/challenge.js`, posts `mp_verify`, and
carries telemetry to the same host. That is the whole difference.

This retires three explanations that have each cost a session: it is not egress
identity (five books answered from this exact egress in run 6 while Caesars
alone was blocked), not geolocation, and not TLS fingerprinting. **Deriving the
token outside the browser is defeating an anti-automation control and is out of
scope permanently** — not a judgement call to revisit, see `venues.md`'s "never
bypass a challenge" and `AGENTS.md` § Scrape.

**Odds never traverse REST.** `v3/sports-menu` is a 19-sport catalogue —
`sportId`, `name`, `eventCount`, `competitions`, `displayOrder` — with **no
prices anywhere in it**, and
`https://api.americanwagering.com/regions/us/locations/il/brands/czr/sb/v3/events/highlights/`,
the path `src/sources/caesars.py:151` actually asks for, **was never requested by
the page at all**. The prices arrive over a Diffusion push socket:

```
wss://api.americanwagering.com/regions/us/locations/il/brands/czr/diffusion?ty=WB&v=25&ca=10&r=0
```

binary frames, 8 sent / 9 received in the capture window (`ty=WB` = WebSocket
binary, `v=25` = Diffusion protocol 25). The other 34 sockets are GeoComply
(`wss.plc-gc.com:9703-9705`, 11 each, no frames) and one
`be-push.us.williamhill.com` — William Hill being Caesars' platform.

So a working Caesars adapter needs **both** a browser session that legitimately
holds the WAF token **and** a Diffusion client speaking a binary push protocol —
not a re-pointed URL. That is a materially larger build than any prior estimate,
and it is the honest reason this book has stayed unreachable while five peers
were promoted.

### Fanatics: closed — the odds board is behind a login — 2026-08-12

The operator checked the Fanatics app directly on 2026-08-12: **the board is not
browsable pre-login.** Combined with there being no browser sportsbook at all
(`sportsbook.fanatics.com` redirects to the marketing site `betfanatics.com`),
this closes the venue for a repository whose whole collection model is anonymous.

There is no remaining first-party path, so **Fanatics is not a reachability
question any more and should not be reopened as one**. It stays a required book
in both IL and PA (`an_fanatics` ids 2990 / 2791), which means it stays at
`SINGLE_SOURCE` until a **second same-licence republisher** exists — that, not
first-party work, is the only thing that moves it. The earlier `404` readings
from `sportsbook.1il.betfanatics.com` were taken from a third-party egress, were
never re-measured from Illinois, and are now moot.

## Pennsylvania first-party routes promoted — 2026-08-08

`configured=PA detected_egress=PA` throughout (each run's collect printed the
match; the egress *fingerprint* is not persisted per run, and the stored
`egress_state.json` was re-detected — with a rotated IP, hence a new
fingerprint — 3 ms before run 32, so no per-run fingerprint claim is made
here). Three routes moved TEMPLATE → VALIDATED on the bar
`../MULTI_STATE.md` sets — parser-clean quotes through matching egress, twice each:

| book | runs | quotes | rejections | replay |
|---|---|---|---|---|
| FanDuel (`sbapi.pa.sportsbook.fanduel.com`, `state=pa`) | 29, 30 | 1579 / 1967 | 0 / 0 | PASS / PASS |
| BetRivers (`rsiuspa`, `market=US-PA`) | 28, 29 | 3699 / 3698 | 0 / 0 | FAIL* / PASS |
| DraftKings (`US-PA-SB`) | 29, 31 | 130 / 130 | 0 / 0 | PASS / PASS |

\* Replay has no per-source filter. Run 28's FAIL is entirely DraftKings'
pre-trueOdds-fix rows drifting against the fixed parser; BetRivers' own run-28
rows replay without drift. Its second fully-PASS run is 29.

Two defects had to fall first, both caught by this validation pass:

- **DraftKings recorded the page's rounding, not the price.** The
  `sportscontent` payload carries `trueOdds: 1.46948357` beside
  `displayOdds.decimal: "1.46"`, and the converter preferred the printed string.
  Measured on this capture, `trueOdds` agrees with the published American price
  on **122 of 122** selections, the display value on only about two-thirds
(66-68 depending on the comparison metric), drifting to 2.06%.
  Fourteen `odds_format_mismatch` errors failed the run; the parser now prefers
  `trueOdds`. Run 28's stored DraftKings rows predate the fix and its replay now
  FAILs with exactly that drift — the replay checker refusing pre-fix evidence,
  which is why DraftKings' second clean run is 31, not 28.
- **FanDuel's `TO_RECORD_*` player-threshold props were neither collected nor
  declared**, so `TO_RECORD_6+_REBOUNDS_WNBA` and the 8+ sibling rejected run
  28. Declared out of scope as a family (`TO_RECORD`), not as the two instances,
  because the threshold and statistic both vary.

What a PA run says after promotion (run 32, core tier, 4627 quotes):
`state_native_sources_below_two` is gone, three books cross-compare, 2
opportunities from 172 cross-book markets, and the remaining ERRORs are two:
`required_book_missing: PlaySugarHouse` — the deliberate sentinel — and
DraftKings' `scopes_refused` (the eventgroup denials below, equally deliberate
in staying loud).

Still TEMPLATE, with the day's measurements on the route:

- **BetMGM** — HTTP 400 `Access id not allowed for application`: the configured
  access id is another state's. The PA web app has to yield its own id
  (recon, not guessing).
- **Caesars** — blocked at the CDN edge (`request blocked` marker) from PA
  egress, so egress alone did not clear the refusal seen from IL.
- **DraftKings caveat**: eventgroups 94682, 42133 and 40253 answer `access
  denied` from this egress, so MLB and tennis produce while WNBA/NFL/NHL do
  not. `scopes_refused` keeps that failing loudly; the VALIDATED status claims
  the route is real, not that the shelf is whole.

### The links the promotion made wrong, and the suite that texted — 2026-08-08

Second adversarial round over this work, two findings that matter beyond prose:

**The arb-link layer was state-blind while the promo layer was not.**
`betlinks.SITE` pinned `betrivers_kambi` to `il.betrivers.com` and `caesars` to
`/us/il/bet`; `jurisdictions.PromoRoute` had carried the correct per-state
BetRivers doors all along. Promoting PA made it operative: run 32's two real
opportunities each carried `il.betrivers.com` on a leg priced from
`rsiuspa`/`US-PA`, under "PLACE BOTH NOW" — another licence's site, which does
not show this state's slip. `bet_link` now takes the governed state and a
`STATE_SITE` table resolves BetRivers/Caesars/Unibet per jurisdiction (agreement
with the promo layer pinned by test); the SMS, the dashboard payload and the
promo planner all thread it through. Ungoverned runs get a stateless brand door,
never a confidently wrong state's.

**The test suite was texting the operator.** Five CLI tests drive
`main(["arb"])` over a genuine 4.76% synthetic arb; `alert_ready()` is true by
default on a signed-in Mac, so every full-suite run reached osascript with the
real number — unnoticed because `notify` is fail-soft and the tests pass either
way. The hermetic conftest fixture now replaces `DEFAULT_BOOK.send` with a
refusal, pinned by a test whose identity check fires before anything could
deliver.

Also from the round: `an_open` was the one Action Network descriptor issuing a
`bookIds`-less v2 request — a shape no capture has ever measured — on every
batch's global pass. It now names id 30, and a test keeps every live AN request
on the named-ids shape across the global list and all four states.

## The Pennsylvania recapture — 2026-08-08T16:38Z

`configured=PA detected_egress=PA` (fingerprint `78e5c3810511` as printed by
`detect_state.py` two minutes before the run; fingerprints are not persisted
per run, and the connection's IP had rotated by the day's later runs), 12:38 ET
on a Saturday, so the whole slate was pregame — the condition the 2026-08-07
attempts lacked. Run 27, thirteen sources asked, alerts off.

| source | id | brand | quotes | events |
|---|---|---|---|---|
| `an_betrivers` | 122 | BetRivers PA | 376 | 35 |
| `an_parx` | 74 | Parx | 376 | 35 |
| `an_caesars` | 1906 | Caesars PA | 331 | 35 |
| `an_fanduel` | 255 | FanDuel PA | 331 | 35 |
| `an_unibet` | 246 | UnibetPA | 316 | 35 |
| `an_draftkings` | 1534 | DK PA | 301 | 35 |
| `an_betmgm` | 280 | BetMGM PA | 267 | 35 |
| `an_thescore` | 4623 | theScore Bet PA | 250 | 19 |
| `an_bet365` | 3547 | bet365 PA | 173 | 19 |
| `an_fanatics` | 2791 | Fanatics PA | 150 | 15 |
| `an_fliff` | 2292 | Fliff | **0** | 0 |
| `an_circa` | 78 | Circa | **0** | 0 |
| `an_superbook` | 14 | Westgate | **0** | 0 |

**2871 quotes across ten of Pennsylvania's twelve republished feeds**, where nine
of them had been dark. betPARX, theScore Bet PA and Unibet PA produced rows for
the first time. The three zeros are the ids that carry no odds at all, recorded
above.

MLB captures for `an_parx`, `an_thescore` and `an_unibet` replaced the committed
fixtures, which asked New Jersey ids (1929 / 247 / 4620) through v1 and parsed to
nothing. Offline, those three now parse to 255 / 225 / 195 rows with **no
rejections** — the `periods=` fix demonstrated end to end rather than argued.
Windows per book, measured on the committed bytes: 74 and 4623 carry
`full_game` + `first_5_innings` + `first_1_inning`; **246 has no `firstinning`
key on any of its 15 games** (full-game and first-five only), so a missing F1
row for Unibet PA is that book's shelf, not a defect.

The run raised `prices_disagree_with_every_other_source` against `an_caesars` —
30 of 312 shared markets more than 0.15 of implied probability from the rest,
worst 0.35, on ATH@BOS, ATL@NYY, BAL@TEX and others. **All thirty are
`moneyline/first_1_inning`, and Caesars is right.** From the raw payload for
ATH@BOS:

| book | first-inning moneyline | vig |
|---|---|---|
| 122 BetRivers PA | home +210, away +400, **draw −129** | 8.6% |
| 1906 Caesars PA | home −210, away +170, **no draw** | 4.7% |

Two coherent books pricing **different bets**. The three-way home leg loses when
the inning is scoreless; the two-way one does not, which is the whole 0.323 →
0.677 difference. betPARX and theScore Bet post the three-way market too, so
Caesars was the minority of one and got graded for it.

No position was ever at risk: `src.arb` groups by `contract_shape` and refuses a
two-way moneyline in a draw-pricing window outright (`ambiguous_tie_settlement`),
which is a rule that already names first-five-innings as its case. The defect was
confined to the report — but an ERROR firing every run for a benign reason is how
a report stops being read, so `_check_price_agreement` now carries the contract
shape in its grouping key, the same distinction `src.arb` was already making. The
Pennsylvania board revalidates with that error gone and the swap detection intact.

Two things this run did **not** settle:

- Every republished row is view-only, so the run reports 0 comparable markets and
  0 arbitrage. Correct, not a regression: none of these is a counterparty.
  Pennsylvania still has no first-party route promoted, which is the next step.
- `an_fanatics` (15 events) and `an_bet365` (19) cover materially less of the
  board than the 35 the rest return. Not investigated.

## Multi-state routing update — 2026-08-03

Live collection now supports IL, PA, NJ, and DC batches. The detected current
state is always included; additional states are explicit. Caesars and Hard Rock
NJ values now live only in the NJ route map. Illinois uses only
`locations/il`/`segment=il`; failed exact-state requests remain failed and never
fall back to NJ. NJ/DC/PA route records are templates pending matching-egress
parser-clean probes. Republished Action Network, VegasInsider, and VSiN rows are
global diagnostic observations and cannot form arbitrage legs.

## Pennsylvania routing status — 2026-08-03

Pennsylvania is configured but not yet live-validated. The active-state map
selects FanDuel `pa`, BetRivers `rsiuspa` / `US-PA`, BetMGM's PA host and
subdivision, DraftKings `US-PA-SB`, and Caesars `locations/pa`. FanDuel promos
send only `PA`; BetRivers promos request only `pa.betrivers.com`.

Hard Rock is not in the Pennsylvania Gaming Control Board's authorized online
sportsbook list, so the map marks it unavailable instead of inventing a PA
segment. The registered adapter and comparison feeds remain in the repository.

An Illinois-exit template probe on 2026-08-03 produced parser-clean payloads for
FanDuel (72 MLB quotes), BetRivers (486), and DraftKings (86). BetMGM returned
HTTP 400 (`Access id not allowed for application`), so its PA access ID still
needs discovery from the public PA application. Caesars was blocked at
CloudFront. Hard Rock is unavailable in PA. These are structural results only:
3/5 candidate routes produced quotes, and none was promoted to validated.

The same session's public egress resolved to Illinois, so the real PA probe was
correctly refused. A later automated lookup attempt from the managed development
environment was also blocked by its privacy approval layer; no PA `ok` cache
rows were written. Live certification still requires running the commands below
from a legitimate Pennsylvania connection and consenting to the IP lookup.

From Illinois, inspect route structure without creating validation evidence:

```bash
ODDS_STATE=PA python scripts/probe_sources.py --state PA --template-only
```

To promote individual PA routes, run `detect_state.py` on a legitimate PA
egress, then run `probe_sources.py --state PA --force`. Only parser-clean quotes
under a matching recent detection can write `ok` to the separate cache.

## Illinois re-probe — 2026-08-03

This is the current baseline from the Illinois connection. It supersedes the
older California reachability notes below; those remain as history because they
show which failures are egress-sensitive.

The Illinois Gaming Board's current [authorized sportsbook
list](https://igb.illinois.gov/sports-wagering/sports-authorized-operating-sportsbooks.html)
names ten online operators: bet365, BetMGM, BetRivers, Caesars, Circa,
DraftKings, theScore Bet, Fanatics, FanDuel, and Hard Rock Bet. "Available in a
comparison table" does not by itself mean an operator is legal or usable in
Illinois. In particular, the IGB has issued cease-and-desist letters to
[Polymarket, Kalshi, and Bovada](https://igb.illinois.gov/sports-wagering/cease-and-desist-letters.html).

**Operator decision, 2026-08-09:** asked directly how Kalshi and Polymarket US
should be presented on Illinois runs given those letters, the operator chose
"keep as-is (takeable)" — no per-state exclusion table is added. What that
mechanically means today: **Kalshi** stays in the nationwide-takeable set on
IL runs, unchanged. **Polymarket** has no takeable referent to keep: the one
registered `polymarket` key reads the offshore book and sits in
`US_UNAVAILABLE_SOURCE_KEYS`, and no `polymarket_us` key exists yet — nothing
named Polymarket is takeable on an Illinois run today. The decision's
Polymarket half is therefore *prospective*: when `polymarket_us` is registered
(after the operator's KYC and keys), it enters the takeable set without an IL
carve-out unless the operator revisits this. (Bovada, the third letter
recipient, is US-unavailable on independent grounds.) An earlier revision of
this note said these feeds "must not be presented as Illinois betting
counterparties"; that wording described the letters' position, and the
recorded decision above — not this note — is the repository's answer. The
letters themselves remain a fact worth re-checking if the IGB escalates.

### First-party acquisition follow-up — 2026-08-04 UTC

All results below used the recorded Illinois egress fingerprint and anonymous
application traffic. Research capture stores only sanitized URLs, safe headers,
response bodies, text WebSocket frames, and the egress hash under ignored
`data/research/`; it never persists the exit IP, proxy URL, cookies, or tokens.

- **Hard Rock IL is repaired and live-validated.** The application proved
  segment `il`, channel `ILLINOIS_ONLINE`, millisecond event timestamps, and
  selection-name handicaps. Two runs returned 202 quotes with zero rejections;
  a later probe returned 172 with zero. A genuine dated RawResponse set replays
  to more than 100 quotes without network access.
- **Caesars IL remains blocked at the first-party core-odds boundary.** The
  exact IL application, v3 sports menu, metadata, and sockets answer, but v4
  navigation/home/quick-picks and current v3 MLB highlights return CloudFront
  403 in headed, headless, and persistent sessions. This needs operator-approved
  access or a naturally successful anonymous response, not WAF bypass code.
- **Fanatics currently exposes sportsbook odds through its native apps.** The
  [operator describes the sportsbook as an iOS/Android
  experience](https://www.fanaticsinc.com/fanatics-sportsbook-online-experience);
  its sportsbook web host redirects to the marketing site and yields no
  anonymous first-party sportsbook payload. Native-client research remains,
  without login or untrusted APK installation.
- **bet365 IL is viable but not registered.** A normal browser displays a full
  anonymous Illinois MLB slate, and the exact IL configuration plus compact
  publisher protocol were identified. Fresh isolated Chromium and stable-Chrome
  profiles load the correct IL shell but their catalog subscription stalls at
  the application preloader, so unattended collection is not yet repeatable.

The exact identifiers proved that day, so a later session does not re-derive them:
Caesars IL answers under `sportsbook.caesars.com/us/il/bet/` and its v3 sports menu
proved the hard-coded competition list stale (current MLB id
`04f90892-3afa-4e84-acce-5b89f151063d`, now in `src/sources/caesars.py`); bet365 IL is
`www.il.bet365.com` with state code 28, IL locale, the compact pull-pod protocol and
`365lpodds.com` sockets; Fanatics' `sportsbook.fanatics.com` redirects to the
`betfanatics.com` marketing site. The generated Caesars and bet365 browser profiles
were deleted after diagnosis, so no anonymous session cookies were retained — only the
sanitized ignored manifests remain.

### Redundant feed coverage

Republished rows retain their own source keys for provenance but are one
counterparty with the underlying sportsbook. `src.redundancy` prevents any two
of those observations from becoming opposite legs of an arbitrage and flags
material price drift instead.

| Book | First party | Action Network | Independent comparison | Current Illinois result |
|---|---|---|---|---|
| DraftKings | `draftkings` | `an_draftkings` | `vi_draftkings` | **Three paths registered.** Current first party uses the official `sportscontent` endpoint through browser transport: 48 MLB quotes, zero rejections. AN and VI both produced quotes. |
| Caesars | `caesars` | `an_caesars` | `vi_caesars` | Three paths registered; first-party CloudFront is blocked, while AN and VI produced MLB quotes. |
| FanDuel | `fanduel` | `an_fanduel` | VI column verified, adapter not registered yet | First party 48 MLB quotes; AN 112. |
| BetMGM | `betmgm` | `an_betmgm` | VI column verified, adapter not registered yet | First party 48; AN 104. |
| BetRivers | `betrivers_kambi` | `an_betrivers` | VI `RiversCasino` column verified, adapter not registered yet | First party 476; AN 136. |
| Hard Rock Bet | `hardrock` | `an_hardrock` | `vi_hardrock` | **Three paths registered.** First party now returns current IL core markets parser-clean; AN v2 and VI provide diagnostic comparison rows. |
| bet365 | not implemented | `an_bet365` | `vi_bet365` | AN and VI produce comparison rows. A normal browser shows first-party IL odds, but fresh isolated automation does not complete its catalog subscription. |
| Fanatics | not implemented | `an_fanatics` | `vi_fanatics` | AN v2 and VI produce comparison rows. The current web host is marketing-only; native-app first-party discovery remains. |
| Circa | app-only surface; no web adapter | `an_circa` | `vsin_circa` | Circa's own site points to VSiN as an odds aggregator. The named VSiN column produced 48 MLB quotes / 8 events with no rejections. It is a Las Vegas line tracker, so use it as fallback and drift evidence rather than proof of Illinois-state price identity. *(2026-08-09: `an_circa` has since been deregistered — see `action-network.md` § "Three registered ids carry no odds"; `vsin_circa` is now Circa's only feed.)* |
| theScore Bet | verified IL GraphQL surface; adapter pending | none | none | Official `us-il` edge returned a valid anonymous startup, MLB competition, and lines payload. This is the strongest next first-party adapter candidate. *(2026-08-10: superseded — `an_thescore` now carries IL id 4601 and is proven: 74 soccer rows, run 35, replay PASS; see `action-network.md` § "theScore Bet's Illinois id answers". The "none" in the Action Network column describes the 2026-08-03 catalogue state.)* |

The retained Action Network feeds were rechecked from Illinois on 2026-08-03.
The old v1 scoreboard returned games but omitted all six requested books. The
current Action Network web board revealed a v2 endpoint and grouped `markets`
schema. After adding dual-schema parsing, v2 restored real 48-quote MLB captures
for Hard Rock, Fanatics, and Bally. Fliff, Circa, and SuperBook still returned
zero requested-book rows across MLB, WNBA, NFL, NHL, and soccer. Those remaining
three cannot supply the real, non-empty captures required by the offline
source contract. *(This paragraph's ending is superseded: on 2026-08-09 the
operator approved deregistering all three rather than waiting — see
`action-network.md` § "Three
registered ids carry no odds" — and the full suite is green. `vsin_circa`
remains registered as Circa's fallback.)*

### Pennsylvania: no book reaches the two-feed bar

Recorded so this is not rediscovered. Under the rule in `AGENTS.md`
§ Scrape, a book counts as observed through one first-party route **or** two
republished feeds carrying *Pennsylvania's own* licence that agree on price.

**No PA book reaches the two-feed bar.** Ten of the eleven file exactly one
same-licence feed — Action Network's per-state book id — and PlaySugarHouse files
none at all. The only second feed available anywhere is VegasInsider, whose
`/odds/las-vegas/` column is a different licence of the same brand; it is
recorded as context and excluded from both the count and the price comparison.

That is a statement about the *second* path only. Five books are satisfied
through the **first** path — a first-party route — and the table's own "best
reachable state" column says so. The six with no first-party route are the ones
with no way to comply at all.

**2026-08-13: theScore Bet now has a PA route and is still not satisfied by it.**
`thescore` was registered with a `TEMPLATE` Pennsylvania route on a DNS-confirmed
edge that has never been asked over HTTP. The count above is unchanged on
purpose: `Access.DIRECT` is graded on `direct_rows > 0`, so a route that has
produced nothing lifts nothing, and naming it in `REQUIRED_BOOKS` says which key
*would* be the direct route rather than claiming one exists in practice.

| Book | First party | Same-licence feed (AN book id) | Cross-licence only | Best reachable state |
|---|---|---|---|---|
| BetMGM | `betmgm` | `an_betmgm` (280) | `vi_betmgm` | `DIRECT` |
| BetRivers | `betrivers_kambi` | `an_betrivers` (122) | `vi_betrivers` | `DIRECT` |
| Caesars | `caesars` | `an_caesars` (1906) | `vi_caesars` | `DIRECT` |
| DraftKings | `draftkings` | `an_draftkings` (1534) | `vi_draftkings` | `DIRECT` |
| FanDuel | `fanduel` | `an_fanduel` (255) | `vi_fanduel` | `DIRECT` |
| bet365 | none | `an_bet365` (3547) | `vi_bet365` | `SINGLE_SOURCE` (warning) |
| Fanatics | none | `an_fanatics` (2791) | `vi_fanatics` | `SINGLE_SOURCE` (warning) |
| betPARX | none | `an_parx` (74) — **unproven** | none | `SINGLE_SOURCE` (warning) |
| Mohegan Pennsylvania | none | `an_unibet` (246) — **unproven** | none | `SINGLE_SOURCE` (warning) |
| theScore Bet | `thescore` — **`TEMPLATE`, never asked over HTTP** | `an_thescore` (4623) — **unproven** | none | `SINGLE_SOURCE` (warning) |
| PlaySugarHouse | none | none | none | `MISSING` (error) |

**The three unproven feeds.** `an_parx`, `an_unibet` and `an_thescore` are the
*only* observation path for their books, and no capture in the repository shows
any of them returning a row. The captures taken 2026-08-07T01:55Z asked for the
New Jersey ids 1929 / 247 / 4620 and came back carrying odds for other books on
the same games (`{15, 30, 123}`, `{15, 30, 68, 69, 71, 75}`, `{15, 30, 69, 75}`)
— the requested book was absent, not the slate. The Pennsylvania ids 74 / 246 /
4623 have never been requested. So for these three the coverage table asserts a
watcher that is not known to answer; if a live capture confirms the ids are
wrong, the honest record is no working feed rather than a thin one. Until then
they are `SINGLE_SOURCE` on paper and `MISSING` in practice. *(Resolved
2026-08-08: the recapture asked all three PA ids and every one answered —
255 / 195 / 225 rows, § "The Pennsylvania recapture". The 4623 capture was
later retired from the fixture store by the 2026-08-09 IL licence swap, its
proof standing on record — `action-network.md` § "theScore Bet's Illinois id
answers".)*

**The three new VegasInsider columns.** `vi_betmgm`, `vi_betrivers` and
`vi_fanduel` are the opposite case — the adapters work and the captures are the
problem. Their 2026-08-07T01:55Z captures parse to zero rows, but so does
`vi_caesars` against that same overnight page, and feeding the live 2026-08-03
page to each of the three new parsers yields 48 quotes apiece. They need a
recapture on a live slate, not a code change; `test_the_adapter_produced_rows_at_all`
would currently fail on all six, masked only by the pre-existing
`an_fliff` / `an_circa` / `an_superbook` fixture gap that errors first.
*(Resolved 2026-08-10: the dead-slate captures were replaced with live
IL-egress ones parsing 18/54/54 rows, and the fixture gap is gone with the
deregistration — see `action-network.md` § "One stale board retired for vintage
coherence".)*

bet365 and Fanatics previously read as cross-checked; their second feed was the
Las Vegas column, so they no longer can. For the six books with no first-party
route the only path to compliance is a genuine Pennsylvania first-party adapter
or a second PA-licensed republisher — **not** relabelling a national feed as
local, which `src/coverage.py`'s import-time invariant refuses outright.

`an_bally` and `an_hardrock` are `UNAVAILABLE` in PA (no licence) and are not
built there.

### First-party endpoint findings

| Source | Result from Illinois |
|---|---|
| FanDuel IL | working, 48 MLB quotes / 8 events |
| BetRivers IL | working, 476 / 8 |
| BetMGM IL | working, 48 / 8 |
| DraftKings IL | **repaired**: current browser-observed `sportscontent/.../leagueSubcategory/v1/markets` route returns 43 KB JSON and parses 48 / 8; retired v5 captures still replay |
| Caesars IL and legacy NJ | both `403` / request blocked |
| Hard Rock IL | **working and validated**: current route returns parser-clean core markets; NJ remains template-only pending NJ egress |
| Pinnacle | `401 No authorization token provided`; guest-token discovery required |
| bet365 | normal browser shows full IL MLB odds; fresh isolated browser catalog subscription stalls |
| Fanatics | sportsbook web host redirects to marketing; first-party discovery remains native-app work |
| Bally Kambi | `429 No access` |
| Circa | official site says the complete real-time menu is in its mobile app; no public first-party web odds surface found. Its site explicitly lists VSiN and WagerTalk as aggregators, so `vsin_circa` is the independent fallback. |
| theScore Bet | **first party verified**: `env.js` exposes the public GraphQL host; the default edge redirects this Illinois egress to `sportsbook.us-il.thescore.bet`. Anonymous `Startup`, persisted `CompetitionPage`, and `CompetitionPageSectionLinesTabNode` calls all returned `200`. The MLB lines payload contained 47 event nodes, 55 markets, and 147 selections with structured American odds. Parser/capture work remains. *(2026-08-13: **the adapter shipped** — see § "theScore Bet is registered". Superseded — re-measured end to end, and two details here are wrong. The redirect is a `302` **on the API**, not on the page; and the persisted queries are `GET`s that the endpoint does not require at all, since a plain anonymous `POST` of a query document answers `200`. See § "theScore Bet: the anonymous surface, measured end to end".)* |

The theScore request sequence is state-sensitive and should be implemented as
one adapter rather than hard-coded curl calls: fetch the official public bundle
for current persisted-query hashes, call `Startup` on the resolved regional
edge, keep the anonymous token in memory only, then capture the competition and
lines responses. The startup payload includes the raw exit IP, so it must be
sanitized before persistence; only the region and an IP fingerprint belong in
diagnostic storage.

The full endpoint probe returned usable JSON for 15 of 22 research candidates.
Working non-retail sources included LeoVegas, Matchbook, Smarkets, SX Bet,
Cloudbet, and 1xBet. Those results establish transport health; they do not make
an unlicensed venue an Illinois counterparty.

Everything below was first measured on **2026-07-28** from a host in Davis,
California with plain `httpx`. That pass classified several big books as
permanently closed. **That policy is retired.** The collector now defaults to
Chrome TLS impersonation (`curl_cffi`) and will use `ODDS_HTTP_PROXY` when set.
Rows below that still say "closed" mean *not yet opened with the new transport*,
not *forbidden to open*. Re-probe with:

```bash
python scripts/probe_sources.py --only blocked --template-only
```

---
