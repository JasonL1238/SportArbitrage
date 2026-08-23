# Promotions: what each catalog actually served

What each promo source answered when asked, which offers confirmed which states, and what
a state label on a promo run really asserts. Newest section first, dated headings, literal
results — a refusal is recorded in the same detail as a success.

## betPARX and theScore Bet: the two PA-only books get first-party promo feeds — 2026-08-23

The operator arrived in Pennsylvania the same night (`state-routing.md` §
"Pennsylvania arrival"). A PA promo collection from that egress (run #75, 57 offers
from 13/24 sources) showed the gap plainly: the two books that can only be claimed
here had **no promo source at all**, and the first-party pages of FanDuel
(PerimeterX), Caesars (403), bet365 (no promo signals) and Fanatics (404) still
refuse from PA, so their welcome offers arrive only second-hand via TheLines.

### betPARX — `betparx_kambi`, a JSON catalogue per state lobby

`pa.betparx.com` is a Playtech lobby. Its `/promotions` page renders from
`GET /initialResources/promotionsConfiguration/en_US` (200, 21.6 KB): six promotions
keyed by internal name, each with a headline, `product`, guest visibility, a
scheduler window and two web-content ids. Those blocks are public too —
`GET /webContent/en_US_{id}` (200, `text/plain` HTML fragment) — and the page
itself fetches them, which is how the path was found (the `webContent/{id}_en_US`
guess 404s to the SPA shell). No cookie, token or login anywhere.

What it held on 2026-08-23 (PA): **"New Users Who Sign Up Will Receive 100% Bet
Insurance on Their First Sports Bet up to $50"** (sportsbook; terms block
`TC_BET_INSURANCE_SIGNUP_OFFER`, 30 KB: $10 min deposit, first cash bet back as a
Bonus Bet if it loses; scheduler 2026-07-20 → 2026-08-20, so it is past its
window and the tile now carries the Eagles boost as "Sports Welcome Offer");
**"Lock It In: Birds +50 Points Boosted to +100 Odds"** three times (weeks 1–3,
2026-08-21 → 09-28, new users also get four 50% profit boosts); a casino
welcome (skipped, `product:casino`); and a promo-code stub whose terms id is
`TERMS_FROM_IMS` — Playtech's marker for terms served only inside the account
system, so none are fetched; each public block is fetched once even where three
weekly boosts share one terms id. `nj.betparx.com` is the same shape.

Adapter: `src/promos/betparx.py`, built per state from
`PromoRoute.betparx_url` (PA and NJ; no IL/DC licence, so no entry there), region
stamped from the host like the BetRivers landing. Live from PA: **5 offers, 10
requests, 0 rejections, 1 skipped, 0 unconfirmed**. Fixtures:
`betparx_promotions_configuration_pa.json` and the two bet-insurance web-content
blocks.

### theScore Bet — `thescore`, the help centre's promotional-terms section

The sportsbook host serves no promotions page anonymously: `sportsbook.us-pa.
thescore.bet/promotions` is `Not Found` and `sportsbook.thescore.bet/promotions`
is a 3.5 KB shell. The account menu in the app's own startup payload
(`data/research/thescore/IL/…/response-111.json`) points at two public help-centre
pages, and the second is the catalogue: **`https://sportsbook.thescore.bet/legal/
promo-terms`** (Zendesk, 200, 31.6 KB) lists every running promotion's full T&Cs as
an article — 10 promotions plus the section's own terms and the responsible-gaming
policy, skipped by name. Each article is the complete document, including the
state clause enrich reads (`Must be physically present in MI, NJ, PA, or WV` →
eligible MI/NJ/PA; `geo` drops the trailing "or WV", a known quirk recorded
below under 2026-08-16).

What it held: "$50 Deposit Match" and "$100 Deposit Match" (100% matches, PA
confirmed, invited players), "Bet $10, Get $30 Bonus Bet" (a Hollywood Casino
cross-sell, invited, PA confirmed), "Live Bet & Get" ($10 Bet Reset, invited),
"Kickoff Rewards", "Baseball Ticket Contest" (Ontario), three profit-boost packs
and "Anytime Goalscorer Insurance" (PA confirmed). **No public welcome offer** —
the sign-up bet-and-get lives in the app behind registration, so TheLines stays
the only second-hand reader of it.

Adapter: `src/promos/thescore.py`, a global source (the help centre is
brand-wide; eligibility comes from the terms). Invitation-only articles are kept
and labelled `invited players only` rather than dropped. Live from PA: **10 offers,
11 requests, 0 rejections, 7 PA-confirmed**. Fixtures: the index and two
articles.

### One classifier change, because both catalogues are whole T&C documents

`classify_kind` reads its inputs as one blob, first rule wins, so a sign-up
offer whose 30 KB terms mention "profit boost" before "insurance" came out
`profit_boost`, and deposit matches whose boilerplate says "bonus bets" came out
`bonus_bet`. Both adapters now classify the headline first and fall back to the
terms only when the headline says nothing; and "bet insurance" / "insurance"
joined the `RISK_FREE` needles beside "first bet back", which is the same shape.

### Contests are a kind now, and the planner refuses them

Six of BetMGM's twelve live cards ("$100k Football Frenzy: Win Bonus Bets",
"Fast Break: Win a $50 Bonus Bet & Other Daily Prizes", "Goal Rush: $500,000 in
Bonus Bets", two Pick 'Ems, "Pick A Twin To Win") were typed `bonus_bet` off their
titles and sat at the top of the plan with per-$100 conversion figures for credit
nobody holds. `PromoKind.CONTEST` now exists; the classifier's first rule catches
free-to-play / pick 'em / prize-pool phrasing and pool-scale dollar figures
("$500,000 in", "$2 million"); the planner names it and computes nothing. Checked
against the same run: all six re-read as `contest`, the DK "$5 → $150", TheLines
"$10 → $150 if you win" and theScore's "$10 → $30" still read as bonus bets. The playbook text (`src/promos/strategy.py`) names a
contest first for the same reason — its `reward_type` reads `bonus_bets` and the
bonus-bet branch would have told the operator to hedge a prize pool.

### Spending a promo from the dashboard — 2026-08-23

Every plan card on a served page carries **Log this plan**: the planner's legs go
into My bets as a `kind: promo` slip (credit leg as credit, hedges as cash, the
offer named on the slip, `source_run_id` the odds run the plan was priced from),
and a logged offer reads as done in the Campaign table in every browser — the
per-browser checkbox stays for offers claimed outside the page, and is disabled
once the ledger knows. Promo source names on the page come from the payload
(`BRAND_LABELS`, or `PROMO_ONLY_LABELS` for the Ontario catalogues); the JS table
that used to hold them had drifted and never learned `thescore`. Also fixed
while here: `collect --source betparx_kambi` was refused under the default
Illinois setting because the CLI's choices were the state-resolved registry.

## Scraping fixes proven live, and four path probes — 2026-08-16 (evening)

Same-day follow-up to everything below: four parsing/enrichment defects fixed offline,
then one acceptance batch (`collect --state IL --state PA --state NJ --state DC`, runs
#7–#10, 55 offers per state, all four OK) measured against runs #2–#6, plus a budgeted
live rediscovery of the moved promo paths.

### What the fixes changed, measured

| check | before (runs #2–#6) | after (runs #7–#10) |
| --- | --- | --- |
| confirmed / stored | IL 3, PA 4, NJ 4, DC 3 of 54 | **IL 5, PA 6, NJ 6, DC 5** of 55 |
| tl_betmgm welcome | 1 offer, both state lists merged | 2 offers, each with its own list |
| IL-run rows matching one fixed predicate — `terms LIKE '%physically located%' OR terms LIKE '%min%odds%' OR terms LIKE '%21+%' OR terms LIKE '%Terms%Conditions%' OR ends_at IS NOT NULL` — applied identically to both runs | 6 | **18** |
| `ends_at` non-null (IL run) | 6 (all DraftKings API-native) | 8 (+cloudbet, +tl_betmgm via the new copy-date extractor) |

- **The eligibility feedback loop was real and is gone.** Enrichment ran 3× per collect
  and re-parsed its own generated notes; `_EXCLUDE_CTX` read through the `;` in
  "Not available in: OR; Eligible in: …" and inverted the whole eligible list.  Run #6
  stored DraftKings' WNBA Parlay Boost and MLB SGP(x) Boost — whose own terms name IL —
  as IL-*ineligible*; run #7 confirms both.  Fixes: notes dropped from the parse blob
  and idempotent note appending; pinned by
  `enrich_offer(enrich_offer(x)) == enrich_offer(x)`.  (A third fix shipped with the
  batch — stopping the context regexes at `;` — was **reverted the same evening** by
  the adversarial round: it made "Not available in NV; NY residents are also excluded"
  fail *open*, and the loop was already closed at the source.  Semicolons are instead
  handled where lists are split: `_codes_from_separated_list` now treats `;` as a
  separator, so both clauses of that sentence exclude.)
- **The two-promos-one-blob case is fully repaired.** TheLines' BetMGM welcome section
  now splits on its "The first:/The second:" markers: "Get up to $1,500 paid back …"
  carries the AZ…WY list (IL and DC in it → confirmed there), and "Bet $10 get $150 …
  if you win" carries exactly `[MI, NJ, PA, WV]` — confirmed in PA and NJ, labeled
  `NOT IL`/`NOT DC`.  The offer that wrongly anchored the first IL campaign table now
  reads correctly in every state.
- **Deepen now follows terms links and leaves envelopes.** 15 rows deepened; 4 through
  the new `terms_url` path; every deepen fetch is written to the raw store
  (`deepen-<slug>` endpoints) where before it left no trace.  `html_catalog` detail
  pages prefer the text after a Terms heading over the first 6000 chars of page chrome.
- **Cross-source evidence merge did not fire this batch** — no tl\_ welcome title
  soft-overlaps a first-party welcome in current data (`_is_welcomeish` needs a
  welcome-word on both sides).  The mechanism is unit-tested; the absence of
  `merged_evidence_from` on every row is the honest reading, not a failure.
- **Two wrinkles found by the batch, one fixed, one accepted.** (fixed) The fallback
  money-slice summary opened mid-word once deepen delivered real terms — bovada's poker
  welcome printed "ited States Dollars) up to a maximum of $500USD…"; the slice now
  anchors at a word boundary.  (accepted) The year-less date roll-forward turns *stale*
  marketing copy into next year: TheLines still shows "NBA Playoff Profit Boosts: Ends
  June 19th", which reads as 2027-06-19 rather than the already-passed 2026 date.  For
  live offers the roll-forward is right ("ends January 5" seen in August means next
  January); for stale copy it defers an expiry gate that arguably should fire.  Noted,
  not fixed — distinguishing the two requires knowing the copy is stale.

### The adversarial rounds: seventeen rounds, eighty-seven numbered findings, the accepted remainder listed

Two reviewers per round on the uncommitted diff, per the standing loop.  Round one:
all five regression pins verified to fail on pre-fix code; an 11-mutation sweep
killed 7, and the 4 survivors got killing tests.  Round one's seven findings, fixed
the same evening:

1. **The `;` regex boundary was fail-open and got reverted.**  Stopping the context
   grabs at `;` truncated real legal copy — "Not available in NV; NY residents are
   also excluded" excluded only NV.  The feedback loop it guarded is closed at the
   source (notes never re-enter the parse), and semicolons are now handled where
   lists split (`_codes_from_separated_list`), so both clauses exclude.
2. **`terms_slice`'s plain-text fallback anchored on junk** — footer links, i18n
   labels, script bodies — on every committed fixture containing the phrase, and
   would have *replaced* signal-bearing page text.  Now heading-marked only, with
   script/style content stripped first.
3. **`_apply_terms` applies only Terms-heading-marked slices** — an unmarked body is
   another page's chrome and neither fills nor replaces terms.
4. **Chained-collision evidence loss**: evidence folded into a winner that a later
   collision dropped vanished with it — a vague row's `IL` exclusion disappeared and
   the kept row wrongly confirmed IL.  Merges now follow the drop chain to the row
   that actually survives; pinned in both input orderings.
5. **`_ENDS_AT` matched non-end verbs** ("weekends 9/6/2026", "NBA Legends
   September 1") and read "ends September 2026" as day 20.  Word boundaries on the
   verb plus a day lookahead; five shapes pinned.
6. **Split spans with identical titles collapsed** — same mechanics under two state
   lists lost the second list to title dedupe.  The ordinal marker now suffixes a
   duplicate title; the single-marker no-split threshold is pinned too.
7. **Deepen refetched what it already knew.**  Failed fetches were never cached (a
   venue's shared dead terms link burned budget once per offer), and each state of a
   batch rebuilt the cache: batch one fetched the identical TheLines page **20
   times** (108 deepen envelopes).  Failures now cache, and one `DeepenCache` spans
   the whole batch — batch two (runs #11–#14) wrote **23 envelopes, every URL
   exactly once**, TheLines 1×.

Batch two confirms the same verdicts as batch one — IL 5, PA 6, NJ 6, DC 5 of 55 —
which is the point: the round's corrections were about evidence handling and edge
shapes, not this catalog's verdicts.

**Round two attacked round one's fixes and found six more, a third of them in the
fixes themselves — the ratio the standing loop predicts.**  Fixed the same evening:

8. **The `;` splitter invented states from clause fragments** — "ID verification
   required" read as Idaho, "OK to combine" as Oklahoma, and a lowercase "or …"
   clause as Oregon, contradicting the splitter's own docstring.  The two-letter
   fallback now requires the code uppercase in the source, and English-colliding
   codes (ID/IN/OK/OR/ON/HI/ME) only as a bare token or "XX only".
9. **The evidence fold could widen across distinct promos**: one vague first-party
   welcome (adapter-stamped IL) soft-collides with *two different* specific TheLines
   welcomes, and both absorbed the IL stamp — cross-offer contamination through the
   merge.  A loser attached to several winners, or reached through a drop chain, now
   folds narrow (ineligible only); full folds require a one-loser-one-winner direct
   collision.
10. **`<strong>Terms apply.</strong>` boilerplate counted as a Terms heading**,
    marking everything after a promo card as a terms slice.  Headings now accept
    only the full "Terms & Conditions"/"Full terms"/"Significant terms" wording;
    the broad set (T&Cs, terms apply) still recognizes *anchor* labels.
11. **An unclosed `<script>` survived the paired strip** and its JS could become a
    marked slice; text after an unclosed script/style tag is now discarded.
12. **All four live `terms_url` firings were misfires**: LeoVegas' footer link
    `/en-ca/terms` — the sitewide house rules, which naturally carry a Terms
    heading — *replaced* four offers' genuine parsed terms in runs #7–#14 (the
    "4 through the new terms_url path" above records them as they stored, not as
    wins).  Three defenses now: `_apply_terms` is fill-only into empty terms, the
    terms link is not even fetched when the offer already has terms, and bare
    `…/terms` / `/legal/` hrefs are excluded as house rules.
13. **The year-less roll-forward fabricated a 2027 end date for an elapsed window**:
    cloudbet's "start on August 13 … end on August 15" seen on the 16th stored
    2027-08-15, though its own sentence proves the run is over.  A stated past
    start date now suppresses the roll-forward, so the elapsed window reads as
    expired.  (The no-start-date case — TheLines' stale "Ends June 19th" → 2027 —
    remains the accepted heuristic.)

Batch three, after round two (runs #15–#18): same confirmed counts again; the four
leovegas_on rows carry no `deepened_terms_from` and their genuine parsed terms
survive; cloudbet's tournament row stores the elapsed 2026-08-15 and counts as
expired (the planner gates it `expired ×1`).  The signal predicate reads 13 on run
#15 vs 18 on run #7 — six junk slices (cloudbet CSS, tl\_ chrome) stopped matching
once heading-strictness landed, and the 13 survivors are real terms; still 13 vs 6.
One reviewer also measured round two's own side effect: three DraftKings rows had
carried a round-one *false* `Not available in: OR` (invented from "…leagues, games,
or sports…" by the lowercase fallback) — the uppercase gate fixed it, and DK's
copy genuinely lists OR eligible.

**Round three attacked round two's fixes and found four more (two real, two
edge-hardenings), fixed the same evening:**

14. **The ambiguous-code gate dropped exclusions in the splitter's own flagship
    shape**: "Not available in NV; OR residents are also excluded" lost Oregon —
    fail-open again, one refinement deep.  A person-word suffix
    (residents/players/customers/users/members) now reads as a state; prose
    suffixes ("ID verification required") stay English.
15. **The fold guard missed its mirror**: one specific winner soft-colliding with
    *two* vague primaries absorbed the union of their state lists and confirmed
    both states.  A winner absorbing more than one full-fold loser now demotes
    every fold to narrow.
16. **`_window_elapsed` matched a different promo's start date** in a multi-promo
    blob, stamping a live offer expired.  The start must now share a sentence with
    the end date.
17. **Chrome-terms class closed at its last member**: `</script >` (legal
    whitespace) defeated the paired strip, and deepen's *unmarked* fallback still
    stored raw `@font-face` CSS / inline JS as terms via a bare tag strip — the
    exact class fixed one function away.  The closer accepts whitespace and every
    whole-page fallback now goes through script-stripped `visible_text`.

Batch four, after round three (runs #19–#22): **zero rows carry CSS/JS residue**
(the batch-three cloudbet `@font-face` and tl\_ inline-JS terms now store visible
text).  Confirmed counts read IL 4 / PA 5 / NJ 5 / DC 4 of **54** — one lower than
batch three because DraftKings withdrew its FedEx St. Jude golf boost from its own
catalog between batches (5 DK offers served, not 6); every offer present in both
batches keeps its verdict.  Suite after round three: 4,965 passed, 1 skipped.

**Round four attacked round three's fixes and found two highs plus three smaller,
fixed the same evening:**

18. **Polarity inversion through the semicolon split**: "available in NJ; OR
    customers are excluded" read Oregon as *eligible* — the round-3 person-word
    allowance fed a clause carrying its own exclusion verb into the surrounding
    context's polarity (and "NY customers are excluded" inverted even without the
    allowance, all the way back to round 2).  Exclusion clauses now parse at the
    clause level (`_SUFFIX_EXCLUDE`, verb-anchored, so even English-colliding
    codes are safe), verb-carrying fragments never join a context list, and the
    person-word allowance is gone.
19. **"9:00 a.m. ET" defeated the same-sentence window rule** — the a.m./p.m.
    dots read as sentence ends, so the commonest real promo-window sentence rolled
    its elapsed end date into next year and planned a dead promo.  Abbreviation
    dots are no longer boundaries, and the start may sit on either side of the end
    ("ends August 15, having started August 13").
20. **Person vocabulary widened** (bettors, patrons, persons, individuals) — "OR
    bettors are also excluded" had silently dropped Oregon.
21. **$1,000,000 printed as `$1e+06`** in a stored summary (cloudbet loyalty,
    runs #19–#22) — money formatting now groups digits at every enrich summary
    site.
22. **Commented-out markup's text survived as "visible"** — `<!-- <div>` reads as
    one tag under a bare strip, so a comment's stale copy (old state lists, old
    end dates) could become terms, and betmgm's index rows stored `--> --> -->`
    artifacts.  Comments (and anything after an unclosed one) are scrubbed, and
    the html_catalog detail fallback now uses the same `visible_text` path.

Batch five, after round four (runs #23–#26): counts and verdicts unchanged from
batch four; the betmgm `-->` artifacts and the `$1e+06` summary are gone from
stored rows.  Suite after round four: 4,969 passed, 1 skipped.

**Round five attacked round four's fixes** (both reviewer agents stalled mid-run —
their confirmed traces were reproduced first-hand and their unfinished mutation
sweep completed the same way; all five mutants now killed).  Fixed:

23. **The suffix-exclusion regex captured only the code adjacent to the noun**:
    "NY, AZ and OR customers are excluded" excluded OR alone.  The group now
    captures the whole code list.
24. **The fragment filter had no killing test** (deleting it broke nothing) — a
    noun-less "; NY is excluded." fragment would have leaked NY into the
    *eligible* list.  Pinned.
25. **Single-dot abbreviations ("approx.") still read as sentence ends**,
    resurrecting elapsed windows; and the a.m./p.m. rule turned out to be
    redundant with the multi-dot rule (its deletion survived every test) — the
    redundant sub is gone, the multi-dot rule is pinned, and common single-dot
    abbreviations are cleaned.
26. **A `"<!--"` inside script code truncated the whole page** — the round-4
    comment scrub ran before the script scrub, so a JS string wiped every byte of
    visible text (measured: a real page shape returned "").  Scripts now strip
    first, matching browser semantics, and content after a *closed* comment is
    pinned to survive.
27. **Round four's item 22 called a helper the module never imported**: the
    html_catalog detail fallback invoked `visible_text` without importing it,
    and no test exercised that branch — every test's detail body carried a
    Terms heading — so four acceptance batches ran over a fallback that would
    have raised `NameError` on the first live detail page without one.  `ruff`
    (F821) caught it in the post-round-five ladder.  Import fixed, and the
    branch has a killing test now: a heading-less detail page must store
    script-stripped visible text, not chrome.

Also accepted, recorded (and retired in round eight): two promos joined by a
conjunction in one sentence ("Promo A started August 1 and Promo B ends
January 5") read as one elapsed window and the live promo stamped expired —
item 52's coherence guard now reads it as the correct next January, and the
pin asserts the new behavior.

Batch six, after round five (runs #27–#30): **zero verdict flips** — every offer
present in both batches keeps its `state_confirmed`, measured pairwise in all
four states (#23↔#27 through #26↔#30).  The totals still moved, because the
catalog did: DraftKings' own payload shrank 92,844 → 57,185 bytes (envelope
`byte_size`; the first draft quoted the envelope *file* sizes), withdrawing
its two same-day boosts (WNBA Parlay Boost and MLB SGP(x) Boost — both
IL-confirming), so the batch reads 52 offers per state, confirmed IL 2 / PA 3 /
NJ 3 / DC 2, the IL run's `ends_at` non-null count reads 5 (three remaining
DraftKings API-native dates + cloudbet + tl\_betmgm), and the signal predicate
reads 14.  The five round-five mutants were re-run against the final tree and
every one dies to a dedicated test.

The post-round-five ladder was **not** clean, and the first draft of this
section said it was: `ruff` flagged item 27's F821, and the full suite showed
one failure — 1 failed, 4,972 passed, 1 skipped — whose name the ladder's
tail-only log did not keep and which did not reproduce on the fixed tree the
same night (no test outside `tests/test_promos.py` touches the unimported
branch, so the import cannot explain it; it reads as flake, name lost).  After
the import fix and its pin: **4,974 passed, 1 skipped**, `ruff` clean.

**Round six ran two reviewers with split beats — one attacking the round-five
fixes, one auditing standing weaknesses (untested branches, the pipeline seam,
this document's own claims against the database) — and found twelve, sharing
one flagship.**  Every reproduction was confirmed first-hand before fixing:

28. **Name-form polarity inversion — the flagship, found independently by
    both reviewers.**  Five rounds hardened "; XX residents are excluded" for
    two-letter codes; "Offer available in New Jersey; New York residents are
    also excluded" walked the spelled-out name straight through
    `regions_from_text` into the *eligible* list — `offer_confirmed_for_state`
    returned True for the state the copy excludes, the one direction the
    predicate exists to prevent.  A stand-alone "New York residents are
    excluded." sentence was silently *lost* (no context grab contains it).
29. **The suffix-exclusion regex was exponential.**  Uppercase "OR" parsed as
    both code and conjunction under a nested quantifier: 20 codes ≈ 1 s,
    24 ≈ 18 s, ×4 per +2 codes — one all-caps legal blob joining states with
    "OR" would hang the collector inside `parse_eligibility`.
30. **An Oxford comma restarted the code chain**: "NY, AZ, and OR residents
    are excluded" excluded OR alone (round five's item 23 pin tested only the
    non-Oxford form).
    Items 28–30 fall to one rework: the single regex is gone; clauses split
    first (`[.;!?\n]`), a linear verb pattern finds the exclusion, and the
    region list is read *backward* token by token — codes, single- and
    multi-word names, District of Columbia, commas, "&", and conjunctions.
    The pass scans every clause of the text, so stand-alone sentences land
    too, and it still runs before the eligible pass, whose ineligible-wins
    filter is what keeps the name out of the eligible list.  Uppercase "OR"
    now *always* reads as Oregon in these clauses — over-excluding, which
    fails closed — and a 30-code all-caps "OR"-joined list parses in
    milliseconds (pinned with a timing bound).
31. **`/terms-and-conditions` passed the house-rules deny list** — the
    commonest sitewide house-rules URL of all.  End-to-end: a vague
    empty-terms offer + a footer link stored another document's state list as
    the offer's own (`eligible=['NJ'], ineligible=['NV']` from the sitewide
    page).  Item 12's harm through an href spelling the fix missed; the
    path-ending shape is now denied, sacrificing any genuine promo page under
    that name — the offer keeps its parsed terms, which fails safe.
32. **A decimal number between the dates broke the same-sentence window
    rule**: "started August 13 with 1.5x profit boosts and ends August 15"
    seen on the 16th rolled to 2027 — decimal odds, boost multipliers, and
    cent amounts are ubiquitous in exactly these sentences.  Digit-dot-digit
    is no longer a sentence end.
33. **A year-straddling elapsed window rolled forward**: "started December 20
    and ends January 5" seen in August read the start as *next* December
    (anchor-year construction) and planned the dead promo for a year.  The
    past-tense verb ("started"/"began") now pins a future-dated start to last
    year; present-tense "starts December 20" still means the upcoming window
    and rolls forward, pinned both ways.
34. **An impossible first end date hid a real one**: "Offer ends June 31.
    Offer ends July 1, 2026." returned None — the `ValueError` aborted the
    scan instead of trying the next stated date.
35. **A multi-card index's first T&C link was stamped on the page-level
    offer**: deepen then filled a vague page-level welcome's empty terms from
    *another card's* conditions and confirmed that card's states
    (reproduced end-to-end: `state_confirmed=1` on borrowed copy).  Only a
    page with exactly one terms link stamps it now.
36. **TheLines spans with no recognizable money line re-merged**: the
    whole-section fallback fired when every span failed to parse, carrying
    both promos' state lists in one description — the exact merge the ordinal
    split repaired, one branch over.  An unparseable span now becomes its
    *own* last-resort offer, keeping its own list.
37. **A `<script` token inside a closed comment truncated the page** — the
    round-five reorder's mirror hole: the unclosed-script backstop fired on
    a commented-out script include and silently dropped every byte after the
    comment, eligibility lines included.  Each ordering of two blanket
    passes has a hole, so the scrub now consumes constructs left to right by
    first opener, matching browser tokenization; all four prior scrub pins
    hold.
38. **`usage_guidance` stored `$1e+06`** — the round-four accepted-remainder
    text called the `:g` sites display-only, but `build_usage_guidance` runs
    before the store and `usage_guidance` is a column; runs #19–#22 carry the
    proof.  Four sites (the reviewer counted three; counting first found
    four) now group digits.  The stale accepted-remainder sentence is
    corrected below.
39. **"Accept Terms & Conditions" on a consent button counted as a Terms
    heading**, marking everything after a cookie banner as a terms slice.
    Consent wording (accept/agree/consent) no longer anchors; a genuine
    heading after the banner still does.

Batch seven, after round six (runs #31–#34): 52 offers per state, confirmed
IL 2 / PA 3 / NJ 3 / DC 2, **zero verdict flips** against batch six in all
four states (measured pairwise, #27↔#31 through #30↔#34).  The IL run reads
`ends_at` non-null 5, signal predicate 14, 15 deepened rows — all stable —
and zero rows carry `e+06` in any stored column.  No current catalog copy
uses the name-form suffix-exclusion shape, so item 28's live impact waits on
copy that does; the fix is pinned offline in five shapes.  Suite after round
six: **4,986 passed, 1 skipped**; `ruff`, `compileall`, and the doc check
clean.

One reviewer finding was **investigated and rejected**: a `terms_url` equal to
the offer's own URL takes the terms-only path (marked-slice-or-nothing) rather
than the full detail merge, which the reviewer read as starvation.  The
existing pin proves the starvation is protection: the detail merge's
vague-welcome allowance would let an unrelated chrome page's "$10" fill empty
terms.  Kept, now documented at the site.  The same reviewer also verified
this document's measured claims against the database — every count in the
six-batch history reproduced except the DraftKings byte sizes, corrected above
to envelope `byte_size` (92,844 → 57,185) — and ran the pipeline seam
differentially (composed stages vs `collect_promos_once`: byte-identical
rows).  All eleven round-six mutation candidates die to dedicated pins
(the first sweep's name-walk mutant survived until the pin gained a
single-word-name case — the mutant was weak *and* the pin was).

**Round seven ran the same two-beat pair against the round-six fixes; its ten
raw findings land as eight numbered items — the polarity-inversion class was
still alive in three orderings.**  Every reproduction confirmed first-hand
before fixing:

40. **A second person word swallowed the anchor**: "open to players in
    Michigan and Ohio, and Nevada residents are excluded" anchored the round-6
    verb pattern on "players", lost the exclusion in the gap, and confirmed
    NV — the flagship inversion, one clause shape over.  The pass now anchors
    on the person word *nearest* the verb, and when the same clause carries an
    eligible verb the backward walk stops at the first comma after a region,
    so the glued eligible list is forfeited (masked, never confirmed) rather
    than either polarity swallowing it.
41. **Exclusion verbs the ineligible passes did not recognize inverted
    through the eligible grab**: "available in New Jersey; void in New
    York" confirmed NY — "void in", top-frequency legal phrasing, appeared in
    no pattern; likewise "excludes", "prohibited", "banned".  Two fixes:
    the vocabulary is widened, and — the structural one — **every clause
    carrying a geographic-exclusion verb is masked (offset-preserving) from
    every eligible-side pass**, the `_FRAGMENT_EXCLUSION` principle
    generalized from `;`-fragments to whole clauses, names and codes alike.
    The ineligible-wins filter stays as defense in depth; the mask holds even
    for verbs nothing enumerates (pinned with "wagers from New York are not
    eligible", which no other pass attributes).
42. **Person-first orderings were invisible**: "Customers physically located
    in NJ, NY, PA, WV are excluded" (all four confirmed eligible!),
    "Residents of NY, NV, PA are not eligible", "Any resident of New York may
    not participate" — the round-6 walk only read *backward* from the noun.
    The pass now reads the region list on whichever side of the noun it sits,
    and singular person words count.
43. **Conduct restrictions read as state exclusions**: "residents may not
    participate more than once" excluded the *eligible* list (a frequency
    cap), "New Jersey residents may not bet on in-state college teams"
    poisoned NJ for the whole offer (a market restriction), and the fragment
    filter's bare "may not" dropped an eligible chunk over "users may not
    redeem more than once".  A may-not verb now counts as geography only
    with a non-substantive tail; the same discrimination guards the mask and
    the fragment filter.
44. **The year-straddle fix had a mirror**: "started December 20 and ends
    December 30" seen in January rolled the dead window to *next* December —
    the elapsed check only ran when the anchor-year end date was in the past.
    The mirror branch shifts a future-looking end to last year when the
    past-tense start proves last year, and only when the shifted end still
    follows the start — "started December 20 and ends March 1" keeps its
    genuinely live straddling window.  Pinned both ways.
45. **Live mis-parse: "CA-ONT" stored as California** — DraftKings' terms say
    "physically located in CA-ONT."; the licence-form pattern knew only
    two-letter provinces, so runs #27–#34 carry `CA` (and no `ON`) in a
    stored eligible list.  Three-letter forms now normalize (ONT→ON, QUE→QC,
    ALB→AB, SAS→SK, MAN→MB); the row parses to Ontario on the next collect.
46. **The round-6 scrubber was quadratic and truncated on HTML5 abrupt-close
    comments**: re-searching both construct patterns every iteration made a
    300KB SSR page with 20k hydration comments take ~4 s (1MB of scripts
    ~11 s) — each pattern's next hit is now cached, 40k markers scrub in
    milliseconds, pinned with a generous timing bound; and `<!-->` /
    `<!--->`, which browsers treat as *complete* empty comments, read as
    unclosed and silently dropped everything after them — the closer search
    no longer skips the opener's own dashes.
47. **The house-rules deny list missed the next four spellings and ran on the
    raw href**: `/general-terms-and-conditions`, `/legal` (no trailing
    slash), `/terms.html`, `/terms_and_conditions` all passed, and a
    slash-less relative `href="terms"` resolved to the exact denied URL after
    the check.  The deny list now covers the slug family and tests the
    *resolved* URL.

Batch eight, after round seven (runs #35–#38): 52 offers per state, confirmed
IL 2 / PA 3 / NJ 3 / DC 2, **zero verdict flips** against batch seven in all
four states (pairwise #31↔#35 through #34↔#38); IL `ends_at` non-null still
5, zero `e+06` rows.  Item 45's live proof landed: the DraftKings row whose
terms read "located in CA-ONT" now stores `ON` in its eligible list where
runs #27–#34 stored the false `CA`.  Suite after round seven: **4,996
passed, 1 skipped**; `ruff`, `compileall`, and the doc check clean.

All ten round-seven mutation candidates die to dedicated pins — two sweep
lessons en route: the mask mutant survived until the pin gained the
no-other-pass shape (41's "wagers from…"), and the quadratic mutant's first
sweep run was an artifact — rerun in isolation it fails the timing bound at
6.3 s.  Round seven's fresh-eyes reviewer also re-verified every batch-seven
claim in this document against the database (all reproduce), the pipeline
seam differentially once more, the hardrock fixtures byte-for-byte against
the probe log, and that the betrivers research profile cannot reach the
network outside the sanctioned recon path.  Its process note stands: the two
hardrock fixtures are untracked until the closing commit, and `_first_contact`
skips (not fails) on a missing fixture — the commit must `git add` them.

**Round eight split its beats between the round-seven fixes and a stored-data
audit — hand-checking batch-eight rows against their own copy — and found
fourteen (eight attacker, six auditor), landing as twelve numbered items.**
The audit surfaced defects no code-path attack had seen, including one wrong
stored list standing since run #2:

48. **The may-not tail whitelist failed open**: "Customers located in New
    York may not participate in this promotion **due to state regulations**"
    fell off the whitelist of acceptable exclusion tails, read as conduct,
    and NY stayed confirmable.  The polarity is now inverted — a may-not
    verb excludes by default and only a tail naming a conduct object
    (frequency, market, channel) opts out — and tails read to the clause
    end, so "may not enter, claim, or redeem … more than once" finds its
    conduct object past the verb-list commas.
49. **The round-seven comma-stop under-excluded** — "valid for new customers
    only, NY, NV, and PA residents are excluded" truncated to PA alone, and
    under-exclusion fails *open* against adapter-stamped states (the parse's
    veto is what keeps a stamped state unconfirmed).  The comma-stop is
    gone; the walk may now swallow a ", and"-glued eligible list into
    ineligible, which over-excludes — a fail-closed veto, documented at the
    walk.
50. **The negated-eligible verb family escaped everything**: "it is not open
    to residents of New York", "restricted in New York", "participation from
    New York is not permitted" all confirmed NY.  Two structural changes
    close the class for *any* vocabulary: the eligible grab is **narrowed**
    across ";" to pure region-list continuations only (legal copy's "in NJ;
    NY; PA" still rides; any fragment with a non-region word starts a new
    clause), and a grab whose own narrowed chunk carries exclusion signal is
    **forfeited whole** — partial salvage of a mixed-polarity chunk is where
    every inversion of rounds six through eight lived.  The recognized
    families also widened (restricted/barred/not-open/not-permitted/
    not-allowed, with required prepositions).
51. **Widened "void" was a round-seven regression**: "All bets are void if
    the match is abandoned, offer available in NJ and PA" false-excluded PA
    and masked away NJ/PA's eligibility — market boilerplate, not
    geography.  The exclusion-context verbs now split into two families:
    void/prohibited/banned/restricted/barred and the negated-eligible forms
    require their preposition ("void **in** NY"); and the whole-clause mask
    no longer feeds the grab path at all — chunk narrowing and forfeit
    replace it there, so a market exclusion sharing a comma-spliced clause
    with an eligible list ("Parlays and teasers are excluded, offer
    available in NJ and PA") costs nothing.  The mask still guards the
    bare-code-list fallback, which has no chunk to inspect.
52. **The elapsed-window rule had a third seam**: "started December 20 and
    ends January 5" observed December 28 — mid-run — stamped the *live*
    window expired-last-January.  Elapsed evidence must now cohere: a
    window's start cannot follow its own end.  The same guard turned the
    accepted conjunction case ("Promo A started August 1 and Promo B ends
    January 5") from a documented fail-closed expired-stamp into the correct
    next-January reading — that accepted-remainder item is retired.
53. **Stored since run #2: bare code lists ending ", and XX" lose their last
    state.**  `_CODE_LIST`'s conjunction tail was case-sensitive while both
    call sites feed uppercased text — dead code, so tl\_fanatics' "MI, NJ,
    PA, and WV" stored without WV in every batch.  If a licensed state is
    the last item of such a list, its confirmation silently vanished.
54. **"Up to 100% Parlay Boosts" stored a fabricated $100** — `_MONEY`'s
    optional `$` let a percentage read as dollars (tl\_bet365, all batches);
    the naive lookahead fix backtracked into "$10", so the guard forbids a
    following digit too.
55. **"25% boosted rakeback" classified as a profit boost** — cloudbet's
    welcome package stored reward "boost", a "25% profit boost" summary,
    and boost-branch planner routing from post-welcome casino rakeback
    copy; the boost patterns now require a word boundary.
56. **"Bet $5 and get $300" — the commonest welcome phrasing — extracted
    nothing**: the conjunction was missing from `_BET_GET`'s gap, so the
    flagship DraftKings welcome stored `bonus_amount=None` and a junk
    summary.
57. **The fallback money-slice summary still cut the *trailing* word** —
    round one anchored only the start; batch-eight rows read "…the total of
    your fi", "…one of the fast-growi".  The slice now runs to a word
    boundary.
58. **Eligible-side person-noun lists dropped the noun-adjacent code**:
    "Available to residents of KS and MO" kept only MO — round seven taught
    the exclusion pass two-sided person-noun lists and the eligible side
    never got the same; a person-noun-prefixed list part now reads its
    trailing regions.
59. **Two more house-rules slugs** (`/termsandconditions`, `/terms.php`)
    passed the deny list; the separators and extensions are now optional in
    the slug family, and the class is recorded as open-ended below.

Batch-eight stored-data audit, everything else: the auditor hand-checked 15+
rows across sources against their own copy and field-level-diffed runs
#31↔#35 through #34↔#38 — the only row that changed is draftkings/1074057,
delta exactly {−CA, +ON}, item 45's fix landing.  All thirteen round-eight
mutation candidates die to dedicated pins; three needed a second pass — the
forfeit and may-not-default mutants survived until the pin gained a shape
with no person word (where the forfeit is the *only* defense), and the
summary-tail mutant until the pin's terms actually crossed the 60-char cut.

Batch nine, after round eight (runs #39–#42): 53 offers per state — DraftKings
added a new "Grand Slam Payout" offer to its own catalog, which confirms in
all four states — so confirmed reads IL 3 / PA 4 / NJ 4 / DC 3 with **zero
verdict flips** among offers present in both batches (pairwise #35↔#39
through #38↔#42).  The round-eight fixes are visible in stored rows:
tl\_fanatics' eligible list now carries WV (item 53), the tl\_bet365 percent
boosts store no fabricated dollar amount (item 54), and the tl\_draftkings
flagship welcome stores `bonus_amount=300 / min_deposit=5` (item 56).  Suite
after round eight: **5,008 passed, 1 skipped**; `ruff`, `compileall`, and
the doc check clean.

**Round nine paired an attack on the round-eight fixes with a full audit of
batch nine's stored data — hand-checking every confirmed row against its own
copy, the planner's cards against the corrected amounts, and a field-level
cross-batch diff.  The state verdicts held everywhere; the reward/mechanics
layer did not.  Twelve findings, landing as eleven numbered items:**

60. **The confirmed IL flagship planned at ~5x its value from a reward read
    out of a negation** — the worst find of the audit: DraftKings' "NFL Fast
    Futures 30% Profit Boost", confirmed in all four states, stored
    `reward_type=bonus_bets` because its terms' only "Bonus Bets" mention is
    the sentence *excluding* them ("Bets placed with bonus rewards … are not
    valid"), and the reward-wins dispatch then priced a credit conversion
    (+67.9/100) where a boost lock (~+14/100) is the true model.  The reward
    is now read title-first, and a body sentence that negates rewards never
    votes.  The first-contact section's "63.7–67.9% measured conversion"
    line recorded this misprice as a success.
61. **A signup site-credit bonus planned as pure conversion while its own
    row stored a 5x rollover** — `bonus_like` kinds bypassed the rollover
    branch entirely (bovada, $500); a parsed wagering requirement now routes
    site credit to the rollover model whatever the kind.  The
    "site credit is always a rollover" pin had parametrized five kinds and
    omitted exactly the three it failed on.
62. **TheLines' review prose contaminated kind labels**: "profit-boost
    tokens and odds boosts routinely available" — TheLines' description of
    the *brand*, not the offer — stamped `odds_boost` on FanDuel's $250
    bonus-bet flagship (under-planned as a $250 cash boost hunt, worst
    +0.00) and `parlay_boost` on BetMGM's $10 straight-bet offer, whose
    plan card printed a false "the qualifying leg is a parlay" note.  Kind
    now reads the offer's own title only.
63. **Modal auxiliaries defeated the enumerated negations**: "New York
    residents **will not be** eligible" matched nothing — the exact
    vocabulary one auxiliary away — and the comma-spliced clause confirmed
    NY.  All negation families now tolerate will/shall/may/can/do + "be",
    and "do not qualify" joined them.
64. **The 200-char exclusion-chunk cap truncated long spelled-out lists**:
    a 26-state exclusion list ended inside "Massachusetts" and seven states
    escaped the veto — an adapter-stamped NJ stayed confirmable against
    copy that excludes it.  The cap is 600 now, pinned with the full list.
65. **"restricted TO New Jersey" fabricated an NJ veto** — a round-eight
    regression: the family split gave "restricted" the to-preposition,
    inverting an NJ-only-eligible sentence into ineligible.
    Restricted/barred now require in/from; "restricted to" stores nothing
    (unconfirmed, fail-closed).
66. **"excludes casino games, available in NJ and PA" vetoed both licensed
    states** — the no-preposition "excludes" family over-grabbed through
    the comma (and "except for parlays, offer available in New Jersey"
    had always done the same).  An exclusion grab now ends at the last
    list boundary before an eligible verb — which also half-heals the old
    both-directions semicolon swallow: "Not available in NV; eligible in
    NY and NJ" no longer false-excludes NY/NJ (they stay unconfirmed).
67. **Combinability and employment boilerplate fabricated vetoes** — item
    48's default-exclude read "may not claim this offer in conjunction with
    any other promotion" and "may not participate if employed by the
    operator" as geographic, wiping the preceding eligible lists; and bare
    "cannot" forfeited "cannot be combined with any other offer" copy.
    The conduct markers now cover conjunction/combin-/stack-/employ- (and
    "per" requires a frequency object, so "per order of the gaming
    commission" stays a real exclusion).
68. **"legal residents of KS and MO" dropped KS** — item 58's person-noun
    fix anchored at the part start; up to two leading modifier words are
    tolerated now.
69. **Percentages and token counts fabricated bonus dollars** through three
    more holes: "Bet $5 and get 30% profit boost" → $30 (reachable through
    item 56's own "and"), "receive 3 bonus bets" → $3, "up to 27.5%" →
    $27 by regex backtracking, "100 percent" → $100; and "27.5% profit
    boost" summarized as "5% profit boost".  Reward amounts now require a
    literal dollar sign, the up-to guard rejects decimals and spelled
    "percent", and the percent extractors are left-guarded.
70. **A plan note called a computed number "stated"** — "priced with the
    stated N% boost" printed on cards whose own caveat says the offer does
    not state its boost percentage; the word is gone.

Batch ten, after round nine (runs #43–#46): 53 offers per state, confirmed
IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict flips** (pairwise #39↔#43 through
#42↔#46).  The reward-layer fixes are visible live: the DraftKings flagship
stores `reward_type=boost` and its plan card reads **`boost_locked`** where
runs #1–#42 priced a +67.9% credit conversion (item 60); bovada's $500
signup stores its 5x and plans **`rollover_grind`** (item 61); the
tl\_betmgm $10-straight offer reads `bonus_bet` with no parlay note (item
62).  One residual, accepted and recorded below: tl\_fanduel's flagship
still stores reward "boost" — its title carries no reward word, so the
body scan reads TheLines' review prose; the consequence is an under-planned
boost card (worst +0.00), never a fabricated value.  Suite after round
nine: **5,020 passed, 1 skipped**; `ruff`, `compileall`, doc check clean.

All twelve round-nine mutation candidates die to dedicated pins (the reward-
negation mutant survived one pass — masked by the very title-first fix it
shipped beside — until the pin gained a silent-title case).  The auditor also
verified: every confirmed batch-nine row hand-checks against its own copy in
all four states; the Grand Slam row matches its raw envelope field-for-field;
every cross-batch field change traces to a numbered fix or the DK catalog
addition; and the wrong-verdict sweep over all 212 batch-nine rows found
zero states named-but-unconfirmed beyond TheLines nav chrome.

**Round ten found four — two of them round-nine's own fixes failing in new
directions, one stored live by round nine itself:**

71. **The polarity-flip truncation inverted "Excludes players located in
    Michigan"** — item 66's verb-position rule cut the exclusion grab at any
    eligible-verb *appearance*, and "located" lives inside exclusion
    phrasing: the emptied grab left nothing in ineligible and the eligible
    pass **confirmed MI**.  "not available to persons physically located in
    New Jersey" — standard US promo legalese — lost its veto the same way.
    A flip now requires a clause-initial eligible verb (boundary or
    conjunction followed only by glue words), "located" left the flip set,
    and "…, or customers located in Nevada" reads as the exclusion's own
    list again.  Pinned in both directions, with the "void in New York and
    only valid in New Jersey" control that a naive keep-whole rule would
    have re-broken.
72. **"up to 3 bonus bets" fabricated $3** — item 69's dollar-sign invariant
    held in `_BET_GET`/`_GET_IN_BONUS` but not `_UP_TO`, whose fallback
    fires exactly on dollar-less counts.  `_UP_TO` requires the literal
    dollar sign now.  No committed capture triggers the shape; the fix
    closes the stated invariant.
73. **Item 60's own negation filter fabricated a reward across sentence
    boundaries — stored live in batch ten**: the space-join of surviving
    sentences turned "…claim $500 Cash. Bonus Spins…" into a "cash bonus"
    match.  The leovegas\_on loyalty row in runs #43–#46 stored
    `reward_type=cash`, `is_specific=1`, a junk mid-sentence summary — and
    the `is_specific` cascade cost the offer its deepen fetch (batch nine
    wrote its deepen envelope; batch ten wrote none).  The auditor caught it
    as the one cross-batch field change tracing to no numbered fix, with
    the raw envelopes differing only by a request UUID.  Sentences now join
    with ". ", so no pattern can span a boundary; corrects on the next
    collect.
74. **The planner never read `product`** — bovada's *poker* welcome printed
    a 74% sportsbook "conversion" of poker-room deposit credit onto soccer
    moneylines.  A non-sportsbook product now plans `text_only` with its
    reason named (`non_sportsbook_product`) and a caveat saying why.

Batch eleven, after round ten (runs #47–#50): 53 offers per state, confirmed
IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict flips** (pairwise #43↔#47 through
#46↔#50).  Item 73's correction landed live: the leovegas\_on loyalty row is
back to an empty reward with `is_specific=0`, and its deepen fetch — lost to
the cascade in batch ten — ran again (`deepened_from` restored in metadata).
Suite after round ten: **5,023 passed, 1 skipped**; statics clean.

All five round-ten mutation candidates die to dedicated pins.  The closing
auditor also verified batch ten row-by-row (every confirmed row against its
own copy; every cross-batch change traced), re-derived the planner's
arithmetic by hand (the DK `boost_locked` stakes solve exactly; bovada's
`rollover_grind ev=+500.00` is genuine — the cheapest clearing market has a
positive floor, so the stated face-value assumption holds conservatively),
and swept the full diff for unrelated edits, leftovers, and secrets: none.
A tautological assertion in the round-9 modal pin (`… or True`) was removed.

**Round eleven split: the convergence auditor came back clean — batch eleven
fully traced (item 73's correction the only stored change), every batch-
history claim 1–11 reproducing from the database, the planner's arithmetic
re-derived to the cent, the diff free of unrelated edits — while the
attacker found four in the round-ten code:**

75. **"available in all states except New York" confirmed NY** — three
    defenses failing together: the greedy 600-char exclusion grab consumed
    the "except" trigger whole, the flip cut discarded the tail
    un-rescanned, and "except" — a recognized family since round one — was
    in no eligible-side vocabulary.  The exclusion scan now restarts inside
    each chunk so nested triggers match in their own right, and an eligible
    chunk stops at an exception trigger — "available in NJ, PA and WV
    except New York" keeps the head eligible AND vetoes NY.  The bare
    "valid in New Jersey but not in New York" sibling keeps NJ and leaves
    NY unconfirmed (fail-closed; noted in the remainder).
76. **The flip glue missed common auxiliaries and tight commas** — "but is
    still available in New Jersey" was not read as a flip, so the eligible
    clause was swallowed into the veto, destroying an adapter-stamped NJ's
    confirmation on realistic copy — a round-ten regression over round
    nine's merely-unconfirmed behavior.  The glue now covers
    still/will/be/was/currently/remains/stays and zero-space commas.
77. **An "etc." severed reward names from their own negation** — the reward
    scan's sentence split had none of the abbreviation cleaning the window
    rule gained in round three ("the exact class fixed one function away",
    again): DraftKings' live sentence shape fabricated `bonus_bets` on a
    silent-title row, the exact re-opening of item 60.  Abbreviation dots
    are cleaned before the split now.
78. **The product gate's vocabulary disagreed with the adapters'** —
    DraftKings deliberately admits "Sports"/"Predict" cards to the sports
    board, and the gate would have text_only'd a genuine sports offer.  The
    sports family is exempt; poker/casino still gate (verified live: run
    #47 gates exactly bovada-poker and two cloudbet-casino rows).

All six round-eleven mutation candidates die to dedicated pins — including
the previously unpinned glue restriction (item 71's load-bearing rule was
deletable unnoticed until this round's pin).

Batch twelve, after round eleven (runs #51–#54): 53 offers per state,
confirmed IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict flips and zero
core-field changes** against batch eleven (pairwise #47↔#51 through
#50↔#54) — the round-eleven shapes have no trigger in current live copy,
so full stability is the expected acceptance.  Suite after round eleven:
**5,026 passed, 1 skipped**; statics and doc check clean.

**Round twelve: the convergence auditor came back clean on the subsystem —
batch twelve fully explained down to raw-envelope chrome (the 12 changed
hashes reduce to `lastUpdatedTime`s, a Next.js redeploy, CSS, and jackpot
tickers; visible text byte-identical), the planner's product gate firing on
exactly the three non-sportsbook rows, every doc claim 1–12 reproducing —
while the attacker found two latents in round eleven's own additions:**

79. **"except" in the forfeit vocabulary ate 1–2-code vetoes** — the
    `;`-fragment filter dropped "NJ except as otherwise stated in these
    terms" whole, codes included, and a short code list has no name-form or
    bare-list fallback: an adapter-stamped NJ stayed confirmed against
    "Not available in NJ".  A 257-shape differential corpus proved the word
    prevents nothing on its intended path (the chunk stop cuts first), so
    it is gone from `_EXCLUSION_WORD` — and pinned, since reverting it had
    survived all 547 tests.
80. **The abbreviation cleaning glued "cash vs. bonus" into a fabricated
    "cash bonus"** — item 77's fix replaced the abbreviation *token* with a
    space, creating adjacency that re-opened item 73's exact consequence
    signature (fabricated reward, `is_specific=1`, junk summary, lost
    deepen).  Only the dots are stripped now; item 77's live DK shape stays
    suppressed.

Both round-twelve mutation candidates die to dedicated pins.  The auditor
also flagged, for the operator rather than the loop: **a concurrent
workstream is editing `src/report.py` / `src/report_assets.py` /
`src/report_copy.py`** (a dashboard sportsbook-picker, mid-flight,
untested) — the closing commit is path-scoped to the promotions files so
that work is neither swept in nor disturbed.  Suite after round twelve:
**5,026 passed, 2 failed, 1 skipped** — both failures are dashboard/docs
tests broken by that concurrent workstream's in-flight edits (the hotspot
table's `report_assets.py` line count, and an empty-state string its
picker rewrote), proven by rerunning both with the three report files at
HEAD and the full promotions diff applied: both pass.  The promotions
scope is green.

Batch thirteen, after round twelve (runs #55–#58): 54 offers per state (one
new catalog arrival), confirmed IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict
flips and zero core-field changes** among offers present in both batches
(pairwise #51↔#55 through #54↔#58).

**Round thirteen found two more, one each side:**

81. **A code list severed from its exclusion trigger confirmed** — "except:
    NJ, NY, PA" bypassed the veto (the trigger regex demanded whitespace,
    round twelve's removal of "except" from the forfeit vocabulary opened
    the bare-list fallback), and its pre-existing sibling "Excluded
    states:\nNJ, NY, PA" inverted through the clause-local mask the same
    way — the newline left the list an innocent bare list.  One fix for the
    class: exclusion triggers tolerate adjacent punctuation ("except:" now
    vetoes), "excepting" joined the trigger and stop vocabularies,
    "but not"/"other than" joined the forfeit vocabulary, and a carrier
    clause ending in ":" masks the *next* clause from the bare-list
    fallback — the colon shapes veto, the newline shapes fall closed to
    unconfirmed.  Item 79's own pin held throughout.
82. **Entity-unescape before tag-strip leaked attribute values into stored
    terms** — an `&gt;` inside a quoted attribute became a literal ">" that
    ended the tag early, and the attribute's tail (Tailwind class soup,
    data attributes) survived as "visible" text: **92 rows across runs
    #7–#58**, found by the round's new-arrival envelope check (cloudbet's
    endorphina row stored 52% junk terms).  `strip_tags` now strips first
    and unescapes second, like a browser.  This also corrects the
    batch-four sentence below: "zero rows carry CSS/JS residue" was true of
    the *scrub* class it measured, but runs #19–#22 carried this
    entity-leak variant all along — the fifth mechanism of the
    chrome-as-terms class (items 17/22/26/37/46), none of whose pins
    covered it.  All 92 rows stored empty region lists and no fabricated
    values; corrects on the next collect.

Both round-thirteen mutation candidates die to dedicated pins, and the
misplaced round-ten auditor paragraph was returned to its round (an
editorial fix flagged by the closing auditor, not numbered).  Suite after
round thirteen: **5,041 passed, 3 failed, 1 skipped** — all three failures
are dashboard tests broken by the concurrent report workstream (which has
grown to eight files including its own test edits); with every
non-promotions file at HEAD and the full promotions diff applied, all
three pass.  The promotions scope is green.

Batch fourteen, after round thirteen (runs #59–#62): 54 offers per state,
confirmed IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict flips**, and item 82's
fix proven live: the entity-leak signature (`span]:line-clamp`) reads **12
rows in batch thirteen → 0 in batch fourteen** — every affected cloudbet
row now stores clean visible text.  The round-fourteen auditor traced all
12 changed rows to item 82 with the raw envelopes as arbiter (both batches'
envelopes still carry the leak in markup; the fixed extractor produces
byte-identical output from both), re-verified the 92-row historical count
and its harmlessness claim, reproduced the batch history 1–14 in full, and
proved the commit manifest sufficient in both directions — the promotions
scope passes the entire suite with every concurrent-workstream file at
HEAD.

**Round fourteen: the auditor was clean; the attacker found two in round
thirteen's own geo fix:**

83. **A wrapped exclusion list confirmed its continuation lines** — the
    colon-carry reached exactly one clause, and worse, a plain "not
    available in NJ, NY, PA,\nWV, MI, IL." sentence wrapped mid-list stored
    *"Not available in: NJ, NY, PA; Eligible in: WV, MI, IL"* — half a
    single exclusion sentence vetoed, the other half confirmed.  The
    exclusion chunk now extends across newline-wrapped pure-region-list
    continuations (the whole list vetoes), and the mask carry chains while
    clauses remain pure lists (colon shapes stay fail-closed unconfirmed).
84. **Round thirteen's "but not"/"other than" forfeit words re-opened item
    79 verbatim** — the `;`-fragment filter dropped "NY other than as
    required by law" whole, veto included.  They are `_EXCLUDE_CTX`
    *triggers* now, not forfeit words: the codes land in `ineligible`
    before the fragment filter runs, "but not: NJ, NY, PA" upgrades from
    masked to vetoed, and "Eligible states include NJ, NY, PA but not NV"
    reads ideally — head confirmed, tail vetoed.

All three round-fourteen mutation candidates die to dedicated pins.  Suite
after round fourteen: **5,042 passed, 1 skipped** on the promotions scope
(the same three concurrent-workstream dashboard failures, attributed as
above).  Batch fifteen, after round fourteen (runs #63–#66): 54 offers per
state, confirmed IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict flips and zero
core-field changes** (pairwise #59↔#63 through #62↔#66) — the round-15
auditor measured stronger: zero changes of any kind across all 23
non-provenance columns, and re-arbitrated item 82's 92-row history against
era envelopes (the 20 rows with $2,500 carry it from their own genuine
copy).

**Round fifteen: the delta auditor was clean; the attacker found one more
turn of item 83's class:**

85. **A semicolon at the wrap point severed the list again** — "not
    available in NJ, NY;\nPA, WV, MI." half-vetoed and half-*confirmed*:
    ";" was missing from the wrap-extension's open-tail set, and on the
    mask side a clause can never even end in ";" (it is a clause boundary),
    so no tail check could see it.  The extension tuple gained ";", and the
    mask carry now also fires when the separator following a carrier is
    ";" — the US-group;Canada-group list ("NY, NV;\nON, QC, AB"), the
    formatting geo.py's own comments call routine, vetoes in full.

Accepted with round fifteen: an exclusion trigger's full-name scan reads a
team's city as its state — "Boost applies to all teams but not the New
York Knicks" vetoes NY (confirmation lost, nothing falsely confirmed).
The class predates round fourteen ("except the New York Knicks" behaved
identically through all rounds); every guard attempted ("the"-prefix
rejection, capitalized-next-word rejection) either loses "the state of
New York" vetoes fail-open or misreads multi-word state lists, so the
fail-closed cost is taken deliberately.

Batch sixteen, after round fifteen (runs #67–#70): 54 offers per state,
confirmed IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict flips and zero
core-field changes** (pairwise #63↔#67 through #66↔#70) — the round-16
verifier measured all 25 non-provenance columns byte-identical.  Suite
after round fifteen: **5,042 passed, 1 skipped** on the promotions scope.
Environmental note from the same audit: bet365's health row flipped
`empty_after_parse` → `captcha` in batch sixteen — the refusal recorded
honestly, zero offers either way; relevant to the operator's bet365-egress
watch, not to any stored claim.

**Round sixteen: the attacker's differential sweep found the wrap class
three mechanisms deeper, and the closing verifier found one unpinned
defense:**

86. **A newline that merely wraps a list severed every remaining
    mechanism** — verb-after-list heads confirmed ("Residents of NJ, NY,
    IL,\nand WV are excluded." confirmed the head three), a wordless glyph
    line between carrier and list broke both round-14 defenses (one
    trailing space on a blank line resurrected the half-and-half
    signature), dash introducers — which `_EXCLUDE_CTX` itself accepts —
    were missing from both open-tail sets, and the eligible side lost its
    wrapped tails (ten-state list stored as five).  Rounds 13–16 had fixed
    one severed mechanism per round; the structural cure is **wrap-join
    normalization before any parsing**: a newline preceded by an open-list
    token (comma, semicolon, colon, dash, ampersand, conjunction) or
    followed by a conjunction line joins into a space, blank and bullet
    filler consumed with it — so every downstream mechanism sees whole
    sentences.  All four severed classes now veto in full (or fall closed
    where no trigger exists), the eligible tail case keeps all ten states,
    and every prior wrap pin holds.
87. **Item 85's mask-carry half was unpinned** — its revert survived all
    552 promotions tests while "Excluded states are as follows;\nNJ, NY,
    PA." *confirmed* all three states: the carry is the only defense when
    the carrier matches the exclusion vocabulary but no exclusion-context
    trigger fires (bare past-participle "excluded" with no code list in
    its clause).  Round 15 was also the only round without a recorded
    mutation sweep — this is the mutant it would have caught.  Pinned with
    exactly that shape.

Both round-sixteen mutation candidates die to dedicated pins.  Suite after
round sixteen: **5,043 passed, 1 skipped** on the promotions scope.  Batch
seventeen, after round sixteen (runs #71–#74): 54 offers per state,
confirmed IL 3 / PA 4 / NJ 4 / DC 3, **zero verdict flips and zero
core-field changes** (pairwise #67↔#71 through #70↔#74).

**Round seventeen — the terminating clean round.**  Both reviewers came
back clean.  The end-state auditor: batch seventeen byte-identical outside
provenance across all 28 columns; both round-16 mutants killed; the batch
history 1–17 reproducing from the database in full; the commit manifest
proven *sufficient* (the suite is green in a tree of HEAD + the 15
promotions files + the two fixtures) and *complete* (the same run passes
with none of the concurrent workstream's edits present).  The attacker: 45
recognized-vocabulary × join-token combinations produce **zero
confirmations of an excluded state**; every behavioral delta the join
introduces maps the wrapped form exactly onto its flat sentence's
round-15 behavior; enrichment is byte-stable over three passes on
wrap-bearing offers; both round-16 pins are load-bearing and correctly
strong.  Recorded so no later round rediscovers them, all judged below the
material bar: Oregon at the exact wrap point of an unpunctuated line
before an eligible-verb line reads as a conjunction and loses its veto
(OR is not an operator state and no adapter stamps it); `_WRAP_JOIN`'s
second branch is quadratic on ≥100KB contiguous space runs that no
pipeline surface can produce (HTML collapses whitespace before the parse;
API terms cap at 4,000 chars); "&" never splits one- or two-code lists
(pre-existing, flat and wrapped identical); a masked-header list withholds
parse-derived confirmation but never vetoes an adapter *stamp* — the
accepted colon/newline wording covers parse confirmation only; and
hyphen-split vocabulary words ("un-\navailable") are invisible on every
real surface.  After seventeen rounds, eighty-seven numbered findings, and
seventeen live acceptance batches, the loop terminates.


Accepted and recorded, not fixed: the collector pipeline composition
(enrich→deepen→enrich→prefer→stamp) has no direct test — `deepen_by_source`'s cache
passthrough is pinned but the `collector.py` wiring line is not; a multi-promo index
page's first T&C link is attributed to the page-level offer; terms folded in by the
collision merge are not re-enriched afterward; a batch-shared deepen cache freezes a
transient fetch failure for the remaining states of that batch (retried next batch);
the exclusion grab reads through ";" in *both* directions, so "Not available in NV;
eligible in NY and NJ" also swallows the eligible clause — fail-closed, pre-existing,
kept; deepen envelopes are attributed to whichever source fetched the URL first; an
adjacent-sentence start date ("The promo started August 13. It ends August 15.")
does not suppress the roll-forward — the price of the multi-promo guard; the
suffix-exclusion person vocabulary is a finite list; a present-tense start
earlier in the year still reads as *this* year's ("starts January 2 and ends
January 5" seen in August stamps expired though live copy means next January) —
fail-closed, and the mirror rule would regress cloudbet's genuine present-tense
elapsed window; a `terms_url` equal to the offer's own URL keeps the terms-only
deepen path (marked-slice-or-nothing — the detail merge's vague-welcome
allowance would let chrome fill empty terms, and the pin that proves it stays);
and the *planner's* display strings still format money with `:g` (the stored
columns — summaries and `usage_guidance` — are fixed; a plan card for a ≥$1M
figure would still print scientific notation).  Round eight adds: the
house-rules slug family is open-ended — new sitewide-terms spellings will
keep appearing, with residual harm bounded to fill-only marked slices on
empty-terms offers; exclusion vocabulary outside the recognized families
(novel verbs with no person subject in a single mixed clause) can still ride
an eligible grab — the ";"-crossing route is structurally closed, the
same-clause route is vocabulary-bound by `_NEGATED_ELIGIBLE`; a TheLines
offer whose title names no reward may still take its reward label from the
review prose (tl\_fanduel's flagship stores "boost" — the consequence is an
under-planned boost card, never a fabricated value); one sentence gluing a
genuine geographic may-not to combinability copy ("NJ residents may not
participate in this promotion and it cannot be combined with any other
offer") reads the combinability marker and loses the NJ veto — the flip
side of item 67's trade, moderately contrived phrasing; bare "but not in
<state>" tails keep the head eligible but attribute no veto — the state is
merely unconfirmed; dotted
European dates ("until 31.12.2026", live on leovegas_on) never set
`ends_at` — fail-closed, no expiry gate; "Washington DC residents are
excluded" over-excludes WA beside DC; a ", and"-glued eligible list may be
swallowed into ineligible by the exclusion walk (item 49's fail-closed
trade); and dotted state abbreviations ("N.Y. residents are excluded") are
invisible to both polarities — the clause dots split them apart, so nothing
is confirmed and nothing is excluded.

### Path probes: hardrock, fanatics, thescore, betrivers — 15 requests, no new routes

Budgets declared up front (4/5/3/3 with one page-mode observation each for fanatics and
betrivers), stop-on-conclusive, one closing control per venue.  Egress: the operator's
own Illinois connection; ipapi.co detection still 403s, the first probe verified via the
continuity fallback (fingerprint `34e56847c7ec…`, unchanged since 2026-08-14), the rest
ran `--template-only`.  Manifests under ignored `data/research/<venue>/IL/20260816T*`.

| # | request | answer |
| --- | --- | --- |
| H0 | `www.hardrock.bet/promotions` (registry URL) | **404**, 396,319 B, sha256 `5fab45480a1e` — a full page shell whose casino-blog links parse into 2 phantom offers |
| H1 | `app.hardrock.bet/en-us/promotions` | **200**, 16,349 B, `f4c0d79b8699` — JS shell, "You need to enable JavaScript", zero promo words |
| H2 | same URL, browser navigation | 200, 15,585 B, `0544265b15cc` — still the pre-render shell |
| Hc | `app.hardrock.bet/en-us/sports/` (control) | 200, 16,349 B — host answers; the verdicts above are path verdicts |
| F0 | `sportsbook.fanatics.com/promotions` | **404**, 215 B, `3250c6832757` (bare 404, not the Akamai challenge) |
| F1 | `betfanatics.com/promos` (browser) | 404, 42,080 B, `48a67f2b2a55` |
| F2 | `betfanatics.com/promotions` (browser) | 404, 42,053 B, `bb39c3e7381e` |
| F3 | one page-mode observation of `betfanatics.com` | 19 responses; the site's own RSC nav prefetches enumerate search / casino / blog / responsible-gaming — **no promotions route exists**, and the oddschecker promo widget seen 2026-08-14 is gone |
| Fc | repeat F0 (control) | 404, 281 B, `ebd896cc0e61` |
| T1 | `sportsbook.thescore.bet/promotions` | **200**, 3,501 B, `9f0139ad3c0d` — Next.js shell, GeoComply loader, zero promo words |
| T2 | same URL, browser navigation | byte-identical shell |
| Tc | site root (control) | 200, same 3,501 B document — the whole site is one SPA shell |
| B1 | `il.betrivers.com/promotions` | 200, 6,831 B, `81ab3033aa82` — a "Redirecting…" JS stub |
| B2 | one page-mode observation of `il.betrivers.com` | 51 responses, 2 websockets; the only promo-flavored XHRs are **config**: `promotionsMF.json` (a micro-frontend template URL on rushstreetcontent.com), `landingBanner.json` (imagery only), `bonusNotifications.json` (account-scoped), `promoJackpotConf.json` (97 B of feature flags).  No anonymous catalog endpoint was observed; none was invented |
| Bc | landing root (control) | 200, 262,296 B — the known-good landing still answers |

**Verdict: no registry changes.** Hard Rock's promotions route exists only as a
client-rendered SPA on `app.hardrock.bet` (the registry's `www.` host is wrong *and*
irrelevant — both fail); Fanatics has no anonymous promo surface on either host;
theScore's catalog renders client-side behind GeoComply (the campaign-book coverage gap
is now probed-and-closed, not merely assumed); BetRivers' landing keeps `empty_is_ok`.
The tl\_ failovers stay canonical for hardrock and fanatics.  Two genuine bodies were
committed as fixtures with tests: `hardrock_promotions_404.html` (the phantom-offer body
— the fetch guard's 404 refusal is the pinned defense, the caesars-403 pattern) and
`hardrock_promotions_app_shell.html` (parses to zero with `no_promo_signals`, so a
future registry switch to the app host grades honestly).  A `betrivers`
`ResearchProfile` was added to `src/sources/research.py` for these probes.

## Multi-state batch: four runs, one fetch — 2026-08-16

`python -m src.promos collect --state IL --state PA --state NJ --state DC`, the first batch
ever, hours after first contact and after label-don't-drop shipped. Runs #2–#5, one
`batch_id` (`a3fd0daf…`), **54 offers stored per state** with per-state verdicts:

| state | confirmed / stored | the confirmed set |
| --- | --- | --- |
| IL | 3 / 54 | 2 DraftKings boosts + tl_betmgm "$150 if you win" |
| PA | 4 / 54 | same + tl_fanatics "Bet $20, Get $200 in FanCash" |
| NJ | 4 / 54 | same as PA |
| DC | 3 / 54 | same as IL |

Global sources were fetched **once** and replayed per state (PA/NJ/DC per-source latencies
of 3–8 ms are cache replays, not requests). The run verdict flipped DEGRADED→OK and source
health became honest: betmgm grades OK with its 12 offers where the drop-based gate had
graded it `FAILED [deduped_empty]`; the venues that grade FAILED now are the genuine walls
(fanduel PerimeterX, caesars 403, bet365/onexbet captcha, smarkets cookie wall, betmgm_on
403, fanatics/hardrock 404) plus `vague_offers` on catalogs whose copy carries no numbers.

**What a state label on these runs asserts, precisely.** All four runs were fetched from a
single Illinois egress. `state_confirmed=True` asserts only that the offer's *own copy*
named the state — not that the venue was asked from inside it, and not that the copy is
well-attributed: the tl_betmgm "$150 if you win" confirms in all four states because
TheLines describes two different BetMGM promos in one blob, and the *other* promo's state
list (which includes IL and DC) bleeds into this one's verdict. Its own sentence says
MI/NJ/PA/WV. A venue that geo-varies its public catalog would not be caught either — no
promo `GeoRestrictedError` analogue exists. The stored description on each card is the
operator's recourse; the label is a screen, not a verdict.

## First live promo collection, Illinois — 2026-08-16

The promotions subsystem's first execution ever (`promo_runs` was empty until this run).
`python -m src.promos collect --state IL -v`, from the operator's own Illinois egress, after
a fresh full odds slate (run 30: 85,531 quotes, 35 sources). Batch stored as promo run #1,
verdict **DEGRADED**, exit 1 — 3 offers from 5/24 sources, 5/17 brands.

### Per-source results, all 24

| source | result | detail |
| --- | --- | --- |
| fanduel | **split** | merchandising API answered 200 with `{"promotions":[]}` — zero promos merchandised to anonymous IL; the marketing page was blocked by PerimeterX. Health graded `bot_wall` |
| draftkings | OK | 6 offers, 109 KiB; 2 survived the state gate (both profit boosts whose copy named states) |
| bovada | OK→emptied | 5 offers parsed; every one dropped by the state gate; graded `deduped_empty` |
| cloudbet | OK→emptied | 5 offers; same fate |
| leovegas_kambi | OK→emptied | 4 offers; same |
| betmgm | OK→emptied | **12 offers**, the largest catalog of the run; all dropped by the gate; graded `deduped_empty` |
| caesars | refused | HTTP 403 bot-script body. The body *would* parse into 2 phantom offers — the fetch guard is the defense, pinned by test |
| fanatics | 404 | promotions index path has moved |
| hardrock | 404 | promotions index path has moved |
| bet365 | refused | Cloudflare challenge (`challenge-platform`) |
| leovegas_on | OK→emptied | 4 offers; gate |
| betmgm_on | refused | HTTP 403 without a parseable body |
| onexbet | refused | CAPTCHA on the bonus page |
| pinnacle | OK | 0 offers, `empty_is_ok` — its landing genuinely carries no promo copy |
| smarkets | refused | cookie/bot challenge |
| matchbook | OK | 0 offers, `empty_is_ok` |
| betrivers_kambi | OK | 0 offers, 1 skipped, 257 KiB — the IL landing carries no parseable promo copy |
| tl_* (7 tenants) | OK | one shared fetch; 18 offers total; only `tl_betmgm`'s single offer survived the gate |

**No parse crashes anywhere.** The four families that had never met production HTML either
parsed correctly (bovada 5, betmgm 12, betrivers/pinnacle honest empties) or were refused by
walls. Eight genuine bodies were committed as fixtures under `tests/fixtures/promos/`
(including the refusal bodies), each with a focused test; sources named in the table above.

### The unconfirmed measurement — 94% of the catalog eaten

Pre-gate offers ≈ 54 (draftkings 6, betmgm 12, bovada 5, cloudbet 5, leovegas 4+4,
TheLines 18). Stored: **3**. `filtered unconfirmed for IL: 51`.

The fail-closed gate (`offer_confirmed_for_state`) requires the offer's own copy to name the
state, and marketing copy overwhelmingly doesn't. This is far past the plan's decision
threshold (half), so **label-don't-drop is mandatory** and follows the operator's 2026-08-08
odds-side precedent (collect and show, label every surface).

*Shipped the same day:* every offer now stores the gate's verdict as `state_confirmed`
(store schema v3) instead of being dropped on False. The predicate is unchanged; the CLI
prints `confirmed=NOT {ST}` per offer, the dashboard pills unconfirmed rows, and every
planner card for an unconfirmed offer opens with a verify-in-app caveat.

The audit-trail defect is visible in the health grades themselves: betmgm answered with 12
genuine offers and is graded `FAILED [deduped_empty]` because the gate emptied it
downstream. A working source reads as broken, and the dropped offers leave no row anywhere.

### The one survivor is wrong — twice

The single national offer that passed the IL gate, `tl_betmgm` "Bet $10 get $150 in bonus
bets **if you win**", should not have passed and was then mispriced:

1. **Wrong state.** TheLines' BetMGM copy describes *two* different promos in one blob: a
   "$1,500 paid back if you don't win" valid in a list *including IL*, and the "$150 if you
   win" available only in **MI, NJ, PA and WV**. `parse_eligibility` merged both lists, so
   the NJ/PA-only variant was stored as IL-confirmed — while 51 legitimate offers dropped.
   One description covering multiple offers defeats region attribution; the honest fix is
   the Phase 1 label, which shows the operator the copy rather than asserting eligibility.
2. **Wrong price.** The planner printed `ev=+108.57` with no win-conditional caveat: its
   `_WIN_CONDITIONAL` regex matches "if your bet wins" but not the plain **"if you win"**.
   Conditional credit was priced as unconditional. The honest number is
   `p(win) × conversion × 150 − qualifying_cost` — still positive, materially smaller.
   Found on the first live offer this planner ever priced; *fixed the same day*: the
   subject set admits the bare "you" (with the unconditional "win or lose" veto mirrored
   onto the win reading so the commonest bet-and-get wording stays unconditional), and
   the classifiers read summary+title — the enricher's canonical summary drops trailing
   clauses, and the description is deliberately excluded because it is where the *other*
   offer's state list and loss clause live.

### The planner worked on first contact

`promo run #1 × odds run #30: 3 offers over 27,640 priceable markets`. All three offers
produced concrete plans: DraftKings 25% profit boosts as `bonus_conversion`
(63.7–67.9% measured conversion) and `boost_locked` (+$11–12 per $100), and the
qualify-then-convert card above ($0.04 measured qualifying cost against a $10 stake).
(The `bonus_conversion` routing later proved to be item 60's misprice — the reward
was read out of a negating sentence, and the boost lock is the true model; recorded
here as it was measured at the time.)
Gate counters fired in volume and correctly (`below_min_odds ×457`, `no_hedge_price ×2471`,
`observation_spread ×1752`, `regime_mismatch ×87` — the prediction-market exclusion working).
Known display gap, accepted for now: hedge legs carry no locality marking, so a plan may
name a hedge book (smarkets, onexbet) not reachable from the run's state.

### Operational notes

- `python -m src.promos collect` has **no alert path** — it cannot text anyone. `plan` is
  fully offline.
- `plan` auto-selects the latest odds run for the promo run's jurisdiction; a fresh full
  odds slate must immediately precede planning or hedges price against a stale/thin run.
  `plan --odds-run N` now pins a specific run, refusing cross-jurisdiction picks.
- The planner now refuses offers whose stated `ends_at` has passed (counted as
  `expired`), and the settlement-regime gate — which fired 87 times on the first live
  slate — gained the test that asserts it fires.
- The dashboard's Promos panel gained a **Campaign** table: every offer ranked by the
  planner's own EV figure, claimed-state per browser; the operator playbook it serves is
  `docs/PROMO_CAMPAIGN.md`. The bet ledger records promo positions truthfully
  (`stake_kind`, bonus stakes excluded from bankroll, `promo_source`/`promo_offer_id`
  linkage).
- `stakeable_odds_sources` filters by the *ambient* `VIEW_ONLY_SOURCES` (process
  `ODDS_STATE`) — suppress-only, documented at `planner.py:340-347`; left as is.
- Automatic egress detection failed this run (ipapi.co 403 — its known Cloudflare wall);
  detection is advisory and the explicit `--state IL` governed.
