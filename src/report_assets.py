"""Static assets for the dashboard: stylesheet, markup shell, and behaviour.

Kept apart from :mod:`src.report` so that module stays about *what the numbers
are* and this one about how they are shown.  Everything here is inline — the page
must open from a local file with no network access at all, so there are no CDN
stylesheets, no webfonts, and no remote images.  A webfont that silently fell
back would also break the column alignment the tables depend on.

The copy is written for someone who has never placed a bet.  Betting notation is
compact but opaque, so the tables lead with a sentence ("Reds win by 2 or more")
and keep the book's own wording as a tooltip and in the glossary.  The technical
vocabulary is still reachable — it is just not the first thing on screen.

Nothing here is sport-specific.  What a total counts, and whether a draw is a real
outcome, come from the payload (:func:`src.report._sport_facts`), which reads them
out of :mod:`src.vocab` — so the page says "runs" for baseball and "goals" for
hockey without either being written down twice, and it cannot describe a bet in
terms the pipeline does not actually settle it on.
"""
from __future__ import annotations

CSS = """
*, *::before, *::after { box-sizing: border-box; }

:root {
  color-scheme: light dark;

  /* Blue-biased neutrals: a pure grey reads as unconsidered, and biasing toward
     the accent leaves red/green free to mean only "price moved". */
  --ground:      #eef1f6;
  --surface:     #ffffff;
  --surface-2:   #f6f8fb;
  --line:        #d8dfe9;
  --line-soft:   #e7ecf3;
  --ink:         #131922;
  --ink-2:       #3d4757;
  --muted:       #5a6675;
  --accent:      #1b44a8;
  --accent-soft: #e6ecfa;
  --up:          #0b7a4b;
  --down:        #b32318;
  --warn:        #8a5a00;
  --warn-soft:   #fdf3df;
  --bad-soft:    #fdeceb;
  --good-soft:   #e6f4ec;

  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;

  --rail: 236px;
  --pad: 22px;
  --radius: 10px;
}

@media (prefers-color-scheme: dark) {
  :root {
    --ground:      #0b0f15;
    --surface:     #141b24;
    --surface-2:   #1a2230;
    --line:        #2a3542;
    --line-soft:   #212b37;
    --ink:         #e4eaf2;
    --ink-2:       #b6c2d1;
    --muted:       #8b98a9;
    --accent:      #6fa0ff;
    --accent-soft: #17253d;
    --up:          #3fc98a;
    --down:        #ff7a6e;
    --warn:        #e0ab4f;
    --warn-soft:   #2a2113;
    --bad-soft:    #2c1715;
    --good-soft:   #10241a;
  }
}

/* The viewer's own toggle must win over the OS preference in both directions. */
:root[data-theme="dark"] {
  --ground: #0b0f15; --surface: #141b24; --surface-2: #1a2230;
  --line: #2a3542; --line-soft: #212b37; --ink: #e4eaf2; --ink-2: #b6c2d1;
  --muted: #8b98a9; --accent: #6fa0ff; --accent-soft: #17253d;
  --up: #3fc98a; --down: #ff7a6e; --warn: #e0ab4f;
  --warn-soft: #2a2113; --bad-soft: #2c1715; --good-soft: #10241a;
}
:root[data-theme="light"] {
  --ground: #eef1f6; --surface: #ffffff; --surface-2: #f6f8fb;
  --line: #d8dfe9; --line-soft: #e7ecf3; --ink: #131922; --ink-2: #3d4757;
  --muted: #5a6675; --accent: #1b44a8; --accent-soft: #e6ecfa;
  --up: #0b7a4b; --down: #b32318; --warn: #8a5a00;
  --warn-soft: #fdf3df; --bad-soft: #fdeceb; --good-soft: #e6f4ec;
}

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font: 400 14px/1.55 var(--sans);
  -webkit-font-smoothing: antialiased;
}

a { color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

/* ── frame ─────────────────────────────────────────────────────────────── */

.shell { display: grid; grid-template-columns: var(--rail) minmax(0, 1fr); gap: 0; }

.rail {
  position: sticky; top: 0; align-self: start; height: 100vh;
  display: flex; flex-direction: column; gap: 18px;
  padding: 20px 16px; border-right: 1px solid var(--line);
  background: var(--surface); overflow-y: auto;
}
.brand { display: flex; flex-direction: column; gap: 2px; }
.brand b { font: 800 15px/1.2 var(--sans); letter-spacing: -0.02em; }
.brand span { font: 400 11px/1.4 var(--mono); color: var(--muted); }

.nav { display: flex; flex-direction: column; gap: 1px; }
.nav a {
  display: flex; justify-content: space-between; gap: 8px; align-items: baseline;
  padding: 7px 10px; border-radius: 7px; text-decoration: none;
  color: var(--ink-2); font-size: 13px; font-weight: 500;
}
.nav a:hover { background: var(--surface-2); color: var(--ink); }
.nav a[aria-current="true"] { background: var(--accent-soft); color: var(--accent); }
.nav a i { font: 400 11px/1 var(--mono); color: var(--muted); font-style: normal; }

.rail-block { display: flex; flex-direction: column; gap: 6px; }
.rail-block > label,
.eyebrow {
  font: 600 10px/1.2 var(--sans); letter-spacing: 0.09em;
  text-transform: uppercase; color: var(--muted);
}
select, input[type="search"], input[type="text"] {
  width: 100%; padding: 6px 8px; border: 1px solid var(--line);
  border-radius: 7px; background: var(--surface); color: var(--ink);
  font: 400 12px/1.4 var(--sans);
}
.rail-foot { margin-top: auto; font: 400 11px/1.5 var(--mono); color: var(--muted); }

main { min-width: 0; padding: 0 var(--pad) 96px; }

.masthead {
  display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
  gap: 16px; padding: 34px 0 22px;
}
.masthead h1 { margin: 0; font: 800 27px/1.15 var(--sans); letter-spacing: -0.025em; text-wrap: balance; }
.masthead p { margin: 8px 0 0; max-width: 68ch; color: var(--ink-2); }

/* One panel is on screen at a time.  Ten stacked sections meant scrolling past
   nine of them to reach the tenth; the rail switches between them instead, and
   clicking a row opens the thing that row describes. */
section { display: none; margin-bottom: 34px; }
section.on { display: block; }
section > header { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
section > header h2 { margin: 0; font: 700 16px/1.2 var(--sans); letter-spacing: -0.015em; }
section > header p { margin: 0; color: var(--muted); font-size: 12.5px; max-width: 78ch; }

/* ── drill-down ────────────────────────────────────────────────────────────
   The trail is the only thing telling a reader how deep they are and how to get
   back, so it is always rendered — even one level down, where it is just the
   panel's own name. */

.crumbs { display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px; margin: 0 0 16px; font-size: 12.5px; }
.crumbs a { color: var(--muted); text-decoration: none; }
.crumbs a:hover { color: var(--accent); text-decoration: underline; }
.crumbs b { color: var(--ink); font-weight: 600; }
.crumbs i { font-style: normal; color: var(--muted); opacity: 0.5; }

/* A row that opens something has to look like it does. */
tbody tr.go { cursor: pointer; }
tbody tr.go:hover { background: var(--accent-soft); }
tbody tr.go:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.headline { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
.headline b { font: 700 21px/1.25 var(--sans); letter-spacing: -0.02em; text-wrap: balance; }
.headline span { font: 400 13px/1.5 var(--sans); color: var(--ink-2); }
.headline code { font: 400 11.5px/1.5 var(--mono); color: var(--muted); }

/* One card per venue holding that venue's price for one bet. */
.quotes { display: grid; grid-template-columns: repeat(auto-fit, minmax(212px, 1fr)); gap: 1px; background: var(--line-soft); }
.qcard { display: flex; flex-direction: column; gap: 8px; padding: 13px 14px; background: var(--surface); }
.qcard .who { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.qcard .who b { font: 700 13.5px/1.2 var(--sans); }
.qcard .price { font: 700 27px/1 var(--mono); font-variant-numeric: tabular-nums; letter-spacing: -0.03em; }
.qcard.top .price { color: var(--up); }
.qcard.off .price { color: var(--muted); }
.qcard dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 0; font-size: 12px; }
.qcard dt { color: var(--muted); }
.qcard dd { margin: 0; font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; }

.card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden;
}
.card + .card { margin-top: 14px; }
.card-head {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  gap: 10px; padding: 12px 14px; border-bottom: 1px solid var(--line-soft);
  background: var(--surface-2);
}
.card-head h3 { margin: 0; font: 700 13px/1.2 var(--sans); }
.card-head .eyebrow { font-size: 10px; }
.card-body { padding: 14px; }
.card-body.flush { padding: 0; }

/* ── explainers ────────────────────────────────────────────────────────────
   A novice reader's difficulty is rarely the numbers themselves; it is not
   knowing what a block of numbers is answering.  So every block is broken into
   named compartments and captioned where it sits, rather than in one long
   preamble nobody scrolls back to. */

/* Hairline-separated compartments inside a single card. */
.tiles {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(218px, 1fr));
  gap: 1px; background: var(--line-soft);
}
.tile { display: flex; flex-direction: column; gap: 5px; padding: 13px 14px; background: var(--surface); }
.tile .ord {
  font: 600 9.5px/1.2 var(--sans); letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--accent);
}
.tile h4 { margin: 0; font: 700 13.5px/1.25 var(--sans); }
.tile p { margin: 0; font-size: 12.5px; color: var(--ink-2); max-width: 44ch; }
.tile b.big { font: 700 17px/1.15 var(--mono); font-variant-numeric: tabular-nums; color: var(--ink); }
.tile code { font: 400 11.5px/1.5 var(--mono); color: var(--muted); }

/* One or two short captions, pinned directly above the data they describe. */
.brief {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 8px 22px; padding: 10px 14px;
  background: color-mix(in srgb, var(--accent) 4%, var(--surface));
  border-bottom: 1px solid var(--line-soft);
}
.brief p { margin: 0; font-size: 12.5px; color: var(--ink-2); max-width: 62ch; }
.brief b {
  display: block; font: 600 9.5px/1.4 var(--sans); letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--accent);
}
.brief em { font-style: normal; font-weight: 600; color: var(--ink); }
.brief.solo { border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 14px; }

/* The page's own contents: each part paired with the question it answers. */
.tour { display: grid; gap: 1px; background: var(--line-soft); }
.tour a {
  display: grid; grid-template-columns: 2.5ch minmax(110px, 176px) 1fr; gap: 4px 14px;
  align-items: baseline; padding: 9px 14px; background: var(--surface);
  color: var(--ink); text-decoration: none; font-size: 12.5px;
}
.tour a:hover { background: var(--surface-2); }
.tour i { font: 600 10.5px/1.5 var(--mono); font-style: normal; color: var(--muted); }
.tour b { font-weight: 600; }
.tour span { color: var(--muted); }

/* ── numbers ───────────────────────────────────────────────────────────── */

.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(146px, 1fr)); gap: 1px; background: var(--line-soft); }
.stat { background: var(--surface); padding: 13px 14px; display: flex; flex-direction: column; gap: 3px; }
.stat b { font: 700 22px/1.1 var(--mono); font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
.stat span { font: 600 10px/1.2 var(--sans); letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
.stat small { font: 400 11.5px/1.35 var(--sans); color: var(--muted); }
.stat.is-good b { color: var(--up); }
.stat.is-bad b { color: var(--down); }
.stat.is-warn b { color: var(--warn); }

.pill {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px;
  border-radius: 999px; font: 600 11px/1.5 var(--sans); border: 1px solid transparent;
}
.pill.ok   { background: var(--good-soft); color: var(--up); border-color: color-mix(in srgb, var(--up) 30%, transparent); }
.pill.bad  { background: var(--bad-soft);  color: var(--down); border-color: color-mix(in srgb, var(--down) 30%, transparent); }
.pill.warn { background: var(--warn-soft); color: var(--warn); border-color: color-mix(in srgb, var(--warn) 30%, transparent); }
.pill.flat { background: var(--surface-2); color: var(--muted); border-color: var(--line); }
.pill.accent { background: var(--accent-soft); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 25%, transparent); }
.pill i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.num  { font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.dim  { color: var(--muted); }
.up   { color: var(--up); }
.down { color: var(--down); }
.plain { font-weight: 500; white-space: nowrap; }

/* ── tables ────────────────────────────────────────────────────────────── */

.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { padding: 7px 12px; text-align: left; border-bottom: 1px solid var(--line-soft); white-space: nowrap; }
thead th {
  position: sticky; top: 0; z-index: 1; background: var(--surface-2);
  font: 600 10px/1.4 var(--sans); letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--muted); border-bottom: 1px solid var(--line);
}
/* A grouping row above the headings, so a wide table reads as three or four
   labelled bands instead of a dozen equal columns.  Fixed height, because the
   headings below stick to exactly that offset when the body scrolls. */
thead tr.grouped th {
  top: 0; z-index: 2; height: 24px; padding: 0 12px;
  font: 600 9.5px/24px var(--sans); letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--accent); border-bottom: 1px solid var(--line-soft);
}
thead tr.grouped + tr th { top: 24px; }
th.gsep, td.gsep { border-left: 1px solid var(--line); }
thead th.sortable { cursor: pointer; user-select: none; }
thead th.sortable:hover { color: var(--ink); }
thead th[aria-sort]:not([aria-sort="none"]) { color: var(--accent); }
tbody tr:hover { background: var(--surface-2); }
tbody tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; }
td.wrap { white-space: normal; min-width: 22ch; }
.tall { max-height: 470px; overflow: auto; }
.best { color: var(--up); font-weight: 700; }
.empty { padding: 26px 14px; text-align: center; color: var(--muted); font-size: 12.5px; }

/* ── source cards ──────────────────────────────────────────────────────── */

.sources { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 14px; }
.src {
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 14px; display: flex; flex-direction: column; gap: 10px;
  color: inherit; text-decoration: none;
}
.src:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: var(--surface-2); }
.src-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.src-top b { font: 700 15px/1.2 var(--sans); }
.src-top code { display: block; font: 400 10.5px/1.5 var(--mono); color: var(--muted); word-break: break-all; }
.src-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 9px 10px; }
.src-grid div { display: flex; flex-direction: column; }
.src-grid b { font: 700 15px/1.2 var(--mono); font-variant-numeric: tabular-nums; }
.src-grid span { font: 600 9.5px/1.3 var(--sans); letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
.src p { margin: 0; font-size: 12.5px; color: var(--ink-2); }

/* ── pipeline ──────────────────────────────────────────────────────────── */

.flow { display: flex; flex-wrap: wrap; align-items: stretch; gap: 8px; }
.flow-step { flex: 1 1 150px; border: 1px solid var(--line); border-radius: 8px; padding: 11px 12px; background: var(--surface-2); }
.flow-step b { display: block; font: 700 18px/1.2 var(--mono); font-variant-numeric: tabular-nums; }
.flow-step span { font: 600 9.5px/1.3 var(--sans); letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
.flow-step small { display: block; margin-top: 3px; font-size: 11.5px; color: var(--ink-2); }
.flow-arrow { align-self: center; color: var(--muted); font-family: var(--mono); }

/* ── coverage grid ─────────────────────────────────────────────────────── */

.cov td.cell { text-align: center; font-family: var(--mono); font-variant-numeric: tabular-nums; padding: 5px 8px; }
.cov td.cell.zero { color: color-mix(in srgb, var(--muted) 50%, transparent); }
.cov tbody tr { cursor: pointer; }
.cov tbody tr.sel { background: var(--accent-soft); }
.heat { display: inline-block; min-width: 34px; padding: 2px 6px; border-radius: 5px; }

/* ── charts ────────────────────────────────────────────────────────────── */

.chart { width: 100%; height: auto; display: block; }
.spark { display: block; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font: 400 11.5px/1.4 var(--sans); color: var(--muted); }
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.swatch { width: 9px; height: 9px; border-radius: 2px; }

/* ── misc ──────────────────────────────────────────────────────────────── */

.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.controls select, .controls input { width: auto; min-width: 130px; }
.note { margin: 10px 0 0; font-size: 12.5px; color: var(--muted); max-width: 88ch; }
.note code { font-family: var(--mono); }
.bars { display: flex; flex-direction: column; gap: 5px; }
.bar-row { display: grid; grid-template-columns: minmax(120px, 210px) 1fr 56px; gap: 10px; align-items: center; font-size: 12px; }
.bar-row .track { height: 8px; border-radius: 4px; background: var(--surface-2); overflow: hidden; }
.bar-row .fill { height: 100%; background: var(--accent); border-radius: 4px; }
.bar-row .val { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink-2); }

.kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; font-size: 12.5px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; font-family: var(--mono); }

.gloss { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px 26px; }
.gloss > div { display: flex; flex-direction: column; gap: 3px; }
.gloss dt { font: 700 13px/1.3 var(--sans); color: var(--ink); }
.gloss dd { margin: 0; font-size: 12.5px; color: var(--ink-2); max-width: 52ch; }

.notice {
  display: none; gap: 10px; align-items: flex-start; margin-bottom: 18px;
  padding: 11px 13px; border: 1px solid color-mix(in srgb, var(--warn) 35%, var(--line));
  border-radius: 8px; background: var(--warn-soft); color: var(--ink-2); font-size: 12.5px;
}
.notice.on { display: flex; }
.notice b { color: var(--warn); }

@media (prefers-reduced-motion: no-preference) {
  .nav a, tbody tr, .flow-step { transition: background-color 120ms ease, color 120ms ease; }
}

@media (max-width: 860px) {
  .shell { grid-template-columns: minmax(0, 1fr); }
  .rail { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
  .nav { flex-direction: row; flex-wrap: wrap; }
  main { padding: 0 14px 64px; }
}

@media (max-width: 620px) {
  .tour a { grid-template-columns: 2.5ch 1fr; }
  .tour span { grid-column: 2; }
}
"""


BODY = """
<div class="shell">
  <aside class="rail">
    <div class="brand">
      <b>Odds Collector</b>
      <span id="brand-sub">local pipeline</span>
    </div>

    <nav class="nav" id="nav" aria-label="Sections">
      <a href="#overview">Start here</a>
      <a href="#run">This collection</a>
      <a href="#sports">Sports &amp; leagues <i id="nav-sports"></i></a>
      <a href="#sources">Sportsbooks <i id="nav-sources"></i></a>
      <a href="#events">Fixtures <i id="nav-events"></i></a>
      <a href="#odds">All prices <i id="nav-odds"></i></a>
      <a href="#movement">Price changes <i id="nav-move"></i></a>
      <a href="#quality">Checks <i id="nav-quality"></i></a>
      <a href="#raw">Saved pages <i id="nav-raw"></i></a>
      <a href="#glossary">Glossary</a>
      <a href="#schema">Field reference</a>
    </nav>

    <div class="rail-block">
      <label for="run-pick">Which collection to show</label>
      <select id="run-pick"></select>
      <span class="rail-foot" id="run-meta" style="margin:0"></span>
    </div>

    <div class="rail-block">
      <label for="sport-pick">Which sport to show</label>
      <select id="sport-pick"></select>
      <span class="rail-foot" id="sport-meta" style="margin:0"></span>
    </div>

    <div class="rail-foot" id="built"></div>
  </aside>

  <main>
    <header class="masthead">
      <div>
        <h1>What the collector grabbed</h1>
        <p id="lede"></p>
      </div>
      <div id="masthead-pills" class="controls"></div>
    </header>

    <nav class="crumbs" id="crumbs" aria-label="Where you are"></nav>

    <div class="notice" id="run-notice"></div>

    <section id="overview">
      <header>
        <h2>Start here</h2>
        <p>Three things about betting prices, the four kinds of bet, then a map of the page.
        About a minute, and the rest of the page will make sense.</p>
      </header>

      <div class="card">
        <div class="card-head"><h3>How to read a price</h3><span class="eyebrow">in order — each builds on the last</span></div>
        <div class="card-body flush">
          <div class="tiles">
            <div class="tile">
              <span class="ord">1 &middot; the price</span>
              <b class="big">2.30 &rarr; $230 back</b>
              <p>Bet $100 at 2.30 and a win returns $230 in total: your $100 back, plus
              $130 profit.</p>
              <code>a US book writes this as +130</code>
            </div>
            <div class="tile">
              <span class="ord">2 &middot; the chance</span>
              <b class="big">1 &divide; 2.30 = 43%</b>
              <p>Flip the price over and you get roughly how likely the book thinks it is.
              Short price, likely; long price, unlikely.</p>
              <code>&minus;150 means stake $150 to profit $100</code>
            </div>
            <div class="tile">
              <span class="ord">3 &middot; the book's cut</span>
              <b class="big">43% + 61% = 104%</b>
              <p>Both sides of a bet are priced, and they add up to over 100%. That extra
              4% is the sportsbook's margin.</p>
              <code>under 100% would be impossible</code>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>The four kinds of bet collected</h3><span class="eyebrow">nothing else is stored</span></div>
        <div class="brief">
          <p><b>why only four</b> These four exist at every sportsbook in the same form, so
          their prices can be compared between books. Everything else is counted and left
          alone — see <a href="#sources">Sportsbooks</a>.</p>
        </div>
        <div class="card-body flush">
          <div class="tiles">
            <div class="tile"><h4>Who wins</h4>
              <p>Pick the winning team. Nothing else matters.</p><code>Reds win</code></div>
            <div class="tile"><h4>Winner with a handicap</h4>
              <p>One team starts with runs added or taken away, which evens out a mismatch.</p>
              <code>Reds win by 2 or more</code></div>
            <div class="tile"><h4>Combined total</h4>
              <p>Both sides' scores added together, over or under a number. What gets
              counted depends on the sport: runs, goals, points or games.</p>
              <code>9 or more runs in the game</code></div>
            <div class="tile"><h4>One side's total</h4>
              <p>Just one side's score, over or under a number.</p>
              <code>Reds score 5 or more</code></div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>What's on this page</h3><span class="eyebrow">the question each part answers</span></div>
        <div class="card-body flush">
          <div class="tour" id="tour">
            <a href="#run"><i>01</i><b>This collection</b><span>How much was grabbed just now, and did every step work?</span></a>
            <a href="#sports"><i>02</i><b>Sports &amp; leagues</b><span>Which sports two or more books priced — the ones that can be compared at all.</span></a>
            <a href="#sources"><i>03</i><b>Sportsbooks</b><span>Which books were read, and what came back from each?</span></a>
            <a href="#events"><i>04</i><b>Fixtures</b><span>Which fixtures are on, and which books priced them?</span></a>
            <a href="#odds"><i>05</i><b>All prices</b><span>Every price collected, searchable and sortable.</span></a>
            <a href="#movement"><i>06</i><b>Price changes</b><span>Which prices moved — the evidence this is a live feed.</span></a>
            <a href="#quality"><i>07</i><b>Checks</b><span>What was tested, and anything that looked wrong.</span></a>
            <a href="#raw"><i>08</i><b>Saved pages</b><span>The original files every number here was read from.</span></a>
            <a href="#glossary"><i>09</i><b>Glossary</b><span>Any betting word used above, in plain English.</span></a>
            <a href="#schema"><i>10</i><b>Field reference</b><span>What gets stored for one price, for querying the database.</span></a>
          </div>
        </div>
      </div>
    </section>

    <section id="run">
      <header>
        <h2>This collection</h2>
        <p>One collection is one pass over every venue. The sidebar switches
        between every collection stored.</p>
      </header>

      <div class="card">
        <div class="card-head"><h3>The headline numbers</h3><span class="eyebrow">this collection only</span></div>
        <div class="brief">
          <p><b>a price vs a bet</b> One <em>bet</em> — say who wins tonight's game at one
          book — has two or three sides, and each side is one <em>price</em>. That is why
          there are far more prices than bets.</p>
        </div>
        <div class="card-body flush"><div class="stats" id="stat-strip"></div></div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Step by step, what happened</h3>
          <span class="eyebrow">each step feeds the next</span>
        </div>
        <div class="brief">
          <p><b>read it as a funnel</b> Pages asked for, pages saved, bets read, bets that
          passed every check, bets stored. Anything dropped between two steps is accounted
          for somewhere on this page.</p>
          <p><b>why saving comes second</b> Pages go to disk <em>before</em> anything reads
          them, so the whole run can be re-checked later without going back online — and no
          number can be quietly changed after the fact.</p>
        </div>
        <div class="card-body"><div class="flow" id="flow"></div></div>
      </div>

      <div class="card">
        <div class="card-head"><h3>What was collected, by kind of bet</h3><span class="eyebrow">prices per venue</span></div>
        <div class="brief">
          <p><b>how to read it</b> One row per kind of bet and part of the game; one column
          per venue. A dash means that venue published nothing of that kind.</p>
        </div>
        <div class="card-body flush scroll"><table id="matrix"></table></div>
      </div>
    </section>

    <section id="sports">
      <header>
        <h2>Sports &amp; leagues</h2>
        <p>Several sports are collected at once. This is the part that says which of them
        can actually be used — and which cannot, and why.</p>
      </header>

      <div class="card">
        <div class="card-head">
          <h3>Which sports two or more books priced</h3>
          <span class="eyebrow" id="sports-bar-note"></span>
        </div>
        <div class="brief">
          <p><b>the bar</b> Two books is the minimum for a sport to be usable: one book's
          price cannot be compared with anything, so a sport only one book covered is
          marked <em>not comparable</em> however many prices it has.</p>
          <p><b>how to read it</b> One row per sport, one column per venue, counting
          the prices stored. A dash means that book published nothing for that sport.</p>
        </div>
        <div class="card-body flush scroll"><table id="sports-grid"></table></div>
      </div>

      <div class="card">
        <div class="card-head"><h3>Leagues within each sport</h3><span class="eyebrow">prices per venue</span></div>
        <div class="brief">
          <p><b>why league is shown but never joined on</b> The books disagree about
          classification constantly — the same tennis match is a tour event at one book and
          a challenger at another — so a fixture's identity never depends on it. It is
          recorded so coverage can be reported, and nothing else.</p>
        </div>
        <div class="card-body flush scroll tall"><table id="leagues-grid"></table></div>
      </div>

      <div class="card">
        <div class="card-head"><h3>Asked for, nothing came back</h3><span class="eyebrow">gaps, not absences</span></div>
        <div class="brief">
          <p><b>why this table exists</b> A league that returned nothing looks exactly like
          a league nobody asked for. Each book records which leagues it was configured to
          collect, so a genuine gap can be told apart from a deliberate omission.</p>
        </div>
        <div class="card-body flush scroll"><table id="sports-gaps"></table></div>
      </div>
    </section>

    <section id="sources">
      <header>
        <h2>Where the prices come from</h2>
        <p>Sportsbooks, betting exchanges and prediction markets, each read straight from
        its own public website. No paid data provider, no account, no login. They are not
        the same kind of thing: a book takes the other side of your bet and its margin is
        already in the price, while an exchange or a prediction market charges a
        commission on top and does not always refund a game that is called off. Each card
        says which it is.</p>
      </header>
      <div class="brief solo">
        <p><b>what each card shows</b> What kind of venue it is, what it charges, how much
        it returned this time, how long it took, and whether the reply was byte-for-byte
        the same as last time.</p>
        <p><b>click a card</b> to open that venue on its own — what it published, what it
        skipped, and every page saved from it.</p>
      </div>
      <div class="sources" id="source-cards"></div>
      <div class="card">
        <div class="card-head">
          <h3>What was left alone on purpose</h3>
          <span class="eyebrow">seen, counted, not collected</span>
        </div>
        <div class="brief">
          <p><b>why</b> These bets are on the books' pages but outside the four kinds
          collected here. Recording one as something it isn't would be worse than not having
          it, so each one is counted and skipped rather than guessed at.</p>
        </div>
        <div class="card-body flush scroll tall"><table id="skips"></table></div>
      </div>
    </section>

    <section id="book">
      <div class="headline">
        <b id="book-title">Pick a venue</b>
        <span id="book-what"></span>
        <code id="book-host"></code>
      </div>
      <div class="card">
        <div class="card-head"><h3>What came back from this book</h3><span class="eyebrow" id="book-state"></span></div>
        <div class="brief">
          <p><b>if one fails</b> The run carries on with the others. A book that returned a
          block page, a login page or nothing is named here rather than quietly vanishing.</p>
        </div>
        <div class="card-body flush"><div class="stats" id="book-stats"></div></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>What it published</h3><span class="eyebrow">prices by kind of bet</span></div>
        <div class="brief">
          <p><b>coverage is the point</b> Books differ in what they price at all. A blank row
          here is why some bets cannot be compared between books.</p>
        </div>
        <div class="card-body flush scroll"><table id="book-mix"></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>What it offered that was left alone</h3><span class="eyebrow" id="book-skip-count"></span></div>
        <div class="card-body flush scroll tall"><table id="book-skips"></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>Pages saved from this book</h3><span class="eyebrow">the originals its numbers came from</span></div>
        <div class="card-body flush scroll"><table id="book-raws"></table></div>
      </div>
    </section>

    <section id="events">
      <header>
        <h2>Fixtures</h2>
        <p>Which fixtures are on, which books priced them, and then every price for one
        fixture with the books side by side. Use the sidebar to narrow this to one sport.</p>
      </header>
      <div class="card">
        <div class="card-head">
          <h3>Which books cover which fixtures</h3>
          <div class="controls">
            <label class="eyebrow" for="cov-mode">Break down</label>
            <select id="cov-mode">
              <option value="source">by venue</option>
              <option value="market">by kind of bet</option>
            </select>
          </div>
        </div>
        <div class="brief">
          <p><b>how to read it</b> One row per fixture. Each cell counts the prices found —
          darker means more. A dash means that book had nothing for that fixture.</p>
          <p><b>click any row</b> to open that fixture on its own, with every book's price
          for it side by side.</p>
        </div>
        <div class="card-body flush scroll"><table class="cov" id="coverage"></table></div>
      </div>
    </section>

    <section id="fixture">
      <div class="headline">
        <b id="event-title">Pick a fixture</b>
        <span id="event-sub"></span>
      </div>
      <div class="card">
        <div class="card-head"><h3>Every bet on this fixture</h3><span class="eyebrow" id="event-count"></span></div>
        <div class="brief">
          <p><b>one row is one bet</b>, written as a sentence, with each book's price beside
          it. Hover a row for the shorthand a sportsbook would print.</p>
          <p><b>green is the best price</b> available. Click any row to open that one bet on
          its own — every book, and how the price has moved.</p>
        </div>
        <div class="card-body flush scroll tall"><table id="event-detail"></table></div>
      </div>
    </section>

    <section id="bet">
      <div class="headline">
        <b id="bet-title">Pick a bet</b>
        <span id="bet-sub"></span>
        <code id="bet-notation"></code>
      </div>
      <div class="card">
        <div class="card-head"><h3>What each venue pays</h3><span class="eyebrow" id="bet-spread"></span></div>
        <div class="brief">
          <p><b>same bet, different books</b> One card per book, showing the price it is
          offering right now. The green one pays the most for the identical wager.</p>
        </div>
        <div class="card-body flush"><div class="quotes" id="bet-books"></div></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>The other sides of this bet</h3><span class="eyebrow">and what the book keeps</span></div>
        <div class="brief">
          <p><b>why this is here</b> A bet only makes sense beside its opposite. Add up the
          chances each book implies for every side and the excess over 100% is its margin on
          this bet specifically.</p>
        </div>
        <div class="card-body flush scroll"><table id="bet-sides"></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>How this price has moved</h3><span class="eyebrow" id="bet-runs"></span></div>
        <div class="brief">
          <p><b>one row per book</b>, one column per collection, oldest on the left. A blank
          cell means that book was not offering this bet when that collection ran.</p>
        </div>
        <div class="card-body flush scroll"><table id="bet-history"></table></div>
      </div>
    </section>

    <section id="odds">
      <header>
        <h2>All prices</h2>
        <p>Every price collected, in one table. Ten venues, ten different formats, one
        set of columns. Prices from a venue that charges commission are shown after it.</p>
      </header>
      <div class="card">
        <div class="card-head">
          <div class="controls">
            <input type="search" id="q" placeholder="team, game, kind of bet&hellip;" aria-label="Filter prices" />
            <select id="f-source"><option value="">every venue</option></select>
            <select id="f-league"><option value="">every league</option></select>
            <select id="f-market"><option value="">every kind of bet</option></select>
            <select id="f-period"><option value="">any part of the game</option></select>
            <select id="f-alt">
              <option value="">main and extra lines</option>
              <option value="0">main line only</option>
              <option value="1">extra lines only</option>
            </select>
          </div>
          <span class="eyebrow" id="odds-count"></span>
        </div>
        <div class="brief">
          <p><b>three bands of columns</b> what the bet is, what it pays, and whether you
          could actually place it right now.</p>
          <p><b>one row is one price</b> at one venue. Hover a row for sportsbook shorthand;
          click any heading to sort by it.</p>
        </div>
        <div class="card-body flush scroll tall"><table id="odds-table"></table></div>
      </div>
      <p class="note" id="odds-note"></p>
    </section>

    <section id="movement">
      <header>
        <h2>Price changes</h2>
        <p>Prices move as money comes in. Seeing them move is how you know this is a live
        feed rather than a saved copy.</p>
      </header>
      <div class="card">
        <div class="card-head"><h3>Every collection so far</h3><span class="eyebrow">prices found, and how long fetching took</span></div>
        <div class="brief">
          <p><b>the bars</b> are how many prices each collection stored. Red means a check
          failed on that one.</p>
          <p><b>the line</b> is how long the fetching took. Oldest on the left, newest on the
          right; the highlighted bar is the collection you are viewing.</p>
        </div>
        <div class="card-body" id="runs-chart"></div>
      </div>
      <div class="card">
        <div class="card-head">
          <h3>Bets whose price moved</h3>
          <div class="controls">
            <select id="move-source"><option value="">every venue</option></select>
            <span class="eyebrow" id="move-count"></span>
          </div>
        </div>
        <div class="brief">
          <p><b>why this matters</b> Prices move as money comes in. Movement here is the
          evidence these are live feeds and not a saved copy being replayed.</p>
          <p><b>the little chart</b> in each row is that one bet's price across every
          collection, oldest on the left.</p>
        </div>
        <div class="card-body flush scroll tall"><table id="move-table"></table></div>
      </div>
      <p class="note" id="move-note"></p>
    </section>

    <section id="quality">
      <header>
        <h2>Checks</h2>
        <p>Collected data is worthless if it is quietly wrong, so every price is tested
        before it is kept. This is what those tests found.</p>
      </header>
      <div class="card">
        <div class="card-head"><h3>The scoreboard</h3><span class="eyebrow">this collection</span></div>
        <div class="brief">
          <p><b>two grades</b> A <em>problem</em> means the data is wrong or unusable. Worth
          a look means it is only suspicious. Both are counted; neither is hidden.</p>
        </div>
        <div class="card-body flush"><div class="stats" id="quality-strip"></div></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>Anything that looked wrong</h3><span class="eyebrow">problems worth a human's attention</span></div>
        <div class="brief">
          <p><b>what these check</b> Mostly things one price cannot know about itself: do the
          books agree on who is at home, is every side of a bet present, is any bet priced
          twice. An empty table is the good case.</p>
        </div>
        <div class="card-body flush scroll tall"><table id="findings"></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>The sportsbook's cut</h3><span class="eyebrow">how much margin is built into each bet</span></div>
        <div class="brief">
          <p><b>what is being counted</b> For each fully-priced bet, the chances the book
          implies for every side, added up. The amount over 100% is its margin — how
          sportsbooks make money.</p>
          <p><b>why it is also a check</b> A bet totalling <em>under</em> 100% would be free
          money, which never happens — so it would mean this tool had paired the wrong prices
          together.</p>
        </div>
        <div class="card-body" id="overround"></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>Prices thrown away</h3><span class="eyebrow">offered, but not understood well enough to keep</span></div>
        <div class="brief">
          <p><b>not the same as skipped</b> These <em>were</em> one of the four kinds of bet,
          but something about the row could not be read safely, so it was dropped and counted
          here instead of stored as a guess.</p>
        </div>
        <div class="card-body flush scroll"><div class="note dim" id="rejections-note" style="padding:8px 14px"></div><table id="rejections"></table></div>
      </div>
    </section>

    <section id="raw">
      <header>
        <h2>Saved pages</h2>
        <p>Every page fetched is kept exactly as it arrived. These are the originals every
        number above was read from.</p>
      </header>
      <div class="card">
        <div class="card-head"><h3>The capture ledger</h3><span class="eyebrow">one row per page fetched</span></div>
        <div class="brief">
          <p><b>the fingerprint</b> is a checksum of the file. Two fetches with the same
          fingerprint are byte-for-byte identical.</p>
          <p><b>why that is here</b> It tells a genuinely quiet market — nothing moves at 3am
          — apart from a feed that has got stuck serving an old copy.</p>
        </div>
        <div class="card-body flush scroll tall"><table id="raws"></table></div>
      </div>
    </section>

    <section id="glossary">
      <header>
        <h2>Glossary</h2>
        <p>Every term used above, in plain words — with the phrase a sportsbook would use.</p>
      </header>
      <div class="card"><div class="card-body"><div class="gloss" id="glossary-list"></div></div></div>
    </section>

    <section id="schema">
      <header>
        <h2>Field reference</h2>
        <p>The technical layer: exactly what is stored for one price. Useful if you are
        querying the database directly.</p>
      </header>
      <div class="card">
        <div class="card-head"><h3>One stored price, column by column</h3><span class="eyebrow">the <code>quote</code> table</span></div>
        <div class="brief">
          <p><b>this is the raw form</b> Everything above is this, translated. The plain
          sentences on this page are built from <em>market</em>, <em>period</em>,
          <em>selection</em>, <em>line</em> and <em>side</em> read together.</p>
        </div>
        <div class="card-body flush scroll"><table id="schema-table"></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>The only values allowed</h3><span class="eyebrow">anything else is refused, not guessed at</span></div>
        <div class="card-body" id="vocab"></div>
      </div>
    </section>
  </main>
</div>
"""


JS = r"""
'use strict';

const DATA = JSON.parse(document.getElementById('report-data').textContent);
const S = DATA.strings;
const Q = DATA.quotes;              // { columns, rows } — rows across several runs
const COL = {};
Q.columns.forEach((name, i) => { COL[name] = i; });
const NET_ODDS = 'net_decimal_odds';

const el = (id) => document.getElementById(id);
const txt = (v) => (v === null || v === undefined ? '' : String(v));
const str = (i) => (i === null || i === undefined || i < 0 ? null : S[i]);

/* ── plain language ──────────────────────────────────────────────────────── */

// Every enum the pipeline stores, paired with the words a person would use and the
// phrase a sportsbook prints.  The technical value stays reachable as a tooltip.
//
// Venue names come from the payload rather than from a literal here.  A hard-coded
// map of three keys was right when there were three sources and became wrong the
// moment there were ten: the other seven rendered as raw slugs in every table on
// the page, and nothing failed.
const SOURCE_INFO = new Map((DATA.sources || []).map((s) => [s.key, s]));
const book = (key) => (SOURCE_INFO.get(key) || {}).label || key;

// What kind of counterparty each venue is.  Not decoration: it decides whether the
// quoted price is the price you are paid, whether there is a real amount behind it,
// and what happens to the stake if the game is called off.
const venueKind = (key) => (SOURCE_INFO.get(key) || {}).kind || 'sportsbook';
const commissionOf = (key) => (SOURCE_INFO.get(key) || {}).commission || '';
const charges = (key) => Boolean(commissionOf(key));

/*  The price after the venue's cut — what you are actually paid.
 *
 *  Computed in the pipeline and carried on the row, not recomputed here: the
 *  arbitrage engine ranks and prices every comparison net, and a page that
 *  showed gross contradicted it on exactly the rows the commission model exists
 *  for. Falls back to the quoted number for a venue that charges nothing, which
 *  is every sportsbook. */
/*  American odds for the price actually paid.  The stored value is the venue's
 *  quoted number, so on a commission venue it disagrees with the decimal column
 *  beside it — and the two columns sorted differently for 3,754 of 4,693 such
 *  rows. */
function americanOf(r) {
  const net = netOdds(r);
  if (!charges(str(r[COL.source]))) return r[COL.american_odds];
  return net >= 2 ? Math.round((net - 1) * 100) : -Math.round(100 / (net - 1));
}

/*  Which of two rows for one venue and one bet to show.  A price that is not
 *  taking bets never displaces one that is, however good it looks; among equals
 *  the better net price wins, matching the detector. */
function _betterPrice(candidate, held) {
  const liveNow = str(candidate[COL.status]) === 'active';
  const heldLive = str(held[COL.status]) === 'active';
  if (liveNow !== heldLive) return liveNow;
  return netOdds(candidate) > netOdds(held);
}

function netOdds(r) {
  const net = COL[NET_ODDS] === undefined ? null : r[COL[NET_ODDS]];
  return net === null || net === undefined ? r[COL.decimal_odds] : net;
}

// The stored vocabulary is sport-neutral; a baseball-only database named the same
// three markets run_line / total_runs / team_total_runs.  Normalising here means
// every branch below reads one set of names, and an older page still describes its
// bets correctly instead of taking the wrong branch and claiming a handicap is a
// total — a wrong sentence a reader has no way to catch.
const MARKET_ALIASES = { run_line: 'spread', total_runs: 'total', team_total_runs: 'team_total' };
const mkt = (key) => MARKET_ALIASES[key] || key;

// What a total counts is a property of the sport, so the wording is looked up
// rather than written in: "runs" for baseball, "goals" for hockey and soccer,
// "points" for basketball and football, "games" for tennis.  The facts come from
// the payload, which reads them out of the same settlement table the pipeline
// uses, so the page cannot describe a bet in units it does not settle in.
const SPORT_FACTS = DATA.sport_facts || {};
function unitFor(sport, period) {
  const facts = SPORT_FACTS[sport];
  if (!facts) return 'points';
  const window = facts.periods && facts.periods[period];
  return (window && window.unit) || facts.unit || 'points';
}
// How many priced outcomes a complete moneyline has in this window: three where
// a draw is a real, backable outcome, two otherwise.  Read from the same
// settlement table the pipeline settles on.
function moneylineSides(sport, period) {
  const facts = SPORT_FACTS[sport];
  const window = facts && facts.periods && facts.periods[period];
  return window && window.draw_is_priced ? 3 : 2;
}

// Whether a book's own version of one market is complete enough to sum.
//
// ``src/validation.py`` will only sum a market when nothing is missing and
// every row is active — its comment says why: "two legs of a three-way market
// sum to less than 1.0 on perfectly good prices, and reporting that as 'the
// book prices itself to lose' blames the prices for a missing row."  The page
// re-implemented the sum with only a >= 2 row count, so a suspended draw leg or
// an exchange with no resting draw offer turned a healthy 3-way into a "2-way"
// summing to 0.757 — rendered as "impossible prices: 1, a book pricing itself
// to lose" in the same strip where "problems found" said 0, and as
// "smarkets keeps -23.3%" on the bet panel.  The committed capture holds 221
// three-way moneylines; one suspended leg on any of them flips the headline.
function sumsToAMargin(rows) {
  if (rows.length < 2) return false;
  const first = rows[0];
  if (str(first[COL.market]) !== 'moneyline') return rows.length >= 2;
  return rows.length >= moneylineSides(str(first[COL.sport]), str(first[COL.period]));
}

// "a 2-run win", not "a 2-runs win".
const one = (unit) => (unit.endsWith('s') ? unit.slice(0, -1) : unit);
const sportLabel = (sport) => (SPORT_FACTS[sport] ? SPORT_FACTS[sport].label : label(sport));
const leagueLabel = (key) => (DATA.league_names && DATA.league_names[key]) || txt(key);

const MARKETS = {
  moneyline:  { plain: 'Who wins',               term: 'moneyline' },
  spread:     { plain: 'Winner with a handicap', term: 'handicap / spread' },
  total:      { plain: 'Combined total',         term: 'total, over/under' },
  team_total: { plain: "One side's total",       term: 'team total' },
};
const PERIODS = DATA.period_labels || {};
function marketOf(key, sport) {
  const base = MARKETS[mkt(key)];
  if (!base) return { plain: txt(key).replace(/_/g, ' '), term: key };
  if (!sport) return base;
  // Named in the sport's own units where they are known, because "Total goals"
  // and "Total runs" are what the books themselves print.
  const unit = unitFor(sport, 'full_game');
  if (mkt(key) === 'total') return { plain: 'Total ' + unit, term: base.term };
  if (mkt(key) === 'team_total') return { plain: "One side's " + unit, term: base.term };
  return base;
}
const periodOf = (key) => PERIODS[key] || { plain: txt(key).replace(/_/g, ' '), term: key };
const label = (v) => txt(v).replace(/_/g, ' ');

// Participants are addressed by the identity the pipeline resolved — "MLB-CIN",
// "TENNIS-humbertugo" — not by the book's spelling, because two books spell the
// same club three ways and the page must not turn that into two competitors.
function participant(key) {
  return (DATA.participants && DATA.participants[key]) || null;
}
function nick(key) {
  const known = participant(key);
  return known ? known.short : txt(key);
}
function abbr(key) {
  const known = participant(key);
  return known ? known.abbr : txt(key);
}
function fullName(key) {
  const known = participant(key);
  return known ? known.name : txt(key);
}

const isHalf = (n) => Math.abs(Math.abs(n % 1) - 0.5) < 1e-9;

// A quarter line (.25 / .75) is neither of the other two: the stake is split
// across the two neighbouring half-lines, so one half can win while the other
// pushes. Describing it as "whole" — which is what a two-way split did — asserts
// a refund on an event that cannot occur ("a 0.25-goal win refunds") and, worse,
// hides a payout: on away +0.25 a draw is a HALF_WIN, and the reader was told
// the only outcomes were an away win and an impossible refund.
// ``src/arb.py`` models this exactly (LINE_QUARTER, HALF_WIN, HALF_LOSE); the
// page and the detector were describing the same stored row differently.
const isQuarter = (n) => Math.abs(Math.abs(n % 1) - 0.25) < 1e-9
  || Math.abs(Math.abs(n % 1) - 0.75) < 1e-9;

// The two half-lines a quarter line is split between, lower first.
const quarterHalves = (n) => {
  const lower = Math.floor(n * 2) / 2;
  return [lower, lower + 0.5];
};

/** One bet, as a sentence: "Reds win by 2 or more". */
function describeBet(bet, homeRaw, awayRaw) {
  const home = nick(homeRaw), away = nick(awayRaw);
  const market = mkt(bet.market);
  const units = unitFor(bet.sport, bet.period);
  const unit = one(units);
  const picked = bet.selection === 'home' ? home : bet.selection === 'away' ? away : null;
  const PART = {
    first_5_innings: [' after 5 innings', ' in the first 5 innings'],
    first_1_inning:  [' after 1 inning', ' in the first inning'],
    first_half:      [' at half time', ' in the first half'],
    regulation:      [' in regulation', ' in regulation'],
  };
  const [after, during] = PART[bet.period] || ['', ''];
  let text;

  if (market === 'moneyline') {
    // A draw is only ever offered where the scoring window can really end level —
    // soccer, and hockey over regulation time only — so it is named as the result
    // it is rather than as a refund.
    text = bet.selection === 'draw'
      ? (after ? `Scores level${after}` : 'Scores level — a draw')
      : (after ? `${picked} ahead${after}` : `${picked} win`);

  } else if (market === 'spread') {
    // Half numbers cannot be tied with, so they always settle one way or the other.
    // Whole numbers can land exactly on the handicap, which refunds the stake — say so,
    // because "lose by fewer than 1" is not a thing that can happen.
    const size = Math.abs(bet.line);
    const margin = (n) => (n === 1 ? 'lose by 1' : `lose by ${n} or fewer`);
    if (isQuarter(bet.line)) {
      // Half the stake on each neighbouring line, so the middle outcome pays
      // half and the stake is only ever half at risk there.
      const [lo, hi] = quarterHalves(Math.abs(bet.line));
      text = bet.line < 0
        ? `${picked} win by ${Math.ceil(hi)} or more; a ${Math.ceil(lo)}-${unit} win pays half`
        : `${picked} win, or lose by fewer than ${lo}; a ${lo}-${unit} loss pays half`;
    } else if (bet.line < 0) {
      text = isHalf(size)
        ? `${picked} win by ${Math.ceil(size)} or more`
        : `${picked} win by ${size + 1} or more (a ${size}-${unit} win refunds)`;
    } else {
      const room = isHalf(size) ? Math.floor(size) : size - 1;
      const core = room < 1 ? `${picked} win` : `${picked} win, or ${margin(room)}`;
      text = isHalf(size) ? core : `${core} (a ${size}-${unit} loss refunds)`;
    }
    text += during;

  } else {
    const scorer = market === 'team_total'
      ? (bet.side === 'home' ? home : away)
      : 'Both sides together';
    const n = bet.line;
    if (isQuarter(n)) {
      const [lo, hi] = quarterHalves(n);
      text = bet.selection === 'over'
        ? `${scorer} score ${Math.ceil(hi)} ${units} or more; exactly ${Math.ceil(lo)} pays half`
        : `${scorer} score ${Math.floor(lo)} ${units} or fewer; exactly ${Math.ceil(lo)} pays half`;
    } else if (bet.selection === 'over') {
      text = isHalf(n) ? `${scorer} score ${Math.ceil(n)} ${units} or more`
        : `${scorer} score ${n + 1} ${units} or more (exactly ${n} refunds)`;
    } else {
      text = isHalf(n) ? `${scorer} score ${Math.floor(n)} ${units} or fewer`
        : `${scorer} score ${n - 1} ${units} or fewer (exactly ${n} refunds)`;
    }
    text += during;
  }

  return text + (bet.is_alternate ? ' · extra line' : '');
}

/** The same bet in the notation a sportsbook would print, for the tooltip. */
function notation(bet, homeRaw, awayRaw) {
  const side = bet.selection === 'home' ? abbr(homeRaw)
    : bet.selection === 'away' ? abbr(awayRaw) : bet.selection;
  const number = bet.line === null || bet.line === undefined ? ''
    // Two decimals where the line needs them: ``toFixed(1)`` printed -0.25 as
    // "-0.3" and 3.25 as "3.3", naming a market no book offers.
    : ' ' + (mkt(bet.market) === 'spread' && bet.line > 0 ? '+' : '')
      + (isQuarter(bet.line) ? (+bet.line).toFixed(2) : (+bet.line).toFixed(1));
  const which = bet.side ? ` ${abbr(bet.side === 'home' ? homeRaw : awayRaw)}` : '';
  return `${marketOf(bet.market, bet.sport).term} · ${periodOf(bet.period).term}${which} · ${side}${number}`;
}

/** A row from the quotes table, in the shape describeBet() wants. */
const betOf = (r) => ({
  sport: str(r[COL.sport]), league: str(r[COL.league]),
  market: str(r[COL.market]), period: str(r[COL.period]), selection: str(r[COL.selection]),
  side: str(r[COL.side]), line: r[COL.line], is_alternate: !!r[COL.is_alternate],
});
/** The two participant keys of a row — what a description must be built from. */
const sidesOf = (r) => [str(r[COL.home_participant]), str(r[COL.away_participant])];

/* ── formatting ──────────────────────────────────────────────────────────── */

function fmtOdds(d) { return d.toFixed(2); }
function fmtAmerican(a) { return (a > 0 ? '+' : '−') + Math.abs(a); }
function fmtReturn(d) { return '$' + (d * 100).toFixed(0); }
function fmtNumber(v, market) {
  if (v === null || v === undefined) return '';
  return (mkt(market) === 'spread' && v > 0 ? '+' : '') + (+v).toFixed(1);
}
function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}
function fmtClock(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
function fmtTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
}
function ago(iso) {
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 90) return Math.max(0, Math.round(secs)) + ' seconds ago';
  if (secs < 5400) return Math.round(secs / 60) + ' minutes ago';
  if (secs < 172800) return Math.round(secs / 3600) + ' hours ago';
  return Math.round(secs / 86400) + ' days ago';
}
function skipNote(reason) {
  // Longest matching prefix wins, so a specific reason beats its family.
  let best = null;
  for (const [prefix, note] of DATA.skip_notes) {
    if (reason.startsWith(prefix) && (!best || prefix.length > best[0].length)) best = [prefix, note];
  }
  return best ? best[1] : 'Not one of the four kinds of game bet collected here.';
}

/* ── table helper ────────────────────────────────────────────────────────── */

function table(node, columns, rows, opts = {}) {
  if (!rows.length) {
    node.innerHTML = '';
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = opts.empty || 'Nothing to show.';
    node.replaceWith(box);
    box.id = node.id;
    box.dataset.wasTable = '1';
    return;
  }
  if (node.dataset.wasTable) {
    const fresh = document.createElement('table');
    fresh.id = node.id;
    if (opts.className) fresh.className = opts.className;
    node.replaceWith(fresh);
    node = fresh;
  }
  if (opts.className) node.className = opts.className;

  // A wide table reads as labelled bands rather than a dozen equal columns. The bands
  // are derived from the columns themselves, so their spans cannot disagree with what
  // is actually rendered — the failure mode of a hand-written colspan row.
  const bands = [];
  for (const c of columns) {
    const name = c.band || '';
    const last = bands[bands.length - 1];
    if (last && last.label === name) last.span += 1;
    else bands.push({ label: name, span: 1, at: bands.reduce((a, b) => a + b.span, 0) });
  }
  const banded = bands.length > 1 && bands.some((b) => b.label);
  const seps = new Set(banded ? bands.map((b) => b.at).filter((i) => i > 0) : []);
  const bandRow = banded
    ? `<tr class="grouped">${bands.map((b) =>
        `<th colspan="${b.span}" class="${seps.has(b.at) ? 'gsep' : ''}">${escapeHtml(b.label)}</th>`).join('')}</tr>`
    : '';

  const head = columns.map((c, i) =>
    `<th class="${[c.num ? 'num' : '', seps.has(i) ? 'gsep' : ''].filter(Boolean).join(' ')}"${
      c.hint ? ` title="${escapeHtml(c.hint)}"` : ''}>${c.label}</th>`
  ).join('');
  const body = rows.map((r) => '<tr' + (r._attrs || '') + '>' + columns.map((c, i) => {
    const cell = c.cell(r);
    const cls = [c.num ? 'num' : '', seps.has(i) ? 'gsep' : '', cell.cls || ''].filter(Boolean).join(' ');
    return `<td class="${cls}"${cell.title ? ` title="${escapeHtml(cell.title)}"` : ''}>${
      cell.html !== undefined ? cell.html : escapeHtml(cell.text)}</td>`;
  }).join('') + '</tr>').join('');
  node.innerHTML = `<thead>${bandRow}<tr>${head}</tr></thead><tbody>${body}</tbody>`;

  // opts.go turns each row into a link to whatever that row is about. Wired here
  // rather than in every caller, so a table that drills down cannot forget the
  // keyboard path or the class that makes it look clickable.
  if (opts.go) {
    const trs = node.querySelectorAll('tbody tr');
    (trs && trs.length ? [...trs] : []).forEach((tr, i) => {
      const target = opts.go(rows[i]);
      if (!target) return;
      tr.classList.add('go');
      tr.tabIndex = 0;
      tr.addEventListener('click', () => go(target));
      tr.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(target); }
      });
    });
  }
  return node;
}
const cell = (text, cls, title) => ({ text: txt(text), cls, title });
const html = (markup, cls, title) => ({ html: markup, cls, title });
function escapeHtml(s) {
  return txt(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ── run selection ───────────────────────────────────────────────────────── */

const runs = DATA.runs;                       // newest first
const runById = new Map(runs.map((r) => [r.id, r]));
let currentRunId = DATA.meta.latest_run_id ?? (runs[0] && runs[0].id);

const rowsByRun = new Map();
for (const row of Q.rows) {
  const id = row[COL.run_id];
  if (!rowsByRun.has(id)) rowsByRun.set(id, []);
  rowsByRun.get(id).push(row);
}

// '' means every sport.  The filter is applied at the one place the rest of the
// page reads its rows from, so no section can forget to honour it and show a
// different sport's numbers under the same heading.
let currentSport = '';
const runRows = () => rowsByRun.get(currentRunId) || [];
const currentRows = () =>
  currentSport ? runRows().filter((r) => str(r[COL.sport]) === currentSport) : runRows();

function buildRunPicker() {
  const pick = el('run-pick');
  pick.innerHTML = runs.map((r, i) => {
    const when = fmtClock(r.started_at);
    const flag = r.ok ? '' : '  — problems';
    const kept = rowsByRun.has(r.id) ? '' : '  — prices not in this page';
    return `<option value="${r.id}">${i === 0 ? 'Latest: ' : ''}${when}${flag}${kept}</option>`;
  }).join('');
  pick.value = String(currentRunId);
  pick.addEventListener('change', () => { currentRunId = +pick.value; buildSportPicker(); renderRunScoped(); });
}

/** Sports offered by the run being viewed, marked with whether they are usable. */
function buildSportPicker() {
  const pick = el('sport-pick');
  const run = runById.get(currentRunId) || { sports: [] };
  const present = new Set(runRows().map((r) => str(r[COL.sport])));
  const offered = (run.sports || []).filter((entry) => present.has(entry.sport) || entry.quote_count);
  if (currentSport && !offered.some((entry) => entry.sport === currentSport)) currentSport = '';
  pick.innerHTML = ['<option value="">every sport</option>'].concat(
    offered.map((entry) => {
      const flag = entry.comparable ? ''
        : (entry.meets_bar ? '  — no shared fixture' : '  — one book only');
      return `<option value="${escapeHtml(entry.sport)}">${escapeHtml(sportLabel(entry.sport))}${flag}</option>`;
    })
  ).join('');
  pick.value = currentSport;
  const chosen = offered.find((entry) => entry.sport === currentSport);
  el('sport-meta').textContent = chosen
    ? (chosen.comparable
        ? `${chosen.books.length} books, ${chosen.cross_book_events} shared fixture(s) — comparable`
        : (chosen.meets_bar
            ? `${chosen.books.length} books but no fixture both priced — nothing to compare`
            : 'only one book priced it — nothing to compare it with'))
    : `${offered.filter((e) => e.comparable).length} of ${offered.length} sport(s) comparable across books`;
}

/* ── panels and routing ──────────────────────────────────────────────────────
   Every panel is addressable, so a fixture or a single bet can be linked to and
   the browser's back button works.  Routing goes through location.hash rather
   than history.pushState because pushState throws a SecurityError on file://,
   which is where this page is usually opened from.

   Child panels are not in the rail: they are reached by clicking the thing they
   describe, and they name their parent so the trail can be built and the rail
   can still show which part of the page you are inside. */

const PANELS = {
  overview:  { title: 'Start here' },
  run:       { title: 'This collection' },
  sports:    { title: 'Sports & leagues' },
  sources:   { title: 'Sportsbooks' },
  book:      { title: 'One sportsbook', parent: 'sources' },
  events:    { title: 'Fixtures' },
  fixture:   { title: 'One fixture', parent: 'events' },
  bet:       { title: 'One bet', parent: 'events' },
  odds:      { title: 'All prices' },
  movement:  { title: 'Price changes' },
  quality:   { title: 'Checks' },
  raw:       { title: 'Saved pages' },
  glossary:  { title: 'Glossary' },
  schema:    { title: 'Field reference' },
};

let here = { panel: 'overview', arg: null };

/** Navigate. Everything else happens in the hashchange handler. */
function go(hash) {
  if (typeof location === 'undefined') return;
  location.hash = hash;
}
const href = (panel, arg) => '#' + panel + (arg === undefined || arg === null ? '' : '/' + encodeURIComponent(arg));

/** One bet's identity, which is what a drill-down link carries.
 *
 *  Deliberately not the source: the interesting question about a bet is what
 *  every book is charging for it, so the key is the contract and the books are
 *  what the panel compares. */
/*  ``is_alternate`` is deliberately NOT part of this key, because it is not part
 *  of the detector's either (see src/arb.py): a book may reach the same number
 *  through its main market or an extra one, and that is the same bet.  The flag
 *  is also not reliably set — five of the ten adapters never mark an alternate at
 *  all, and the two Kambi tenants tag *different* offers as the main line for one
 *  betOffer id.  Including it split 693 selections the detector compares into
 *  "1 venue offering it — nothing to compare". */
const betKeyOf = (r) => [
  str(r[COL.event_key]), str(r[COL.market]), str(r[COL.period]), str(r[COL.side]) || '',
  r[COL.line] === null || r[COL.line] === undefined ? '' : r[COL.line],
  str(r[COL.selection]), '0',
].join('~');

function parseBetKey(key) {
  const [event, market, period, side, line, selection, alt] = txt(key).split('~');
  return {
    event, market, period, selection,
    side: side || null,
    line: line === '' || line === undefined ? null : +line,
    is_alternate: alt === '1',
  };
}

function panelOf(name) { return PANELS[name] ? name : 'overview'; }

function applyRoute() {
  const raw = (typeof location === 'undefined' ? '' : (location.hash || '')).replace(/^#/, '');
  const cut = raw.indexOf('/');
  const panel = panelOf(cut < 0 ? raw : raw.slice(0, cut));
  const arg = cut < 0 ? null : decodeURIComponent(raw.slice(cut + 1));
  here = { panel, arg };

  // #events/<sport> narrows the whole page to one sport, so a sport is linkable
  // rather than only reachable through a sidebar control.
  if (panel === 'events' && arg !== null && arg !== currentSport) {
    currentSport = arg === 'all' ? '' : arg;
    selectedEvent = null;
    buildSportPicker();
    renderRunScoped();
  }

  // Detail panels re-render for the thing being asked for. The list panels are
  // already current: they are rebuilt whenever the run or sport changes.
  if (panel === 'book') renderBook(arg);
  if (panel === 'fixture') selectEvent(arg);
  if (panel === 'bet') renderBet(arg);

  for (const id of Object.keys(PANELS)) el(id).classList.toggle('on', id === panel);
  const inRail = PANELS[panel].parent || panel;
  for (const link of navLinks()) {
    link.setAttribute('aria-current', String(link.getAttribute('href') === '#' + inRail));
  }
  renderCrumbs();
  if (typeof window !== 'undefined' && typeof window.scrollTo === 'function') window.scrollTo(0, 0);
}

function navLinks() {
  const found = document.querySelectorAll('.nav a');
  return found && found.length ? [...found] : [];
}

/** The trail: where you are, and every level you can step back to. */
function renderCrumbs() {
  const { panel, arg } = here;
  const trail = [];
  const parent = PANELS[panel].parent;
  if (parent) trail.push([PANELS[parent].title, '#' + parent]);

  if (panel === 'book') {
    trail.push([book(arg), null]);
  } else if (panel === 'fixture') {
    trail.push([fixtureLabel(arg), null]);
  } else if (panel === 'bet') {
    const spec = parseBetKey(arg);
    trail.push([fixtureLabel(spec.event), href('fixture', spec.event)]);
    trail.push([betSentence(spec), null]);
  } else {
    trail.push([PANELS[panel].title, null]);
  }

  el('crumbs').innerHTML = trail.map(([text, to], i) =>
    (i ? '<i>&rsaquo;</i>' : '') + (to
      ? `<a href="${escapeHtml(to)}">${escapeHtml(text)}</a>`
      : `<b>${escapeHtml(text)}</b>`)).join('');
}

/** "Guardians at Reds" for a fixture key, without needing its rows loaded. */
function fixtureLabel(key) {
  const row = (Q.rows || []).find((r) => str(r[COL.event_key]) === key);
  if (!row) return txt(key) || 'Fixture';
  const [home, away] = sidesOf(row);
  return `${nick(away)} at ${nick(home)}`;
}

/** The plain sentence for a bet, from its key alone. */
function betSentence(spec) {
  const row = (Q.rows || []).find((r) => str(r[COL.event_key]) === spec.event);
  if (!row) return 'This bet';
  const [home, away] = sidesOf(row);
  return describeBet(Object.assign({ sport: str(row[COL.sport]) }, spec), home, away);
}

/* ── overview ────────────────────────────────────────────────────────────── */

function marketGroups(rows) {
  // Group by the sportsbook's own id for the bet, exactly as the pipeline does:
  // grouping on a row's own number would tear a handicap bet in half.
  const groups = new Map();
  for (const r of rows) {
    const key = [str(r[COL.source]), str(r[COL.source_event_id]), str(r[COL.source_market_id]) ||
      [str(r[COL.market]), str(r[COL.period]), str(r[COL.side]), r[COL.line], r[COL.is_alternate]].join('|')].join('\x1f');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  return groups;
}

function renderOverview() {
  const run = runById.get(currentRunId);
  const rows = currentRows();
  const hasRows = rowsByRun.has(currentRunId);
  const events = new Set(rows.map((r) => str(r[COL.event_key])));
  const groups = marketGroups(rows);
  const health = run.sources;
  const producing = health.filter((h) => h.quote_count > 0);

  el('lede').textContent = DATA.meta.lede;
  el('brand-sub').textContent = DATA.meta.db_name;
  el('built').innerHTML = `page built ${escapeHtml(fmtClock(DATA.meta.generated_at))}<br>from ${escapeHtml(DATA.meta.db_path)}`;
  el('run-meta').textContent = `${ago(run.started_at)}, took ${
    run.duration_ms !== null ? (run.duration_ms / 1000).toFixed(1) + 's' : 'unknown'}`;

  const usableSports = (run.sports || []).filter((entry) => entry.comparable);
  const singleSports = (run.sports || []).filter((entry) => !entry.comparable);
  el('masthead-pills').innerHTML = [
    `<span class="pill ${run.ok ? 'ok' : 'bad'}"><i></i>${run.ok ? 'all checks passed' : 'checks found problems'}</span>`,
    `<span class="pill ${producing.length >= 2 ? 'flat' : 'bad'}">${producing.length} of ${health.length} venues responded</span>`,
    `<span class="pill ${usableSports.length ? 'ok' : 'bad'}"><i></i>${usableSports.length} sport${
      usableSports.length === 1 ? '' : 's'} comparable across books</span>`,
    singleSports.length
      ? `<span class="pill warn"><i></i>${singleSports.length} sport${
          singleSports.length === 1 ? '' : 's'} not comparable</span>`
      : '',
    currentSport ? `<span class="pill accent">showing ${escapeHtml(sportLabel(currentSport))}</span>` : '',
    `<span class="pill flat">${runs.length} collection${runs.length === 1 ? '' : 's'} so far</span>`,
  ].filter(Boolean).join('');

  const notice = el('run-notice');
  notice.classList.toggle('on', !hasRows);
  notice.innerHTML = hasRows ? '' :
    `<b>&#9432;</b><span>This collection's ${run.quote_count.toLocaleString()} prices are not
     included in this page — only the ${DATA.meta.runs_with_rows} most recent collection(s) carry
     prices, to keep the file small. The totals below come from its own summary, so the tables
     further down are empty on purpose. Rebuild with
     <code>--quote-runs ${DATA.meta.runs_recorded}</code> to include it.</span>`;

  const scoped = currentSport ? ` · ${sportLabel(currentSport)} only` : '';
  const stats = [
    ['prices collected', (hasRows ? rows.length : run.quote_count).toLocaleString(),
      'one per bet you could place' + scoped],
    ['fixtures', hasRows ? events.size : run.event_count, `playing ${DATA.meta.slate_dates}`],
    ['separate bets', hasRows ? groups.size.toLocaleString() : '—', 'each with every side priced'],
    ['sports', (run.sports || []).length,
      usableSports.length
        ? `${usableSports.length} comparable: ${usableSports.map((e) => sportLabel(e.sport)).join(', ')}`
        : 'none comparable across books',
      usableSports.length ? '' : 'is-warn'],
    ['venues', producing.length, producing.map((h) => book(h.key)).join(', ')],
    ['problems', run.error_count, run.error_count ? 'see Checks' : 'nothing flagged', run.error_count ? 'is-bad' : 'is-good'],
    ['worth a look', run.warning_count, run.warning_count ? 'see Checks' : 'nothing flagged', run.warning_count ? 'is-warn' : 'is-good'],
  ];
  el('stat-strip').innerHTML = stats.map(([name, value, sub, cls]) =>
    `<div class="stat ${cls || ''}"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b><small>${escapeHtml(sub)}</small></div>`
  ).join('');

  const requests = health.reduce((a, h) => a + h.request_count, 0);
  const bytes = health.reduce((a, h) => a + h.raw_bytes, 0);
  const skipped = health.reduce((a, h) => a + h.skipped_count, 0);
  const rejected = health.reduce((a, h) => a + h.rejection_count, 0);
  const parsed = health.reduce((a, h) => a + h.quote_count, 0);
  // On a scoped run the --sport/--league filter sits between "read" and
  // "checked": validation runs on what the filter kept, so "checked" must count
  // the kept rows, and the dropped ones need their own box — without it 2,953
  // rows vanished between adjacent numbers and "checked" claimed a count the
  // checks never saw.
  const excluded = run.excluded_count || 0;
  const steps = [
    ['1. asked', requests, `${health.length} venues, ${fmtBytes(bytes)} downloaded`],
    ['2. saved', run.raw_count, 'originals kept on disk'],
    ['3. read', parsed + rejected, `${skipped.toLocaleString()} other bets seen and skipped`],
    ...(excluded ? [['4. in scope', parsed - excluded,
      `${excluded.toLocaleString()} dropped by the run's --sport/--league scope`]] : []),
    [`${excluded ? 5 : 4}. checked`, parsed - excluded,
      `${run.error_count} problem${run.error_count === 1 ? '' : 's'}, ${run.warning_count} worth a look`],
    [`${excluded ? 6 : 5}. stored`, run.quote_count, 'no bet stored twice'],
  ];
  el('flow').innerHTML = steps.map((s, i) =>
    (i ? '<div class="flow-arrow">&rarr;</div>' : '') +
    `<div class="flow-step"><span>${escapeHtml(s[0])}</span><b>${Number(s[1]).toLocaleString()}</b><small>${escapeHtml(s[2])}</small></div>`
  ).join('');

  const sources = [...new Set(rows.map((r) => str(r[COL.source])))].sort();
  const combos = new Map();
  for (const r of rows) {
    // Keyed on sport as well: a baseball total and a soccer total are different
    // contracts, and one row counting both would be a number of nothing.
    const key = [str(r[COL.sport]), str(r[COL.market]), str(r[COL.period])].join('\x1f');
    if (!combos.has(key)) combos.set(key, new Map());
    const per = combos.get(key);
    const src = str(r[COL.source]);
    per.set(src, (per.get(src) || 0) + 1);
  }
  const matrixRows = [...combos.entries()].map(([key, per]) => {
    const [sport, market, period] = key.split('\x1f');
    return { sport, market, period, per, total: [...per.values()].reduce((a, b) => a + b, 0) };
  }).sort((a, b) => (a.sport < b.sport ? -1 : a.sport > b.sport ? 1 : b.total - a.total));
  table(el('matrix'), [
    { band: 'what the bet is', label: 'Sport', cell: (r) => cell(sportLabel(r.sport)) },
    { band: 'what the bet is', label: 'Kind of bet',
      cell: (r) => cell(marketOf(r.market, r.sport).plain, '', marketOf(r.market, r.sport).term) },
    { band: 'what the bet is', label: 'Part of the game', cell: (r) => cell(periodOf(r.period).plain, 'dim') },
    ...sources.map((s) => ({
      band: 'prices found', label: book(s), num: true,
      cell: (r) => { const v = r.per.get(s) || 0; return cell(v || '—', v ? '' : 'dim'); },
    })),
    { band: 'prices found', label: 'All books', num: true, cell: (r) => cell(r.total) },
  ], matrixRows, { empty: 'No prices stored for this collection.' });
}

/* ── sports & leagues ────────────────────────────────────────────────────── */

/** The coverage grid: which sports and leagues each book actually priced.
 *
 * Deliberately NOT filtered by the sport picker.  This is the section that says
 * what exists, so hiding the other sports here would remove the only place a
 * reader could learn that a sport was collected but is unusable.  The chosen
 * sport is highlighted instead.
 */
function renderSports() {
  const run = runById.get(currentRunId);
  const entries = run.sports || [];
  const books = [...new Set(entries.flatMap((e) => Object.keys(e.per_source || {})))].sort();
  const usable = entries.filter((e) => e.comparable);
  el('nav-sports').textContent = `${usable.length}/${entries.length}`;
  el('sports-bar-note').textContent =
    `${usable.length} of ${entries.length} sport(s) had ${DATA.meta.min_books}+ books on the same fixture`;

  // Two books and no shared fixture is a different failure from one book, and
  // saying which one it is saves the reader looking for a bug that is not there.
  const verdict = (e) => {
    if (e.comparable) return '<span class="pill ok"><i></i>comparable</span>';
    if (!e.meets_bar) {
      return `<span class="pill bad"><i></i>not comparable — ${e.books.length} book${
        e.books.length === 1 ? '' : 's'}</span>`;
    }
    return '<span class="pill bad"><i></i>not comparable — no shared fixture</span>';
  };

  table(el('sports-grid'), [
    { band: 'sport', label: 'Sport',
      cell: (e) => html(`${escapeHtml(sportLabel(e.sport))}${
        e.sport === currentSport ? ' <span class="pill accent">showing</span>' : ''}`) },
    { band: 'sport', label: 'Usable?',
      hint: `Needs ${DATA.meta.min_books} or more books on the same fixture`,
      cell: (e) => html(verdict(e)) },
    ...books.map((key) => ({
      band: 'prices, per sportsbook', label: book(key), num: true,
      cell: (e) => {
        const n = (e.per_source || {})[key] || 0;
        return cell(n ? n.toLocaleString() : '—', n ? '' : 'dim');
      },
    })),
    { band: 'totals', label: 'Books', num: true, cell: (e) => cell(e.books.length) },
    { band: 'totals', label: 'Fixtures', num: true, cell: (e) => cell(e.event_count) },
    { band: 'totals', label: 'Priced by 2+ books', num: true,
      hint: 'Fixtures more than one book priced — the ones that can actually be compared',
      cell: (e) => cell(e.cross_book_events, e.cross_book_events ? '' : 'dim') },
    { band: 'totals', label: 'Prices', num: true, cell: (e) => cell(e.quote_count.toLocaleString()) },
  ], entries, { empty: 'No sports were recorded for this collection.',
                // Clicking a sport narrows the whole page to it, which is otherwise a
                // sidebar control a reader has no reason to go looking for.
                go: (e) => (e.quote_count ? href('events', e.sport) : null) });

  const leagueRows = entries.flatMap((e) =>
    (e.leagues || []).map((l) => Object.assign({}, l, { sport: e.sport })));
  table(el('leagues-grid'), [
    { band: 'competition', label: 'Sport', cell: (r) => cell(sportLabel(r.sport), 'dim') },
    { band: 'competition', label: 'League', cell: (r) => cell(leagueLabel(r.league)) },
    { band: 'competition', label: 'Key', cell: (r) => cell(r.league, 'mono dim') },
    ...books.map((key) => ({
      band: 'prices, per sportsbook', label: book(key), num: true,
      cell: (r) => {
        const n = (r.per_source || {})[key] || 0;
        return cell(n ? n.toLocaleString() : '—', n ? '' : 'dim');
      },
    })),
    { band: 'totals', label: 'Books', num: true,
      cell: (r) => cell(r.books.length, r.books.length >= DATA.meta.min_books ? '' : 'dim') },
    { band: 'totals', label: 'Fixtures', num: true, cell: (r) => cell(r.event_count) },
    { band: 'totals', label: 'Prices', num: true, cell: (r) => cell(r.quote_count.toLocaleString()) },
  ], leagueRows, { empty: 'No leagues were recorded for this collection.' });

  table(el('sports-gaps'), [
    { label: 'Sportsbook', cell: (g) => cell(book(g.source)) },
    { label: 'Sport', cell: (g) => cell(sportLabel(g.sport), 'dim') },
    { label: 'League asked for', cell: (g) => cell(leagueLabel(g.league)) },
    { label: 'Key', cell: (g) => cell(g.league, 'mono dim') },
    { label: 'What came back', cell: (g) => html('<span class="pill warn"><i></i>nothing</span>') },
  ], run.league_gaps || [], {
    empty: 'Every league these books were configured to collect returned at least one price.',
    go: (g) => href('book', g.source),
  });
}

/* ── venues ──────────────────────────────────────────────────────────────── */

function renderSources() {
  const run = runById.get(currentRunId);
  const byKey = new Map(run.sources.map((h) => [h.key, h]));
  el('nav-sources').textContent = run.sources.filter((h) => h.ok).length + '/' + run.sources.length;

  if (!run.sources.length) {
    // A silently blank section reads as "no venues", which is a different claim.
    el('source-cards').innerHTML =
      `<div class="empty">No per-venue detail was recorded for this collection. Its
       ${run.quote_count.toLocaleString()} prices are still stored — see Games and All prices.</div>`;
    el('skips').innerHTML = '';
    return;
  }

  el('source-cards').innerHTML = DATA.sources.map((src) => {
    const h = byKey.get(src.key);
    if (!h) return '';
    // A source that answered but was refused part of what it was asked for is
    // not "responded normally", and said so nowhere on this page: the columns
    // reached the stored payload and stopped there, so a book that had lost
    // most of its leagues rendered byte-identically to a clean one.
    const refused = (h.scopes_refused || []).length;
    const pill = !h.ok
      ? `<span class="pill bad"><i></i>${escapeHtml(label(h.error_kind) || 'failed')}</span>`
      : refused
        ? `<span class="pill warn"><i></i>refused ${refused} of ${
            h.scopes_requested || refused} scopes</span>`
        : '<span class="pill ok"><i></i>responded normally</span>';
    const cells = [
      ['prices published', h.quote_count.toLocaleString()],
      ...(h.stored_count === undefined || h.stored_count === h.quote_count ? []
        : [['reached the database', h.stored_count.toLocaleString()]]),
      ['games', h.event_count],
      ['pages read', h.request_count],
      ['downloaded', fmtBytes(h.raw_bytes)],
      ['took', h.latency_ms === null ? '—' : (h.latency_ms / 1000).toFixed(2) + 's'],
      ['same as last time', `${h.unchanged_payloads} of ${h.request_count}`],
    ];
    if (refused) {
      cells.push(['refused', `${refused} of ${h.scopes_requested || refused} scopes`]);
    }
    // A link rather than a click handler, so the whole card is keyboard-reachable
    // and the browser's own back button returns here.
    // What kind of venue it is, and what it charges, sit next to the name: an
    // exchange price and a book price are not the same number even when they
    // read the same, and the page compared them as though they were.
    const kindPill = `<span class="pill flat">${escapeHtml(src.kind || 'sportsbook')}</span>`;
    const cut = src.commission
      ? `<p class="dim" style="font-size:11.5px">Commission ${escapeHtml(src.commission)}. ${
          escapeHtml(src.settles || '')}</p>`
      : '';
    return `<a class="src" href="${escapeHtml(href('book', src.key))}">
      <div class="src-top"><div><b>${escapeHtml(src.label)}</b><code>${escapeHtml(src.host)}</code></div>${kindPill}${pill}</div>
      <p>${escapeHtml(src.what)}</p>
      ${cut}
      <div class="src-grid">${cells.map(([k, v]) =>
        `<div><span>${escapeHtml(k)}</span><b>${escapeHtml(String(v))}</b></div>`).join('')}</div>
      ${h.error_message ? `<p class="dim" style="font-size:11.5px">${escapeHtml(h.error_message)}</p>` : ''}
    </a>`;
  }).join('');

  const skips = DATA.skipped.filter((s) => s.run_id === currentRunId)
    .sort((a, b) => b.count - a.count);
  table(el('skips'), [
    { label: 'Venue', cell: (r) => cell(book(r.source)) },
    { label: 'What it was', cell: (r) => cell(r.reason.replace(/^criterion:/, '').replace(/^matchup_type:/, '').replace(/_/g, ' ')) },
    { label: 'How many', num: true, cell: (r) => cell(r.count.toLocaleString()) },
    { label: 'Why it was left alone', cell: (r) => cell(skipNote(r.reason), 'dim wrap') },
  ], skips, { empty: detailLoaded(currentRunId)
                ? 'Everything this collection saw was in scope.'
                : "This collection's skipped bets are not in this page — only the newest few collections carry them. Rebuild with more --quote-runs to include it.",
              go: (r) => href('book', r.source) });
}

/* ── games ───────────────────────────────────────────────────────────────── */

let selectedEvent = null;

function eventSummaries(rows) {
  const events = new Map();
  for (const r of rows) {
    const key = str(r[COL.event_key]);
    if (!events.has(key)) {
      const [home, away] = sidesOf(r);
      events.set(key, {
        key,
        // Participant keys, not the books' spellings: every book's rows for this
        // fixture resolve to the same two keys, so the label cannot depend on
        // which book's row happened to be seen first.
        homeRaw: home, awayRaw: away,
        sport: str(r[COL.sport]), league: str(r[COL.league]),
        commence: str(r[COL.commence_time]),
        bySource: new Map(), byMarket: new Map(), rows: [],
      });
    }
    const e = events.get(key);
    e.rows.push(r);
    const src = str(r[COL.source]);
    const mkt = str(r[COL.market]);
    e.bySource.set(src, (e.bySource.get(src) || 0) + 1);
    e.byMarket.set(mkt, (e.byMarket.get(mkt) || 0) + 1);
  }
  return [...events.values()].sort((a, b) =>
    (a.commence < b.commence ? -1 : a.commence > b.commence ? 1 : a.key < b.key ? -1 : 1));
}

function renderEvents() {
  const rows = currentRows();
  const events = eventSummaries(rows);
  el('nav-events').textContent = events.length;

  const mode = el('cov-mode').value;
  const sources = [...new Set(rows.map((r) => str(r[COL.source])))].sort();
  const markets = [...new Set(rows.map((r) => str(r[COL.market])))].sort();
  const keys = mode === 'source' ? sources : markets;
  const heading = (k) => (mode === 'source' ? book(k) : marketOf(k, currentSport).plain);
  const pickMap = (e) => (mode === 'source' ? e.bySource : e.byMarket);
  const peak = Math.max(1, ...events.flatMap((e) => [...pickMap(e).values()]));

  const heat = (v) => {
    if (!v) return html('<span class="dim">—</span>', 'cell zero');
    const alpha = 0.10 + 0.42 * (v / peak);
    return html(`<span class="heat" style="background:color-mix(in srgb, var(--accent) ${(alpha * 100).toFixed(0)}%, transparent)">${v}</span>`, 'cell');
  };

  const node = table(el('coverage'), [
    { band: 'the fixture', label: 'Starts', cell: (e) => cell(fmtClock(e.commence), 'dim') },
    { band: 'the fixture', label: 'Sport', cell: (e) => cell(sportLabel(e.sport), 'dim') },
    { band: 'the fixture', label: 'League',
      hint: 'Recorded for coverage only — never used to join two books together',
      cell: (e) => cell(leagueLabel(e.league), 'dim') },
    { band: 'the fixture', label: 'Fixture',
      cell: (e) => html(`${escapeHtml(nick(e.awayRaw))} <span class="dim">at</span> ${escapeHtml(nick(e.homeRaw))}`) },
    { band: 'the fixture', label: 'Fixture ID',
      hint: "This tool's own name for the fixture, so books can be compared",
      cell: (e) => cell(e.key, 'mono dim') },
    ...keys.map((k) => ({
      band: mode === 'source' ? 'prices, per sportsbook' : 'prices, per kind of bet',
      label: heading(k), num: true, cell: (e) => heat(pickMap(e).get(k) || 0),
    })),
    { band: 'totals', label: 'Books', num: true, cell: (e) => cell(e.bySource.size) },
    { band: 'totals', label: 'Prices', num: true, cell: (e) => cell(e.rows.length) },
  ], events, { className: 'cov', empty: 'No prices stored for this collection.',
               go: (e) => href('fixture', e.key) });

  // The fixture panel is kept populated even while it is off screen, so a link
  // straight into it opens on something rather than on an empty state.
  const keep = events.find((e) => e.key === selectedEvent);
  selectEvent(keep ? keep.key : (events[0] && events[0].key));
}

function selectEvent(key) {
  const events = eventSummaries(currentRows());
  selectedEvent = key;
  const event = events.find((e) => e.key === key);
  if (!event) {
    el('event-title').textContent = 'Pick a fixture';
    el('event-sub').textContent = 'Open Fixtures and click a row.';
    el('event-count').textContent = '';
    table(el('event-detail'), [{ label: '', cell: () => cell('') }], [],
      { empty: 'No fixture selected. Open Fixtures and click a row.' });
    return;
  }

  el('event-title').textContent = `${fullName(event.awayRaw)} at ${fullName(event.homeRaw)}`;
  el('event-sub').textContent = `${sportLabel(event.sport)} · ${leagueLabel(event.league)} · ${
    fmtClock(event.commence)} · ${event.rows.length} prices from ${
    event.bySource.size} venue${event.bySource.size === 1 ? '' : 's'}`;
  el('event-count').textContent = event.key;

  const sources = [...event.bySource.keys()].sort();
  const lines = new Map();
  for (const r of event.rows) {
    const bet = betOf(r);
    const k = [bet.market, bet.period, bet.side || '', bet.line, bet.selection, bet.is_alternate].join('\x1f');
    if (!lines.has(k)) lines.set(k, Object.assign({}, bet, { key: betKeyOf(r), prices: new Map() }));
    lines.get(k).prices.set(str(r[COL.source]), r);
  }

  // An unrecognised market or period sorts last rather than comparing as NaN, which
  // would leave the whole table in whatever order the rows happened to arrive in.
  const ordering = { moneyline: 0, spread: 1, total: 2, team_total: 3 };
  const periodOrder = { full_game: 0, regulation: 1, first_half: 2, first_5_innings: 3, first_1_inning: 4 };
  const rank = (table, key) => (key in table ? table[key] : 99);
  // Totals read best as over/under pairs at each number, so they sort by number first.
  // Handicaps read best as one ladder per team — a book offers a team at both +1 and −1
  // as separate bets, so pairing by the number alone would interleave four rows.
  const within = (a, b) => (mkt(a.market) === 'spread'
    ? a.selection.localeCompare(b.selection) || ((a.line ?? 0) - (b.line ?? 0))
    : ((a.line ?? 0) - (b.line ?? 0)) || a.selection.localeCompare(b.selection));
  const detail = [...lines.values()].sort((a, b) =>
    (rank(ordering, mkt(a.market)) - rank(ordering, mkt(b.market))) ||
    (rank(periodOrder, a.period) - rank(periodOrder, b.period)) ||
    (a.is_alternate - b.is_alternate) || ((a.side || '').localeCompare(b.side || '')) || within(a, b));

  table(el('event-detail'), [
    { band: 'what the bet is', label: 'The bet',
      cell: (r) => cell(describeBet(r, event.homeRaw, event.awayRaw), 'plain',
        notation(r, event.homeRaw, event.awayRaw)) },
    { band: 'what the bet is', label: 'Kind of bet',
      cell: (r) => cell(marketOf(r.market, r.sport).plain, 'dim') },
    { band: 'what the bet is', label: 'Part of game',
      hint: 'Never compared across windows: overtime included and excluded are different bets',
      cell: (r) => cell(periodOf(r.period).plain, 'dim', periodOf(r.period).term) },
    // Marked and ranked on the price after commission, like everywhere else that
    // compares two venues.  On the captured slate 165 of 1,353 cross-book
    // selections — 12% — have a different best venue gross and net, so ranking
    // here on the quoted number put this table in direct disagreement with the
    // bet panel one click away and with the detector.
    ...sources.map((s) => ({
      band: 'what each venue pays per $1', label: book(s), num: true,
      hint: `${book(s)}'s price after its commission, as a decimal payout per $1`,
      cell: (r) => {
        const q = r.prices.get(s);
        if (!q) return html('<span class="dim">—</span>');
        const net = netOdds(q);
        // "Best" means best *takeable*, as the detector means it: a suspended
        // price is showing, not offering.  37 cross-book selections on the live
        // slate had their green marker on a row not accepting bets.
        const live = [...r.prices.values()].filter((p) => str(p[COL.status]) === 'active');
        const best = live.length ? Math.max(...live.map(netOdds)) : null;
        const suspended = str(q[COL.status]) !== 'active';
        const isBest = !suspended && live.length > 1 && net === best;
        const note = `${fmtAmerican(q[COL.american_odds])} · $100 returns ${fmtReturn(net)}${
          charges(s) ? ` · quoted ${fmtOdds(q[COL.decimal_odds])} before commission` : ''}${
          suspended ? ' · not taking bets right now' : ''}`;
        return html(`<span class="${isBest ? 'best' : ''}${suspended ? ' dim' : ''}">${
          fmtOdds(net)}</span>`, '', note);
      },
    })),
    {
      band: 'what each venue pays per $1',
      label: 'Best vs worst', num: true, hint: 'How much more the best price pays than the worst',
      cell: (r) => {
        const vals = [...r.prices.values()]
          .filter((p) => str(p[COL.status]) === 'active').map(netOdds);
        if (vals.length < 2) return html('<span class="dim">—</span>');
        const pct = (Math.max(...vals) / Math.min(...vals) - 1) * 100;
        return cell(pct.toFixed(1) + '%', pct >= 2 ? 'up' : 'dim');
      },
    },
  ], detail, { empty: 'No prices for this fixture.', go: (r) => href('bet', r.key) });
}

/* ── one bet, across every book ───────────────────────────────────────────── */

//: The order a book prints the sides of a market in: home before away, over before
//: under.  Arrival order would put them in whatever order the parser happened to
//: read them, which changes between books for the same bet.
const SELECTION_ORDER = ['home', 'away', 'draw', 'over', 'under'];

/** Every stored row for one bet, newest run first, tagged with its run. */
/*  How far apart two venues' clocks may be and still mean one fixture — the same
 *  per-league number the pipeline clustered with, carried in the payload.
 *
 *  A flat two-hour window was wrong in both directions to guess at.  Tennis
 *  clusters at 14 hours and soccer at 12, so a window of two dropped a venue
 *  from three real cross-source tennis fixtures on the captured slate; MLB
 *  clusters at 90 minutes, where two hours would reach past a doubleheader's
 *  three-hour gap.  The tolerance is only wide in the sports where two
 *  competitors never meet twice in a day, so there is nothing for it to reach. */
const LEAGUE_TOLERANCES = DATA.league_tolerances || {};
const DEFAULT_FIXTURE_MS = 90 * 60 * 1000;
const fixtureWindowMs = (league) => {
  const seconds = LEAGUE_TOLERANCES[txt(league)];
  return seconds === undefined ? DEFAULT_FIXTURE_MS : seconds * 1000;
};

/*  Every row of one bet, oldest run first — but only the rows that are the same
 *  physical fixture as the one being looked at.
 *
 *  The event key alone is not enough for that across runs.  Its doubleheader
 *  ordinal is assigned by rank among the fixtures a *single collection* can see,
 *  and started games are dropped before the run is reconciled, so once game one
 *  is under way game two is the only cluster left and is numbered one — taking
 *  over the bare key that game one carried an hour earlier.  Joining on the key
 *  alone therefore splices game one's early prices onto game two's later ones
 *  and draws them as one bet drifting.
 *
 *  Scheduled start settles it: it is on every row, it is stable for a fixture
 *  across collections, and the two games of a doubleheader are hours apart. */
/*  Which fixture a row belongs to, at the resolution its league clusters at.
 *  Two rows of one fixture land in the same bucket; the two halves of a
 *  doubleheader do not. */
/*  Rounding to a grid splits a fixture whose two listed starts straddle a
 *  boundary — Matchbook revised one tennis start by 15 minutes inside a 14-hour
 *  window and its price change vanished from the panel.  Bucketing against the
 *  *earliest* start seen for that key measures distance instead.
 *
 *  The anchors are gathered in a full pass first and handed in, rather than
 *  accumulated in a module-level map as rows arrive.  A running minimum can drop
 *  mid-loop, so the same start bucketed differently before and after; and a map
 *  that survives between renders made the second render of the same rows
 *  disagree with the first, which the reader triggers just by switching sport. */
function fixtureAnchorsFor(rows) {
  const anchors = new Map();
  for (const r of rows) {
    const when = +new Date(str(r[COL.commence_time]));
    if (!isFinite(when)) continue;
    const key = str(r[COL.event_key]) + '|' + str(r[COL.league]);
    const seen = anchors.get(key);
    if (seen === undefined || when < seen) anchors.set(key, when);
  }
  return anchors;
}

function fixtureBucket(r, anchors) {
  const when = +new Date(str(r[COL.commence_time]));
  if (!isFinite(when)) return 0;
  const key = str(r[COL.event_key]) + '|' + str(r[COL.league]);
  const window = fixtureWindowMs(str(r[COL.league]));
  const base = anchors.get(key);
  return window > 0 && base !== undefined ? Math.floor((when - base) / window) : 0;
}

function betRows(key, reference) {
  const out = [];
  const anchor = reference === undefined || reference === null ? null : +new Date(reference);
  for (const run of runs) {
    for (const row of (rowsByRun.get(run.id) || [])) {
      if (betKeyOf(row) !== key) continue;
      if (anchor !== null) {
        const when = +new Date(str(row[COL.commence_time]));
        const window = fixtureWindowMs(str(row[COL.league]));
        if (isFinite(when) && isFinite(anchor) && Math.abs(when - anchor) > window) {
          continue;
        }
      }
      out.push({ run, row });
    }
  }
  return out;
}

function renderBet(key) {
  const spec = parseBetKey(key || '');
  // The fixture is pinned by whichever collection is being viewed; every other
  // run contributes only rows scheduled for the same start.
  const here = betRows(key || '').filter((m) => m.run.id === currentRunId);
  const anchor = (here[0] || betRows(key || '')[0] || { row: null }).row;
  const all = betRows(key || '', anchor ? str(anchor[COL.commence_time]) : null);
  const current = all.filter((m) => m.run.id === currentRunId);
  // Fall back to the newest collection holding it, so a bet reached from an older
  // run still shows prices rather than an empty panel.
  const shown = current.length ? current : all.filter((m) => m.run.id === (all[0] && all[0].run.id));
  const sample = shown[0] || all[0];

  if (!sample) {
    el('bet-title').textContent = 'Pick a bet';
    el('bet-sub').textContent = 'Open a fixture or All prices and click a row.';
    el('bet-notation').textContent = '';
    el('bet-spread').textContent = '';
    el('bet-books').innerHTML = '<div class="empty">No bet selected.</div>';
    el('bet-runs').textContent = '';
    table(el('bet-sides'), [{ label: '', cell: () => cell('') }], [], { empty: 'No bet selected.' });
    table(el('bet-history'), [{ label: '', cell: () => cell('') }], [], { empty: 'No bet selected.' });
    return;
  }

  const [home, away] = sidesOf(sample.row);
  const sport = str(sample.row[COL.sport]);
  const bet = Object.assign({ sport }, spec);

  el('bet-title').textContent = describeBet(bet, home, away);
  el('bet-sub').textContent = `${nick(away)} at ${nick(home)} · ${sportLabel(sport)} · ${
    marketOf(spec.market, sport).plain} · ${periodOf(spec.period).plain}${
    spec.is_alternate ? ' · an extra line, not the book’s main number' : ''}`;
  el('bet-notation').textContent = `stored as ${notation(bet, home, away)}`;

  // One card per venue, best price first: the whole point of collecting many of
  // them is that the same wager pays differently at each.  Ranked and compared on
  // the price *after commission*, because that is the one you are paid and the one
  // the arbitrage engine acts on — ranking on the quoted number would name an
  // exchange at 2.10 as better than a book at 2.08 when the exchange pays 2.045.
  // Not last-write-wins.  Dropping ``is_alternate`` from the bet key — right,
  // because the detector ignores it too — means a book's main and alternate row
  // at one number now land here together, and the panel must show the one the
  // reported margin was built on.  Takeable first, then better price.
  const byBook = new Map();
  for (const m of shown) {
    const source = str(m.row[COL.source]);
    const held = byBook.get(source);
    if (!held || _betterPrice(m.row, held)) byBook.set(source, m.row);
  }
  // Best *takeable*, matching the fixture table and the detector: a suspended
  // price is showing, not offering.
  const live = [...byBook.values()].filter((r) => str(r[COL.status]) === 'active');
  const prices = live.map(netOdds);
  const best = prices.length ? Math.max(...prices) : null;
  const worst = prices.length ? Math.min(...prices) : null;

  el('bet-spread').textContent = prices.length > 1
    ? `${((best / worst - 1) * 100).toFixed(1)}% more at the best venue than the worst`
    : `${prices.length} venue${prices.length === 1 ? '' : 's'} offering it — nothing to compare`;

  el('bet-books').innerHTML = [...byBook.entries()]
    .sort((a, b) => netOdds(b[1]) - netOdds(a[1]))
    .map(([source, r]) => {
      const open = str(r[COL.status]) === 'active';
      const net = netOdds(r);
      const cut = charges(source);
      const isBest = open && prices.length > 1 && net === best;
      const facts = [
        ['US odds', fmtAmerican(americanOf(r))],
        ['$100 returns', fmtReturn(net)],
        [cut ? 'implied chance' : "book's chance", ((1 / netOdds(r)) * 100).toFixed(1) + '%'],
        ['max bet', r[COL.limit_amount] === null ? '—' : '$' + Math.round(r[COL.limit_amount]).toLocaleString()],
        ['last moved', str(r[COL.last_change_at]) ? fmtClock(str(r[COL.last_change_at])) : '—'],
      ];
      if (cut) {
        facts.splice(1, 0, ['quoted', fmtOdds(r[COL.decimal_odds]) + ' before commission']);
        facts.push(['commission', commissionOf(source)]);
      }
      return `<div class="qcard ${isBest ? 'top' : ''}${open ? '' : ' off'}">
        <div class="who"><b>${escapeHtml(book(source))}</b>${
          isBest ? '<span class="pill ok"><i></i>best</span>' : ''}${
          open ? '' : '<span class="pill warn">paused</span>'}</div>
        <span class="price">${fmtOdds(net)}</span>
        <dl>${facts.map(([k, v]) =>
          `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join('')}</dl>
      </div>`;
    }).join('') || '<div class="empty">No venue offered this bet in this collection.</div>';

  // The rest of the same market: without the other sides, a price says nothing about
  // whether it is generous, and the book's cut cannot be worked out at all.
  // ``is_alternate`` is not in the bet key and must not be in this lookup
  // either: comparing the row's real flag against a key that no longer carries
  // it made 21,232 bets print "only one side of this bet was stored" with both
  // sides sitting in the same file.
  const groupOf = (r) => [str(r[COL.market]), str(r[COL.period]), str(r[COL.side]) || '',
    r[COL.line] === null ? '' : r[COL.line]].join('~');
  const wanted = [spec.market, spec.period, spec.side || '',
    spec.line === null ? '' : spec.line].join('~');
  const runRowsNow = rowsByRun.get(sample.run.id) || [];
  const siblings = new Map();
  for (const r of runRowsNow) {
    if (str(r[COL.event_key]) !== spec.event || groupOf(r) !== wanted) continue;
    const sel = str(r[COL.selection]);
    if (!siblings.has(sel)) siblings.set(sel, { selection: sel, prices: new Map() });
    // A book may reach the same number through its main market and an extra
    // one.  The detector takes the better of the two (src/arb.py), so this must
    // not be last-write-wins — the panel would show the worse price for the leg
    // the reported margin was built on.
    const held = siblings.get(sel).prices.get(str(r[COL.source]));
    if (!held || _betterPrice(r, held)) {
      siblings.get(sel).prices.set(str(r[COL.source]), r);
    }
  }
  const sideSources = [...new Set(runRowsNow.filter((r) => str(r[COL.event_key]) === spec.event
    && groupOf(r) === wanted).map((r) => str(r[COL.source])))].sort();

  table(el('bet-sides'), [
    { band: 'the side', label: 'The bet', cell: (s) => {
        const one = Object.assign({}, bet, { selection: s.selection });
        return cell(describeBet(one, home, away), s.selection === spec.selection ? 'plain' : 'plain dim',
          notation(one, home, away));
      } },
    ...sideSources.map((source) => ({
      band: 'price, and the chance it implies', label: book(source), num: true,
      cell: (s) => {
        const r = s.prices.get(source);
        if (!r) return html('<span class="dim">—</span>');
        // The chance implied by the price *shown*.  Printing the stored
        // probability beside a net price rendered two venues at an identical
        // 42.0% on prices 0.11 apart, and the card asks the reader to sum these
        // to get the venue's cut — which then disagreed with the cut printed
        // above it.
        return cell(`${fmtOdds(netOdds(r))}  (${(100 / netOdds(r)).toFixed(1)}%)`);
      },
    })),
  ], [...siblings.values()].sort((a, b) =>
       SELECTION_ORDER.indexOf(a.selection) - SELECTION_ORDER.indexOf(b.selection)), {
    empty: 'Only one side of this bet was stored, so the book’s cut cannot be worked out.',
    go: (s) => href('bet', [spec.event, spec.market, spec.period, spec.side || '',
      spec.line === null ? '' : spec.line, s.selection, spec.is_alternate ? '1' : '0'].join('~')),
  });

  const totals = sideSources.map((source) => {
    const sides = [...siblings.values()].map((s) => s.prices.get(source)).filter(Boolean);
    // Same rule as the quality strip: a venue's cut can only be read off a
    // market it priced completely.  On two legs of a three-way this printed
    // "keeps -23.3%" — a 23% edge to the bettor — for a missing draw offer.
    if (!sumsToAMargin(sides)) return null;
    const sum = sides.reduce((a, r) => a + 1 / netOdds(r), 0);
    return `${book(source)} keeps ${((sum - 1) * 100).toFixed(1)}%`;
  }).filter(Boolean);

  // How the price has moved, one row per book and one column per collection. This is
  // the same evidence as the movement table, narrowed to the one bet being looked at.
  const embedded = runs.slice().reverse().filter((r) => rowsByRun.has(r.id));
  const history = [...new Set(all.map((m) => str(m.row[COL.source])))].sort().map((source) => {
    const byRun = new Map(all.filter((m) => str(m.row[COL.source]) === source)
      .map((m) => [m.run.id, m.row]));
    const series = embedded.map((r) => byRun.get(r.id)).filter(Boolean)
      .map(netOdds);
    return { source, byRun, series };
  });

  el('bet-runs').textContent = totals.length
    ? totals.join(' · ')
    : `seen in ${new Set(all.map((m) => m.run.id)).size} of ${embedded.length} collections`;

  table(el('bet-history'), [
    { band: 'sportsbook', label: 'Sportsbook', cell: (h) => cell(book(h.source)) },
    ...embedded.map((run) => ({
      band: 'price at each collection, oldest first',
      label: fmtTime(run.started_at), num: true,
      hint: fmtClock(run.started_at) + (run.ok ? '' : ' — checks found problems'),
      cell: (h) => {
        const r = h.byRun.get(run.id);
        if (!r) return html('<span class="dim">—</span>');
        const cls = run.id === currentRunId ? '' : 'dim';
        return cell(fmtOdds(netOdds(r)), cls);
      },
    })),
    { band: 'over the whole period', label: 'Shape', cell: (h) => (h.series.length > 1
        ? html(sparkline(h.series)) : html('<span class="dim">—</span>')) },
    { band: 'over the whole period', label: 'Change', num: true,
      hint: 'From the first collection that held it to the most recent',
      cell: (h) => {
        if (h.series.length < 2) return html('<span class="dim">—</span>');
        const drift = (h.series[h.series.length - 1] / h.series[0] - 1) * 100;
        // A price that never moved must not render as "−0.00%", which reads as a
        // tiny fall rather than as the flat line it is.
        if (Math.abs(drift) < 0.005) return cell('held', 'dim');
        return cell((drift > 0 ? '+' : '−') + Math.abs(drift).toFixed(2) + '%', drift > 0 ? 'up' : 'down');
      } },
  ], history, {
    empty: embedded.length < 2
      ? 'Only one collection in this page holds prices — collect again to watch this move.'
      : 'This bet was not stored in any collection held by this page.',
  });
}

/* ── one venue ───────────────────────────────────────────────────────────── */

function renderBook(key) {
  const run = runById.get(currentRunId);
  const note = DATA.sources.find((s) => s.key === key);
  const health = (run.sources || []).find((h) => h.key === key);

  if (!note && !health) {
    el('book-title').textContent = 'Pick a venue';
    el('book-what').textContent = 'Open “Where the prices come from” and click a card.';
    el('book-host').textContent = '';
    el('book-state').textContent = '';
    el('book-stats').innerHTML = '<div class="empty">No venue selected.</div>';
    el('book-skip-count').textContent = '';
    table(el('book-mix'), [{ label: '', cell: () => cell('') }], [], { empty: 'No venue selected.' });
    table(el('book-skips'), [{ label: '', cell: () => cell('') }], [], { empty: 'No venue selected.' });
    table(el('book-raws'), [{ label: '', cell: () => cell('') }], [], { empty: 'No venue selected.' });
    return;
  }

  el('book-title').textContent = (note && note.label) || book(key);
  el('book-what').textContent = [
    (note && note.what) || '',
    note && note.commission ? `Commission: ${note.commission}.` : '',
    (note && note.settles) || '',
  ].filter(Boolean).join(' ');
  el('book-host').textContent = (note && note.host) || '';
  el('book-state').innerHTML = health
    ? (health.ok
        ? '<span class="pill ok"><i></i>responded normally</span>'
        : `<span class="pill bad"><i></i>${escapeHtml(label(health.error_kind) || 'failed')}</span>`)
    : '<span class="pill flat">nothing recorded for this collection</span>';

  const mine = currentRows().filter((r) => str(r[COL.source]) === key);
  const stats = health ? [
    // "published", not "stored": these are two numbers whenever an insert
    // fails, and the row below says so rather than letting one stand for both.
    ['prices published', health.quote_count.toLocaleString(), 'by this book in this collection'],
    ...(health.stored_count === undefined || health.stored_count === health.quote_count ? []
      : [['reached the database', health.stored_count.toLocaleString(),
          'the rest were not stored — see Checks']]),
    ['fixtures', health.event_count, 'it published prices for'],
    ['pages read', health.request_count, fmtBytes(health.raw_bytes) + ' downloaded'],
    ['time spent', health.latency_ms === null ? '—' : (health.latency_ms / 1000).toFixed(2) + 's', 'fetching'],
    ['same as last time', `${health.unchanged_payloads}/${health.request_count}`,
      'byte-for-byte identical replies'],
    ['left alone', (health.skipped_count || 0).toLocaleString(), 'seen but out of scope'],
    ['refused', (health.scopes_refused || []).length + ' of ' +
      (health.scopes_requested || (health.scopes_refused || []).length || 0),
      'scopes it was asked for'],
  ] : [['prices stored', mine.length.toLocaleString(), 'no health record for this collection']];
  el('book-stats').innerHTML = stats.map(([name, value, sub]) =>
    `<div class="stat"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b><small>${
      escapeHtml(sub)}</small></div>`).join('');
  if (health && (health.scopes_refused || []).length) {
    // Named, not counted. Which league was refused is the whole content of the
    // signal — "5 refused" says a book had a bad day, "EPL, La Liga, Serie A,
    // Bundesliga, Ligue 1" says which prices are missing from the run.
    el('book-stats').innerHTML += `<div class="stat"><span>what it refused</span><small>${
      health.scopes_refused.map(escapeHtml).join('<br>')}</small></div>`;
  }
  if (health && health.error_message) {
    el('book-stats').innerHTML += `<div class="stat"><span>what it said</span><small>${
      escapeHtml(health.error_message)}</small></div>`;
  }

  const combos = new Map();
  for (const r of mine) {
    const k = str(r[COL.market]) + '\x1f' + str(r[COL.period]);
    if (!combos.has(k)) combos.set(k, { market: str(r[COL.market]), period: str(r[COL.period]),
      sport: str(r[COL.sport]), count: 0, events: new Set() });
    const entry = combos.get(k);
    entry.count += 1;
    entry.events.add(str(r[COL.event_key]));
  }
  table(el('book-mix'), [
    { label: 'Kind of bet', cell: (c) => cell(marketOf(c.market, c.sport).plain, '', marketOf(c.market, c.sport).term) },
    { label: 'Part of the game', cell: (c) => cell(periodOf(c.period).plain, 'dim', periodOf(c.period).term) },
    { label: 'Prices', num: true, cell: (c) => cell(c.count.toLocaleString()) },
    { label: 'Fixtures', num: true, cell: (c) => cell(c.events.size) },
  ], [...combos.values()].sort((a, b) => b.count - a.count),
     { empty: detailLoaded(currentRunId)
        ? 'This book stored no prices in this collection.'
        : `This book's ${(health ? health.quote_count : 0).toLocaleString()} prices are not in this page — only the newest few collections carry them. Rebuild with more --quote-runs to include it.` });

  const skips = DATA.skipped.filter((s) => s.run_id === currentRunId && s.source === key)
    .sort((a, b) => b.count - a.count);
  el('book-skip-count').textContent = skips.length
    ? `${skips.reduce((a, s) => a + s.count, 0).toLocaleString()} offers across ${skips.length} kinds`
    : detailLoaded(currentRunId) ? 'nothing skipped'
    : `${((health && health.skipped_count) || 0).toLocaleString()} recorded — breakdown not in this page`;
  table(el('book-skips'), [
    { label: 'What it was', cell: (r) => cell(r.reason.replace(/^criterion:/, '').replace(/^matchup_type:/, '').replace(/_/g, ' ')) },
    { label: 'How many', num: true, cell: (r) => cell(r.count.toLocaleString()) },
    { label: 'Why it was left alone', cell: (r) => cell(skipNote(r.reason), 'dim wrap') },
  ], skips, { empty: detailLoaded(currentRunId)
      ? 'Everything this book offered was in scope.'
      : `This book's ${((health && health.skipped_count) || 0).toLocaleString()} skipped offers are not in this page — only the newest few collections carry the breakdown. Rebuild with more --quote-runs to include it.` });

  // Rows that were *kept* after a field was rebuilt — shown apart from the
  // skips, because they are in the data.  Filed among the skips they read as
  // discarded; left out of the payload they read as nothing at all.
  const repaired = (DATA.repaired || [])
    .filter((r) => r.run_id === currentRunId && r.source === key)
    .sort((a, b) => b.count - a.count);
  if (repaired.length) {
    el('book-stats').innerHTML += `<div class="stat"><span>rebuilt and kept</span><b>${
      repaired.reduce((a, r) => a + r.count, 0).toLocaleString()}</b><small>${
      repaired.map((r) => escapeHtml(r.reason.replace(/_/g, ' '))).join('<br>')
      }</small></div>`;
  }

  const raws = DATA.raws.filter((r) => r.run_id === currentRunId && r.source === key);
  table(el('book-raws'), [
    { label: 'Which page', cell: (r) => cell(label(r.endpoint)) },
    { label: 'Reply', cell: (r) => html(`<span class="pill ${r.status_code === 200 ? 'ok' : 'bad'}">${
        r.status_code === 200 ? 'OK' : r.status_code}</span>`) },
    { label: 'Fetched at', cell: (r) => cell(fmtTime(r.fetched_at), 'mono dim') },
    { label: 'Size', num: true, cell: (r) => cell(fmtBytes(r.byte_size)) },
    { label: 'Fingerprint', hint: 'A checksum of the file, shortened',
      cell: (r) => cell(r.sha256.slice(0, 12), 'mono dim') },
    { label: 'Since last time', cell: (r) => html(r.unchanged
        ? '<span class="pill flat">identical</span>' : '<span class="pill accent">new content</span>') },
  ], raws, { empty: detailLoaded(currentRunId)
      ? 'No pages were saved from this book in this collection.'
      : `This book's ${((health && health.request_count) || 0).toLocaleString()} fetched pages are not listed in this page — only the newest few collections carry them. Rebuild with more --quote-runs to include it.` });
}

/* ── all prices ──────────────────────────────────────────────────────────── */

let sortKey = null;
let sortDir = 1;

function fillSelect(node, values, keepAll, naming) {
  const current = node.value;
  node.innerHTML = `<option value="">${keepAll}</option>` +
    values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(naming(v))}</option>`).join('');
  if (values.includes(current)) node.value = current;
}

// Search text per row, computed once and remembered.
//
// The search box re-filters on every keystroke, and building this string means
// fourteen lookups, three label translations and a join. At 3,000 rows that is
// invisible; at 300,000 it is 300,000 of them per character typed, and the box
// stops responding. A WeakMap rather than an array index so it stays correct
// whichever subset of rows is being shown, and empties itself when the rows do.
const HAYSTACKS = new WeakMap();

function haystack(r) {
  let found = HAYSTACKS.get(r);
  if (found === undefined) {
    const [home, away] = sidesOf(r);
    const sport = str(r[COL.sport]);
    found = [str(r[COL.event_key]), str(r[COL.home_team]), str(r[COL.away_team]),
      nick(home), nick(away), sport, sportLabel(sport), str(r[COL.league]),
      leagueLabel(str(r[COL.league])),
      marketOf(str(r[COL.market]), sport).plain, marketOf(str(r[COL.market]), sport).term,
      periodOf(str(r[COL.period])).plain, str(r[COL.selection]), book(str(r[COL.source]))]
      .join(' ').toLowerCase();
    HAYSTACKS.set(r, found);
  }
  return found;
}

function renderOdds() {
  const rows = currentRows();
  fillSelect(el('f-source'), [...new Set(rows.map((r) => str(r[COL.source])))].sort(), 'every sportsbook', book);
  fillSelect(el('f-market'), [...new Set(rows.map((r) => str(r[COL.market])))].sort(), 'every kind of bet', (v) => marketOf(v, currentSport).plain);
  fillSelect(el('f-period'), [...new Set(rows.map((r) => str(r[COL.period])))].sort(), 'any part of the game', (v) => periodOf(v).plain);
  fillSelect(el('f-league'), [...new Set(rows.map((r) => str(r[COL.league])))].sort(), 'every league', leagueLabel);

  const query = el('q').value.trim().toLowerCase();
  const fSource = el('f-source').value, fMarket = el('f-market').value;
  const fPeriod = el('f-period').value, fAlt = el('f-alt').value;
  const fLeague = el('f-league').value;

  let filtered = rows.filter((r) => {
    if (fSource && str(r[COL.source]) !== fSource) return false;
    if (fMarket && str(r[COL.market]) !== fMarket) return false;
    if (fPeriod && str(r[COL.period]) !== fPeriod) return false;
    if (fLeague && str(r[COL.league]) !== fLeague) return false;
    if (fAlt !== '' && String(r[COL.is_alternate]) !== fAlt) return false;
    if (query && !haystack(r).includes(query)) return false;
    return true;
  });

  const WHAT = 'what the bet is', PAYS = 'what it pays', CAN = 'can you place it';
  const columns = [
    { band: WHAT, key: 'source', label: 'Sportsbook', cell: (r) => cell(book(str(r[COL.source]))),
      sort: (r) => str(r[COL.source]) },
    { band: WHAT, key: 'sport', label: 'Sport', cell: (r) => cell(sportLabel(str(r[COL.sport])), 'dim'),
      sort: (r) => str(r[COL.sport]) },
    { band: WHAT, key: 'league', label: 'League',
      hint: 'Recorded for coverage only — two books may classify the same fixture differently',
      cell: (r) => cell(leagueLabel(str(r[COL.league])), 'dim', str(r[COL.league])),
      sort: (r) => str(r[COL.league]) },
    { band: WHAT, key: 'event', label: 'Fixture', hint: 'Away side at home side',
      cell: (r) => { const [home, away] = sidesOf(r);
        return cell(`${nick(away)} at ${nick(home)}`, '', str(r[COL.event_key])); },
      sort: (r) => str(r[COL.event_key]) },
    { band: WHAT, key: 'bet', label: 'The bet',
      cell: (r) => { const [home, away] = sidesOf(r);
        return cell(describeBet(betOf(r), home, away), 'plain', notation(betOf(r), home, away)); },
      sort: (r) => { const [home, away] = sidesOf(r); return describeBet(betOf(r), home, away); } },
    { band: WHAT, key: 'market', label: 'Kind of bet',
      cell: (r) => cell(marketOf(str(r[COL.market]), str(r[COL.sport])).plain, 'dim'),
      sort: (r) => str(r[COL.market]) },
    { band: WHAT, key: 'period', label: 'Part of game', cell: (r) => cell(periodOf(str(r[COL.period])).plain, 'dim'),
      sort: (r) => str(r[COL.period]) },
    { band: PAYS, key: 'dec', label: 'Price', hint: 'Decimal odds: total returned per $1 staked', num: true,
      cell: (r) => cell(fmtOdds(netOdds(r))), sort: netOdds },
    { band: PAYS, key: 'us', label: 'US odds', hint: 'The same price in American format, after commission', num: true,
      cell: (r) => cell(fmtAmerican(americanOf(r)), 'dim'), sort: americanOf },
    { band: PAYS, key: 'ret', label: '$100 returns', hint: 'What a winning $100 bet pays back in total', num: true,
      cell: (r) => cell(fmtReturn(netOdds(r))), sort: netOdds },
    { band: PAYS, key: 'prob', label: 'Implied chance', hint: 'How likely the price shown is treating this outcome', num: true,
      cell: (r) => cell((100 / netOdds(r)).toFixed(1) + '%', 'dim'),
      sort: (r) => 1 / netOdds(r) },
    { band: CAN, key: 'limit', label: 'Max bet', hint: 'Largest stake the book will accept, where it says', num: true,
      cell: (r) => cell(r[COL.limit_amount] === null ? '—' : '$' + Math.round(r[COL.limit_amount]).toLocaleString(), 'dim'),
      sort: (r) => r[COL.limit_amount] ?? -1 },
    { band: CAN, key: 'status', label: 'Taking bets', cell: (r) => {
        const active = str(r[COL.status]) === 'active';
        return html(`<span class="pill ${active ? 'flat' : 'warn'}">${active ? 'yes' : 'paused'}</span>`);
      }, sort: (r) => str(r[COL.status]) },
    { band: CAN, key: 'changed', label: 'Book last moved it', num: false,
      cell: (r) => cell(str(r[COL.last_change_at]) ? fmtClock(str(r[COL.last_change_at])) : '—', 'dim'),
      sort: (r) => str(r[COL.last_change_at]) || '' },
  ];

  if (sortKey) {
    const col = columns.find((c) => c.key === sortKey);
    if (col) {
      filtered = filtered.slice().sort((a, b) => {
        const x = col.sort(a), y = col.sort(b);
        return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
      });
    }
  }

  const cap = 500;
  const shown = filtered.slice(0, cap);
  el('odds-count').textContent = `${shown.length.toLocaleString()} of ${filtered.length.toLocaleString()} matching prices`;
  el('nav-odds').textContent = rows.length.toLocaleString();
  // Two different caps, and conflating them would be a lie in one direction or
  // the other. The table cap below is cosmetic — every row is still in the page,
  // narrowing the filters reveals it. The *embed* cap above is not: those rows
  // are in the database and not in this file at all.
  //
  // Which numbers it affects is stated precisely rather than sweepingly. The
  // coverage grid, the run list and each run's own totals are queried against
  // the whole run and are unaffected; only this table and its filters see the
  // embedded subset. Saying "every count on this page" was itself untrue.
  const embedNote = DATA.meta.quote_rows_capped
    ? `<b>&#9432;</b> This table holds the ${DATA.meta.quote_rows_embedded.toLocaleString()}
       soonest-starting of ${DATA.meta.quote_rows_available.toLocaleString()} price rows from
       these collections — the rest are in the database but were left out to keep the file
       openable. The coverage grid and the run totals above are counted over the whole
       collection and are unaffected; the counts in <i>this table</i> are counts of what is
       embedded. Rebuild with
       <code>--max-quote-rows ${DATA.meta.quote_rows_available}</code> to include every row. `
    : '';
  el('odds-note').innerHTML = embedNote + (filtered.length > cap
    ? `Showing the first ${cap} of ${filtered.length.toLocaleString()} matching prices — narrow the
       filters or search to see the rest. All of them are in this page; the table is capped only
       so your browser stays quick. Click any column heading to sort.`
    : 'Click any column heading to sort. Hover a row to see the same bet in sportsbook shorthand.');

  const node = table(el('odds-table'), columns, shown,
    { empty: 'No prices match those filters.', go: (r) => href('bet', betKeyOf(r)) });
  if (node) {
    // The last header row is the one holding the columns; above it sits the band row.
    node.querySelectorAll('thead tr:last-child th').forEach((th, i) => {
      const col = columns[i];
      th.classList.add('sortable');
      th.setAttribute('aria-sort', sortKey === col.key ? (sortDir === 1 ? 'ascending' : 'descending') : 'none');
      th.tabIndex = 0;
      const activate = () => {
        if (sortKey === col.key) sortDir = -sortDir; else { sortKey = col.key; sortDir = 1; }
        renderOdds();
      };
      th.addEventListener('click', activate);
      th.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') activate(); });
    });
  }
}

/* ── price changes ───────────────────────────────────────────────────────── */

function svgRunsChart() {
  const ordered = runs.slice().reverse();       // oldest first
  // The viewBox is near the width this actually renders at, so a fluid width
  // distorts x only slightly and circles stay circles.
  const W = 960, H = 168, top = 12, bottom = 26, left = 4, right = 4;
  const floor = H - bottom;
  const plot = floor - top;
  const maxQ = Math.max(1, ...ordered.map((r) => r.quote_count));
  const maxL = Math.max(1, ...ordered.map((r) => r.total_latency_ms || 0));
  const slot = (W - left - right) / Math.max(1, ordered.length);
  const centre = (i) => left + i * slot + slot / 2;

  const grid = [0.25, 0.5, 0.75, 1].map((f) =>
    `<line x1="${left}" y1="${(floor - plot * f).toFixed(1)}" x2="${W - right}" y2="${(floor - plot * f).toFixed(1)}"
       stroke="var(--line-soft)" stroke-width="1" />`).join('');

  const barW = Math.min(46, slot * 0.62);
  const bars = ordered.map((r, i) => {
    const h = Math.max(1, (r.quote_count / maxQ) * plot);
    return `<rect x="${(centre(i) - barW / 2).toFixed(1)}" y="${(floor - h).toFixed(1)}"
      width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="2"
      fill="${r.ok ? 'var(--accent)' : 'var(--down)'}" opacity="${r.id === currentRunId ? '1' : '0.55'}"
      ><title>${fmtClock(r.started_at)}: ${r.quote_count.toLocaleString()} prices, ${r.event_count} games, ${
        r.ok ? 'all checks passed' : 'checks found problems'}</title></rect>`;
  }).join('');

  const pts = ordered.map((r, i) => [centre(i), floor - ((r.total_latency_ms || 0) / maxL) * plot]);
  const path = pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const area = pts.length > 1
    ? `<polygon points="${left},${floor} ${path} ${W - right},${floor}" fill="var(--warn)" opacity="0.10" />`
    : '';
  const stroke = pts.length > 1
    ? `<polyline points="${path}" fill="none" stroke="var(--warn)" stroke-width="1.6" stroke-linejoin="round" />`
    : '';
  const dots = pts.map((p, i) => {
    const latest = i === pts.length - 1;
    return `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="${latest ? 4 : 2.5}"
      fill="${latest ? 'var(--warn)' : 'var(--surface)'}" stroke="var(--warn)" stroke-width="1.6"
      ><title>${fmtClock(ordered[i].started_at)}: ${((ordered[i].total_latency_ms || 0) / 1000).toFixed(2)}s spent fetching</title></circle>`;
  }).join('');

  const labels = slot > 44 ? ordered.map((r, i) =>
    `<text x="${centre(i).toFixed(1)}" y="${H - 8}" text-anchor="middle"
       font-family="ui-monospace, monospace" font-size="11"
       fill="${r.id === currentRunId ? 'var(--accent)' : 'var(--muted)'}">${
         new Date(r.started_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}</text>`).join('') : '';

  el('runs-chart').innerHTML = `
    <svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px" role="img"
         aria-label="Prices collected and time spent fetching, for each of the ${ordered.length} collections">
      ${grid}${area}${bars}${stroke}${dots}
      <line x1="${left}" y1="${floor}" x2="${W - right}" y2="${floor}" stroke="var(--line)" stroke-width="1.5" />
      ${labels}
    </svg>
    <div class="legend" style="margin-top:8px">
      <span><i class="swatch" style="background:var(--accent)"></i>prices collected — most was ${maxQ.toLocaleString()}</span>
      <span><i class="swatch" style="background:var(--warn)"></i>time spent fetching — most was ${(maxL / 1000).toFixed(1)}s</span>
      <span>oldest on the left, newest on the right</span>
    </div>`;
}

function sparkline(values) {
  const W = 78, H = 20, pad = 2.5;
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => [
    (i / Math.max(1, values.length - 1)) * (W - pad * 2) + pad,
    H - pad - ((v - lo) / span) * (H - pad * 3),
  ]);
  const path = pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const last = pts[pts.length - 1];
  const colour = values[values.length - 1] >= values[0] ? 'var(--up)' : 'var(--down)';
  return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">
    <polygon points="${pad},${H - pad} ${path} ${W - pad},${H - pad}" fill="${colour}" opacity="0.13" />
    <polyline points="${path}" fill="none" stroke="${colour}" stroke-width="1.3" stroke-linejoin="round" />
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2" fill="${colour}" />
  </svg>`;
}

// Only a bet seen **more than once** can be said to have held its price.
//
// This was `series.size - moved.length`, which counted every bet observed in
// exactly one collection as having "held exactly the same price across all N
// collections" — and that sentence is the one the page offers as its staleness
// signal, continuing "a book quietly serving a stale copy would show no movement
// at all here". The count grew by precisely the rows of any venue that went
// missing: across two passes with seven of ten sources blocked on the second,
// the page claimed 7,374 bets held their price when 4,452 had been seen twice
// and 2,922 had been seen once. It needs no outage either — started fixtures
// drop out and new ones appear on every pass.
function movementCounts(series, movedCount) {
  let comparable = 0;
  for (const s of series.values()) if (s.values.length > 1) comparable += 1;
  return { comparable, once: series.size - comparable, stable: comparable - movedCount };
}

function renderMovement() {
  svgRunsChart();

  const ordered = runs.slice().reverse().filter((r) => rowsByRun.has(r.id));
  const anchors = fixtureAnchorsFor(
    ordered.flatMap((run) => rowsByRun.get(run.id) || [])
  );
  const series = new Map();
  for (const run of ordered) {
    for (const r of rowsByRun.get(run.id)) {
      if (currentSport && str(r[COL.sport]) !== currentSport) continue;
      const bet = betOf(r);
      // Keyed on the scheduled start as well, for the reason ``betRows`` is: the
      // doubleheader ordinal is a within-run rank, so once game one has started
      // and been dropped, game two inherits the bare key.  Without this, 97
      // series on the live slate spliced game one's price onto game two's and
      // drew the join as a price movement — one of them a "+29.63% change" with
      // a sparkline, between two different games.
      const key = [str(r[COL.source]), str(r[COL.event_key]), bet.market, bet.period,
        bet.side || '', bet.selection, bet.line, fixtureBucket(r, anchors)].join('\x1f');
      if (!series.has(key)) series.set(key, { row: r, values: [] });
      const s = series.get(key);
      s.values.push(netOdds(r));
      s.row = r;
    }
  }

  const moved = [...series.values()]
    .filter((s) => s.values.length > 1 && Math.min(...s.values) !== Math.max(...s.values))
    .map((s) => {
      const first = s.values[0], last = s.values[s.values.length - 1];
      return Object.assign(s, {
        first, last,
        drift: (last / first - 1) * 100,
        swing: (Math.max(...s.values) / Math.min(...s.values) - 1) * 100,
      });
    })
    .sort((a, b) => b.swing - a.swing);

  fillSelect(el('move-source'), [...new Set(moved.map((s) => str(s.row[COL.source])))].sort(),
    'every sportsbook', book);
  const pickSource = el('move-source').value;
  const rows = moved.filter((s) => !pickSource || str(s.row[COL.source]) === pickSource);

  const { comparable, once, stable } = movementCounts(series, moved.length);
  el('move-count').textContent = `${moved.length.toLocaleString()} of ${comparable.toLocaleString()} bets changed price`;
  el('nav-move').textContent = moved.length.toLocaleString();

  table(el('move-table'), [
    { band: 'what the bet is', label: 'Sportsbook', cell: (s) => cell(book(str(s.row[COL.source]))) },
    { band: 'what the bet is', label: 'Sport', cell: (s) => cell(sportLabel(str(s.row[COL.sport])), 'dim') },
    { band: 'what the bet is', label: 'Fixture',
      cell: (s) => { const [home, away] = sidesOf(s.row);
        return cell(`${nick(away)} at ${nick(home)}`, '', str(s.row[COL.event_key])); } },
    { band: 'what the bet is', label: 'The bet',
      cell: (s) => { const [home, away] = sidesOf(s.row);
        return cell(describeBet(betOf(s.row), home, away), 'plain',
          notation(betOf(s.row), home, away)); } },
    { band: 'how the price moved', label: 'Over time',
      hint: 'Oldest on the left, newest on the right', cell: (s) => html(sparkline(s.values)) },
    { band: 'how the price moved', label: 'Started at', num: true, cell: (s) => cell(fmtOdds(s.first), 'dim') },
    { band: 'how the price moved', label: 'Now', num: true, cell: (s) => cell(fmtOdds(s.last)) },
    { band: 'how the price moved', label: 'Change', num: true,
      hint: 'How much the payout has moved since first seen',
      cell: (s) => cell((s.drift > 0 ? '+' : '−') + Math.abs(s.drift).toFixed(2) + '%',
        s.drift > 0 ? 'up' : 'down') },
    { band: 'how the price moved', label: 'Seen', num: true, hint: 'How many collections this bet appeared in',
      cell: (s) => cell(s.values.length) },
  ], rows.slice(0, 300), {
    go: (s) => href('bet', betKeyOf(s.row)),
    empty: ordered.length < 2
      ? 'Only one collection has prices in this page — collect again to watch them move.'
      // `comparable` is a count, not an array — `.length` on it was undefined,
      // so this branch never rendered and a page with nothing in common still
      // claimed "No price changed between these collections."
      : comparable === 0
        ? 'No bet appears in more than one of these collections, so there is nothing to compare.'
        : 'No price changed between these collections.',
  });

  el('move-note').textContent = ordered.length < 2
    ? 'Comparing prices needs at least two collections in this page. Collect again, then rebuild it.'
    : `${stable.toLocaleString()} bets held exactly the same price everywhere they were seen` +
      (once ? `; ${once.toLocaleString()} appeared in only one of the ${ordered.length} collections and cannot be compared` : '') +
      (rows.length > 300 ? `; the 300 biggest movers of ${rows.length.toLocaleString()} are shown` : '') +
      '. A book quietly serving a stale copy would show no movement at all here, and its saved pages ' +
      'below would be identical every time — which is why both are on this page.';
}

/* ── checks ──────────────────────────────────────────────────────────────── */

// Skips, rejections, raw responses and findings are fetched only for the runs
// whose prices are embedded, while the picker lists every run.  For the rest the
// page has *no rows*, which is not the same as *no such rows* — and it was saying
// the second: "This collection saved nothing" for a run holding 71 saved
// responses, with the true count printed in the flow diagram on the same screen.
function detailLoaded(runId) {
  return (DATA.detail_runs || []).indexOf(runId) !== -1;
}

function renderQuality() {
  const run = runById.get(currentRunId);
  const rows = currentRows();
  const findings = DATA.findings.filter((f) => f.run_id === currentRunId);
  // Findings and prices are both embedded only for the newest few runs, while the
  // picker lists many more.  For the rest, "0 problems / 0 impossible prices /
  // nothing flagged" is not a clean bill of health — it is four positive claims
  // about data the page never loaded, on runs the collector may have marked
  // failed. Say what is actually known instead.
  // Judged on the run's *unfiltered* embedded rows — ``currentRows()`` is
  // sport-filtered, so a sport the row cap happened to cut blanked this whole
  // panel and pointed at the wrong remedy while the run's findings sat embedded
  // in the payload.  And a run can be listed with no prices for two different
  // reasons that need two different sentences: its prices were not embedded
  // (rebuild with a larger --quote-runs), or it genuinely stored none (no
  // rebuild will ever add any — its findings are already here, so show them).
  const loaded = runRows().length > 0;
  const detail = detailLoaded(currentRunId);
  el('nav-quality').textContent = !loaded && !detail
    ? '—' : (findings.length ? String(findings.length) : 'clear');
  // ``run.ok`` is serialized as a JSON boolean; comparing it with 0 left the
  // "recorded this run as failed" branch unreachable, and there is no third
  // verdict for a finished run, so "unknown" never happens either.
  const verdict = !run ? ''
    : run.ok === false ? 'The collector recorded this run as <b>failed</b>.'
    : 'The collector recorded it as passing.';
  if (!loaded && !detail) {
    const stored = run ? (run.error_count || 0) + (run.warning_count || 0) : 0;
    el('quality-strip').innerHTML =
      `<div class="empty">This collection's prices are not embedded in this file — only the
       newest ${DATA.meta.runs_with_rows} of ${DATA.meta.runs_recorded} are, to keep the file
       openable. ${verdict}
       It stored ${stored} finding(s). Re-run the report with a larger
       <code>--quote-runs</code> to see them here.</div>`;
    ['findings', 'overround', 'rejections', 'skips'].forEach((id) => {
      const node = el(id);
      if (node) node.innerHTML = '';
    });
    return;
  }

  // Loaded in full, or only partly?
  //
  // The guard above covers the all-or-nothing case; truncation is the *partial*
  // one, and it is what the row cap actually produces — the fill is ordered by
  // run then kickoff, so the newest run is whole and older ones are cut to
  // their soonest fixtures.  On a cut run "impossible prices: none — the
  // pricing adds up" is a positive claim about rows the page never loaded: a
  // genuinely crossed market on a far-out fixture is simply absent, and the
  // same stored run reports a fault at a larger cap and a clean bill at a
  // smaller one.
  // Measured against the run's *unfiltered* embedded rows.  Comparing the whole
  // run's stored count with ``currentRows()``, which the sport picker filters,
  // made every sport look like a truncation: selecting Hockey on a run embedded
  // in full read "none in the 5% of this collection embedded here", where 5% is
  // the hockey share and nothing had been left out at all — and the remedy it
  // implies, a bigger cap, would change nothing.
  const stored = run ? (run.quote_count || 0) : 0;
  const embedded = runRows().length;
  const partial = stored > embedded;
  const seenShare = stored ? Math.round((embedded / stored) * 100) : 100;

  const groups = marketGroups(rows.filter((r) => str(r[COL.status]) === 'active'));
  const overrounds = [];
  for (const group of groups.values()) {
    // Only a bet with every side priced says anything about the book's margin.
    const bySelection = new Map(group.map((r) => [str(r[COL.selection]) + (str(r[COL.side]) || ''), r]));
    const priced = [...bySelection.values()];
    // Every side present, judged against what this window actually settles on —
    // not against a bare count of the rows that happen to be here.
    if (!sumsToAMargin(priced)) continue;
    const sum = priced.reduce((a, r) => a + 1 / netOdds(r), 0);
    overrounds.push({ sum, source: str(group[0][COL.source]) });
  }
  const sums = overrounds.map((o) => o.sum).sort((a, b) => a - b);
  const median = sums.length ? sums[Math.floor(sums.length / 2)] : null;
  // Only a venue that quotes both sides itself can be said to price itself to
  // lose.  An exchange or prediction market publishes two independent books that
  // nobody quoted against each other, so a briefly crossed pair there is a real
  // (tiny) arbitrage, not a mispairing — counting it as "impossible" reports a
  // true observation as a fault.
  const impossible = overrounds.filter(
    (o) => o.sum < 1 && venueKind(o.source) === 'sportsbook').length;
  const crossed = overrounds.filter(
    (o) => o.sum < 1 && venueKind(o.source) !== 'sportsbook').length;

  // The findings cap is shared across every embedded run, so a run can have its
  // prices on the page and its notes cut from it.  "0 problems / nothing flagged"
  // then contradicts the stat strip above, which counts from the run's own row.
  const recordedFindings = run ? (run.error_count || 0) + (run.warning_count || 0) : 0;
  const findingsShort = findings.length < recordedFindings;
  const items = [
    ['problems found', findingsShort ? recordedFindings : findings.length,
      findingsShort ? 'recorded — not embedded in this page'
        : findings.length ? 'listed below' : 'nothing flagged',
      findingsShort || findings.length ? 'is-warn' : 'is-good'],
    ['prices tested', rows.length.toLocaleString(), 'teams, times, numbers, duplicates'],
    ['bets fully priced', overrounds.length.toLocaleString(), 'every side present'],
    ["venue's usual cut", median === null ? '—' : ((median - 1) * 100).toFixed(1) + '%', 'after commission where there is one'],
    ['impossible prices', impossible,
      impossible ? 'a book pricing itself to lose — mispaired'
        : !runRows().length ? 'no prices stored to test'
        : partial ? `none in the ${seenShare}% of this collection embedded here`
        : 'none — the pricing adds up',
      impossible ? 'is-bad' : 'is-good'],
    ['crossed order books', crossed,
      crossed ? 'two sides of an exchange briefly overlapping — real, not a fault'
        : !runRows().length ? 'no prices stored to test'
        : partial ? `none in the ${seenShare}% of this collection embedded here`
        : 'none on the exchanges or prediction markets'],
    // The replay check runs once, against the newest collection's captures —
    // rendering its PASS beside an older selected run claimed bytes the check
    // never re-read (measured: PASS shown for a run whose captures were pruned
    // and whose own ``replay --run`` says FAIL).
    ['re-read from disk',
      currentRunId === DATA.meta.replay_run_id ? DATA.meta.replay_note : '—',
      currentRunId === DATA.meta.replay_run_id
        ? 'same answer as when stored'
        : `checked for collection ${DATA.meta.replay_run_id} only — run \`replay --run ${run ? run.id : ''}\` to check this one`],
  ];
  el('quality-strip').innerHTML = items.map(([name, value, sub, cls]) =>
    `<div class="stat ${cls || ''}"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b><small>${escapeHtml(sub)}</small></div>`
  ).join('');

  table(el('findings'), [
    { label: 'How serious', cell: (f) => html(`<span class="pill ${f.severity === 'error' ? 'bad' : 'warn'}"><i></i>${
        f.severity === 'error' ? 'problem' : 'worth a look'}</span>`) },
    { label: 'Check', cell: (f) => cell(label(f.code)) },
    { label: 'Sportsbook', cell: (f) => cell(f.source ? book(f.source) : '—', 'dim') },
    { label: 'Fixture', cell: (f) => cell(f.event_key || '—', 'mono dim') },
    { label: 'What it says', cell: (f) => cell(f.message, 'wrap') },
  ], findings, { empty: findingsShort
                   ? `This collection's ${recordedFindings.toLocaleString()} notes are not in this page — the 500 embedded here were used up by other collections. Rebuild with fewer --quote-runs to include them.`
                   : 'Nothing was flagged in this collection — no problems, nothing worth a look.',
                 go: (f) => (f.event_key ? href('fixture', f.event_key) : null) });

  const perSource = new Map();
  for (const o of overrounds) {
    if (!perSource.has(o.source)) perSource.set(o.source, []);
    perSource.get(o.source).push(o.sum);
  }
  const buckets = [[1.0, 1.03], [1.03, 1.05], [1.05, 1.08], [1.08, 1.15], [1.15, 1.6], [1.6, Infinity]];
  el('overround').innerHTML = `<div class="bars">${[...perSource.entries()].sort().flatMap(([source, values]) => {
    const sorted = values.slice().sort((a, b) => a - b);
    const med = sorted[Math.floor(sorted.length / 2)];
    const head = `<div class="bar-row" style="grid-template-columns:1fr"><b>${escapeHtml(book(source))}</b></div>`;
    const rows = buckets.map(([lo, hi]) => {
      const n = values.filter((v) => v >= lo && v < hi).length;
      const pct = (n / values.length) * 100;
      const name = hi === Infinity ? `over ${((lo - 1) * 100).toFixed(0)}%`
        : `${((lo - 1) * 100).toFixed(0)}–${((hi - 1) * 100).toFixed(0)}%`;
      return `<div class="bar-row"><span class="mono dim">${name}</span>
        <span class="track"><span class="fill" style="width:${pct.toFixed(1)}%"></span></span>
        <span class="val">${n}</span></div>`;
    }).join('');
    return [head, rows,
      `<div class="bar-row" style="grid-template-columns:1fr"><span class="dim">typically ${
        ((med - 1) * 100).toFixed(1)}% across ${values.length} bets</span></div>`];
  }).join('')}</div>
  <p class="note">Each row is a slice of that book's bets: how many of them carried a cut in
  that range. Most sit in one or two buckets — a book prices its whole slate to a house style.</p>`;

  const rejections = DATA.rejections.filter((r) => r.run_id === currentRunId);
  // The 500-row cap is shared across every embedded run and the health rows
  // carry each source's true rejection count, so the page can always tell a
  // clean run from one whose rejections were cut — the findings table above
  // makes exactly this check, and this table said "nothing had to be thrown
  // away" about 60 rejections the cap had squeezed out.  (``run`` is the
  // function-level binding from the top of renderQuality.)
  const recordedRejections = run
    ? run.sources.reduce((total, h) => total + (h.rejection_count || 0), 0) : 0;
  const rejectionsShort = rejections.length < recordedRejections;
  el('rejections-note').textContent = rejectionsShort && rejections.length
    ? `${rejections.length.toLocaleString()} of ${recordedRejections.toLocaleString()} rejected rows shown — the rest did not fit this page's 500-row cap. Rebuild with fewer --quote-runs to include them.`
    : '';
  table(el('rejections'), [
    { label: 'Venue', cell: (r) => cell(book(r.source)) },
    { label: 'Why', cell: (r) => cell(label(r.reason)) },
    { label: 'Detail', cell: (r) => cell(r.detail, 'wrap') },
  ], rejections, { empty: rejectionsShort
      ? `This collection's ${recordedRejections.toLocaleString()} rejected rows are not in this page — the 500 embedded here were used up by other collections. Rebuild with fewer --quote-runs to include them.`
      : 'Nothing had to be thrown away in this collection.',
  });
}

/* ── saved pages ─────────────────────────────────────────────────────────── */

function renderRaw() {
  const run = runById.get(currentRunId);
  const raws = DATA.raws.filter((r) => r.run_id === currentRunId);
  el('nav-raw').textContent = detailLoaded(currentRunId)
    ? raws.length : ((run && run.raw_count) || 0);
  table(el('raws'), [
    { band: 'what was asked for', label: 'Sportsbook', cell: (r) => cell(book(r.source)) },
    { band: 'what was asked for', label: 'Which page', cell: (r) => cell(label(r.endpoint)) },
    { band: 'what came back', label: 'Reply', hint: 'The HTTP status the server sent back; 200 means OK',
      cell: (r) => html(`<span class="pill ${r.status_code === 200 ? 'ok' : 'bad'}">${
        r.status_code === 200 ? 'OK' : r.status_code}</span>`) },
    { band: 'what came back', label: 'Fetched at', cell: (r) => cell(fmtTime(r.fetched_at), 'mono dim') },
    { band: 'what came back', label: 'Size', num: true, cell: (r) => cell(fmtBytes(r.byte_size)) },
    { band: 'the file on disk', label: 'Fingerprint', hint: 'A checksum of the file, shortened',
      cell: (r) => cell(r.sha256.slice(0, 12), 'mono dim') },
    { band: 'the file on disk', label: 'Since last time', cell: (r) => html(r.unchanged
        ? '<span class="pill flat">identical</span>' : '<span class="pill accent">new content</span>') },
    { band: 'the file on disk', label: 'Address', cell: (r) => cell(r.url, 'dim') },
  ], raws, { empty: detailLoaded(currentRunId)
               ? 'This collection saved nothing.'
               : `This collection's ${(run && run.raw_count || 0).toLocaleString()} saved replies are not in this page — only the newest few collections carry them. Rebuild with more --quote-runs to include it.`,
             go: (r) => href('book', r.source) });
}

/* ── reference ───────────────────────────────────────────────────────────── */

function renderReference() {
  el('glossary-list').innerHTML = DATA.glossary.map((entry) =>
    `<div><dt>${escapeHtml(entry.term)}</dt><dd>${escapeHtml(entry.plain)}</dd></div>`).join('');

  table(el('schema-table'), [
    { label: 'Column', cell: (f) => cell(f.name, 'mono') },
    { label: 'Type', cell: (f) => cell(f.type, 'dim') },
    { label: 'Always set?', cell: (f) => html(f.required
        ? '<span class="pill flat">always</span>' : '<span class="dim">only when it applies</span>') },
    { label: 'What it holds', cell: (f) => cell(f.note, 'wrap') },
  ], DATA.schema_fields);

  el('vocab').innerHTML = `<dl class="kv">${DATA.vocabularies.map((v) =>
    `<dt>${escapeHtml(v.name)}</dt><dd>${v.values.map((x) => escapeHtml(x)).join(' · ')}</dd>`).join('')}</dl>
    <p class="note">${escapeHtml(DATA.meta.vocab_note)}</p>`;
}

/* ── wiring ──────────────────────────────────────────────────────────────── */

function renderRunScoped() {
  renderOverview();
  renderSports();
  renderSources();
  renderEvents();            // also fills the fixture panel with a default
  renderOdds();
  renderMovement();
  renderQuality();
  renderRaw();
  // The detail panels are rendered even while off screen: a link straight into one
  // must open on something, and a panel that is only filled on arrival is a panel
  // whose render path nothing exercises until a reader finds it broken.
  renderBook(defaultBook());
  renderBet(defaultBet());
}

/** The book and bet the detail panels sit on until something is clicked. */
function defaultBook() {
  const run = runById.get(currentRunId) || { sources: [] };
  const first = (run.sources || [])[0];
  return here.panel === 'book' && here.arg ? here.arg : (first ? first.key : null);
}
function defaultBet() {
  if (here.panel === 'bet' && here.arg) return here.arg;
  const row = currentRows()[0];
  return row ? betKeyOf(row) : null;
}

buildRunPicker();
buildSportPicker();
renderRunScoped();
renderReference();

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('hashchange', applyRoute);
}
applyRoute();

['q', 'f-source', 'f-league', 'f-market', 'f-period', 'f-alt'].forEach((id) => {
  el(id).addEventListener('input', renderOdds);
});
el('cov-mode').addEventListener('change', renderEvents);
el('move-source').addEventListener('change', renderMovement);
// The sport filter is bound once, on the element rather than on its options, so
// rebuilding the list for a different run cannot silently drop the handler.
el('sport-pick').addEventListener('change', () => {
  currentSport = el('sport-pick').value;
  selectedEvent = null;
  buildSportPicker();
  renderRunScoped();
});
"""
