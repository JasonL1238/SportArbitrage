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
  /* Calm dark board. Soft amber marks the best takeable price on one line. */
  color-scheme: dark;
  --ground:      #141414;
  --surface:     #1a1a1a;
  --surface-2:   #222222;
  --line:        #333333;
  --line-soft:   #2a2a2a;
  --ink:         #ecece8;
  --ink-2:       #b5b5ae;
  --muted:       #8a8a82;
  --accent:      #7aa3c9;
  --up:          #5cb88a;
  --down:        #e07a70;
  --warn:        #d0b45a;
  --warn-soft:   #2a2618;
  --best:        #5a4e22;
  --best-ink:    #f5edd0;

  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", sans-serif;

  --rail: 220px;
  --pad: 22px;
  --radius: 4px;
}

:root[data-theme="light"] {
  color-scheme: light;
  --ground: #f7f7f5; --surface: #ffffff; --surface-2: #f1f1ee;
  --line: #dddcd6; --line-soft: #ebeae4; --ink: #1c1c1a; --ink-2: #4a4a45;
  --muted: #6e6e67; --accent: #2f5d8c;
  --up: #1f6b45; --down: #a33a30; --warn: #8a6a14;
  --warn-soft: #f7f1de;
  --best: #f0e2a8; --best-ink: #3a3208;
}
:root[data-theme="dark"] {
  color-scheme: dark;
}

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font: 400 13.5px/1.5 var(--sans);
  -webkit-font-smoothing: antialiased;
}

a { color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }

/* ── frame ─────────────────────────────────────────────────────────────── */

.shell { display: grid; grid-template-columns: var(--rail) minmax(0, 1fr); gap: 0; }

.rail {
  position: sticky; top: 0; align-self: start; height: 100vh;
  display: flex; flex-direction: column; gap: 16px;
  padding: 16px 14px; border-right: 1px solid var(--line);
  background: var(--surface); overflow-y: auto;
}
.brand { display: flex; flex-direction: column; gap: 2px; padding: 0 4px; }
.brand b { font: 600 13px/1.3 var(--sans); }
.brand span { font: 400 11px/1.4 var(--mono); color: var(--muted); }

.nav { display: flex; flex-direction: column; gap: 0; }
.nav a {
  display: flex; justify-content: space-between; gap: 8px; align-items: baseline;
  padding: 5px 6px; border-radius: 3px; text-decoration: none;
  color: var(--ink-2); font-size: 12.5px; font-weight: 400;
}
.nav a:hover { color: var(--ink); background: var(--surface-2); }
.nav a[aria-current="true"] { color: var(--ink); font-weight: 600; background: transparent; }
.nav a i { font: 400 11px/1 var(--mono); color: var(--muted); font-style: normal; }
.nav-more {
  margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line-soft);
}
.nav-more > summary {
  list-style: none; cursor: pointer; user-select: none;
  padding: 5px 6px; border-radius: 3px;
  font: 500 11px/1.2 var(--sans); color: var(--muted);
}
.nav-more > summary::-webkit-details-marker { display: none; }
.nav-more > summary::before {
  content: '▸ '; font-size: 10px; opacity: 0.7;
}
.nav-more[open] > summary::before { content: '▾ '; }
.nav-more > summary:hover { color: var(--ink-2); background: var(--surface-2); }
.nav-more .nav { margin-top: 2px; }
.fold {
  margin-top: 14px; border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--surface); overflow: hidden;
}
.fold > summary {
  list-style: none; cursor: pointer; user-select: none;
  display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between;
  gap: 8px; padding: 9px 12px; border-bottom: 1px solid transparent;
  font: 600 12.5px/1.3 var(--sans); color: var(--ink);
}
.fold > summary::-webkit-details-marker { display: none; }
.fold[open] > summary { border-bottom-color: var(--line-soft); }
.fold > summary .eyebrow { font-weight: 400; }
.fold .card-body { border: 0; }

.rail-block { display: flex; flex-direction: column; gap: 5px; }
.rail-block > label,
.eyebrow {
  font: 500 11px/1.3 var(--sans); color: var(--muted);
}
select, input[type="search"], input[type="text"] {
  width: 100%; padding: 5px 7px; border: 1px solid var(--line);
  border-radius: 3px; background: var(--surface); color: var(--ink);
  font: 400 12px/1.4 var(--sans);
}
.rail-foot { margin-top: auto; font: 400 11px/1.45 var(--mono); color: var(--muted); }
.state-picks { display: grid; grid-template-columns: repeat(2, 1fr); gap: 3px 8px; }
.state-picks label { font: 500 11px/1.3 var(--mono); color: var(--ink-2); }
.state-picks input { margin: 0 4px 0 0; vertical-align: -1px; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}

.scrape-btn {
  width: 100%; margin-top: 4px; padding: 7px 10px; border-radius: 3px;
  border: 1px solid var(--line); background: var(--surface-2); color: var(--ink);
  font: 500 12.5px/1.2 var(--sans); cursor: pointer;
}
.scrape-btn:hover { border-color: var(--muted); background: var(--surface); }
.scrape-btn:disabled { opacity: 0.55; cursor: wait; }
.scrape-btn.is-file {
  background: var(--surface-2); color: var(--muted);
  border-color: var(--line); cursor: not-allowed;
}
.scrape-progress {
  display: none; flex-direction: column; gap: 5px; margin-top: 6px;
}
.scrape-progress.on { display: flex; }
.scrape-bar {
  height: 4px; border-radius: 2px; background: var(--line-soft); overflow: hidden;
}
.scrape-bar > i {
  display: block; height: 100%; width: 0%;
  background: var(--accent); transition: width 0.25s ease;
}
.scrape-progress.is-indeterminate .scrape-bar > i {
  width: 35% !important;
  animation: scrape-pulse 1.1s ease-in-out infinite;
}
@keyframes scrape-pulse {
  0% { transform: translateX(-120%); }
  100% { transform: translateX(320%); }
}
.scrape-progress .scrape-msg {
  font: 400 11px/1.35 var(--sans); color: var(--ink-2);
}
.scrape-progress .scrape-meta {
  font: 400 10.5px/1.3 var(--mono); color: var(--muted);
}

.promo-toolbar {
  display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: end;
  margin: 0 0 12px;
}

.campaign-table th {
  text-align: left; padding: 6px 10px; white-space: nowrap;
  font: 500 10px/1.3 var(--mono); text-transform: uppercase;
  letter-spacing: .08em; color: var(--muted);
  border-bottom: 1px solid var(--line);
}
.campaign-table td {
  padding: 6px 10px; white-space: nowrap; border-bottom: 1px solid var(--line);
}
.campaign-table tbody tr { cursor: pointer; }
.campaign-table tbody tr:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }
.campaign-table td.num { font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; }
.campaign-table tr.is-claimed td { color: var(--muted); text-decoration: line-through; }
.campaign-table tr.is-claimed td.claim-cell { text-decoration: none; }
.campaign-table td.ev-pos { color: var(--up); font-weight: 600; }
.campaign-table .expiry-soon { color: var(--warn); }
.promo-toolbar label { display: flex; flex-direction: column; gap: 3px; min-width: 140px; }
.promo-toolbar .eyebrow { margin: 0; }
.promo-list { display: flex; flex-direction: column; gap: 8px; }
.promo-row {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 14px;
  padding: 10px 0; border-top: 1px solid var(--line-soft);
  cursor: pointer;
}
.promo-row:first-child { border-top: 0; padding-top: 0; }
.promo-row:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }
.promo-row.is-open { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.promo-row .title { margin: 0; font: 600 13px/1.35 var(--sans); color: var(--ink); }
.promo-row .meta {
  margin: 3px 0 0; font: 400 11.5px/1.4 var(--sans); color: var(--ink-2);
}
.promo-row .desc {
  margin: 5px 0 0; font: 400 12px/1.45 var(--sans); color: var(--muted);
  max-width: 72ch;
}
.promo-row .side {
  display: flex; flex-direction: column; align-items: flex-end; gap: 4px;
  font: 400 11px/1.3 var(--mono); color: var(--muted); white-space: nowrap;
}
.promo-row a.promo-link {
  color: var(--accent); text-decoration: none; font: 500 11.5px/1.3 var(--sans);
}
.promo-row a.promo-link:hover { text-decoration: underline; }
.promo-detail {
  grid-column: 1 / -1;
  margin-top: 4px;
  padding: 10px 12px;
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  background: var(--bg, transparent);
  display: none;
}
.promo-row.is-open .promo-detail { display: block; }
.promo-detail h4 {
  margin: 0 0 6px; font: 600 12px/1.3 var(--sans); color: var(--ink);
}
.promo-detail p, .promo-detail li {
  margin: 0 0 6px; font: 400 12px/1.45 var(--sans); color: var(--ink-2);
  max-width: 78ch;
}
.promo-detail .terms {
  white-space: pre-wrap; max-height: 160px; overflow: auto;
  font: 400 11px/1.4 var(--mono); color: var(--muted);
}
.promo-detail .pill-row { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 8px; }
.plan-card {
  border: 1px solid var(--line-soft); border-radius: 6px;
  padding: 8px 10px; margin: 6px 0 10px;
}
.plan-card h5 { margin: 0 0 2px; font: 600 12.5px/1.4 var(--sans); color: var(--ink); }
.plan-card .plan-sub { margin: 0 0 6px; font: 400 11.5px/1.4 var(--sans); color: var(--muted); }
.plan-card table { width: auto; min-width: 60%; margin: 4px 0; }
.plan-card td, .plan-card th { padding: 2px 10px 2px 0; font-size: 12px; }
.plan-card .plan-outcomes { margin: 4px 0 0; font: 400 11.5px/1.4 var(--mono); color: var(--ink-2); }
.promo-health {
  display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px;
}
.promo-health .pill { font-size: 11px; }

.run-list {
  display: flex; flex-direction: column; gap: 4px;
  max-height: min(60vh, 480px); overflow: auto; padding-right: 2px;
}
.run-item {
  display: flex; flex-direction: column; gap: 1px; text-align: left;
  width: 100%; padding: 6px 7px; border-radius: 3px; cursor: pointer;
  border: 1px solid transparent; background: transparent; color: inherit;
  font: 400 11.5px/1.35 var(--sans);
}
.run-item:hover { background: var(--surface-2); }
.run-item.on { border-color: var(--line); background: var(--surface-2); }
.run-item b { font: 600 12px/1.3 var(--sans); }
.run-item .when { font: 400 10.5px/1.3 var(--mono); color: var(--muted); }
.run-item .bits { font: 400 10.5px/1.3 var(--sans); color: var(--ink-2); }
.run-item.is-bad { border-color: color-mix(in srgb, var(--down) 28%, var(--line)); }
.run-item.is-thin { opacity: 0.7; }

main { min-width: 0; padding: 0 var(--pad) 72px; }

.masthead {
  display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
  gap: 12px; padding: 22px 0 14px;
}
.masthead h1 { margin: 0; font: 600 20px/1.25 var(--sans); text-wrap: balance; }
.masthead p { margin: 6px 0 0; max-width: 72ch; color: var(--muted); font-size: 12.5px; }
.scrape-stamp {
  margin: 4px 0 0; font: 500 12.5px/1.35 var(--sans); color: var(--ink-2);
}
.scrape-stamp b { font-weight: 600; color: var(--ink); }
.scrape-stamp .ago { color: var(--muted); font-weight: 400; }

/* One panel is on screen at a time.  Ten stacked sections meant scrolling past
   nine of them to reach the tenth; the rail switches between them instead, and
   clicking a row opens the thing that row describes. */
section { display: none; margin-bottom: 28px; }
section.on { display: block; }
section > header { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px 12px; margin-bottom: 10px; }
section > header h2 { margin: 0; font: 600 14px/1.3 var(--sans); }
section > header p { margin: 0; color: var(--muted); font-size: 12px; max-width: 78ch; }

/* ── drill-down ────────────────────────────────────────────────────────────
   The trail is the only thing telling a reader how deep they are and how to get
   back, so it is always rendered — even one level down, where it is just the
   panel's own name. */

.crumbs { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; margin: 0 0 12px; font-size: 12px; }
.crumbs a { color: var(--muted); text-decoration: none; }
.crumbs a:hover { color: var(--accent); text-decoration: underline; }
.crumbs b { color: var(--ink); font-weight: 550; }
.crumbs i { font-style: normal; color: var(--muted); opacity: 0.5; }

/* A row that opens something has to look like it does. */
tbody tr.go { cursor: pointer; }
tbody tr.go:hover { background: var(--surface-2); }
tbody tr.go:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.headline { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.headline b { font: 600 17px/1.3 var(--sans); text-wrap: balance; }
.headline span { font: 400 12.5px/1.45 var(--sans); color: var(--ink-2); }
.headline code { font: 400 11.5px/1.45 var(--mono); color: var(--muted); }

/* One card per venue holding that venue's price for one bet. */
.quotes { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px; }
.qcard {
  display: flex; flex-direction: column; gap: 6px; padding: 10px 12px;
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
}
.qcard .who { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.qcard .who b { font: 600 13px/1.2 var(--sans); }
.qcard .price { font: 600 22px/1 var(--mono); font-variant-numeric: tabular-nums; }
.qcard.top .price { color: var(--up); }
.qcard.off .price { color: var(--muted); }
.qcard dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 0; font-size: 12px; }
.qcard dt { color: var(--muted); }
.qcard dd { margin: 0; font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; }

/* Arbitrage opportunity cards */
.arb-empty { padding: 14px 2px; color: var(--ink-2); font-size: 12.5px; max-width: 64ch; }
.arb-card + .arb-card { margin-top: 10px; border-top: 1px solid var(--line-soft); padding-top: 12px; }
.arb-card .arb-top {
  display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between;
  gap: 6px 14px; margin-bottom: 8px;
}
.arb-card .arb-top b { font: 600 14px/1.3 var(--sans); }
.arb-card .arb-meta { font: 400 12px/1.4 var(--sans); color: var(--ink-2); }
.arb-pill {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 1px 0; border-radius: 0;
  background: transparent; color: var(--ink);
  font: 600 12px/1.2 var(--mono); font-variant-numeric: tabular-nums;
}
.arb-pill.ok { color: var(--up); }
.arb-pill.warn { color: var(--warn); }
.arb-kpis {
  display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 0 0 10px;
  font: 400 12px/1.35 var(--sans); color: var(--ink-2);
}
.arb-kpis strong { color: var(--ink); font-family: var(--mono); font-variant-numeric: tabular-nums; }
.arb-legs th {
  text-align: left; font: 500 11px/1.2 var(--sans);
  color: var(--muted); padding: 0 10px 5px 0;
}
.arb-legs td {
  padding: 6px 10px 6px 0; border-top: 1px solid var(--line-soft);
  font-variant-numeric: tabular-nums; vertical-align: top;
}
.arb-legs td.num { font-family: var(--mono); text-align: right; padding-right: 0; }
/* An exact bet link is the one the reader wants; a league page is a consolation
   prize and must not look like the same offer. */
.arb-legs a.bet-exact { font-weight: 600; }
.arb-legs a { white-space: nowrap; }
.arb-outcomes {
  margin: 8px 0 0; font: 400 12px/1.45 var(--mono); color: var(--ink-2);
  font-variant-numeric: tabular-nums;
}
.arb-notes { margin: 6px 0 0; font-size: 12px; color: var(--muted); }
.arb-reject { display: flex; flex-wrap: wrap; gap: 6px 12px; }
.arb-reject span {
  display: inline-flex; gap: 6px; align-items: baseline;
  padding: 0; border: 0; border-radius: 0;
  font: 400 12px/1.35 var(--sans); color: var(--ink-2);
}
.arb-reject b { font-family: var(--mono); color: var(--ink); font-weight: 600; }

/* ── placed-bet ledger ─────────────────────────────────────────────────────
   The one part of the page that writes rather than reads, so its controls are
   deliberately louder than the rest of the chrome: a stake box that looks like
   a label gets typed into by accident. */
.bl-btn {
  font: 500 11.5px/1 var(--sans); color: var(--ink);
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 5px; padding: 5px 9px; cursor: pointer; white-space: nowrap;
}
.bl-btn:hover { border-color: var(--muted); }
.bl-btn:disabled { opacity: 0.5; cursor: default; }
.bl-btn.primary { font-weight: 600; border-color: var(--muted); }
.bl-btn.danger:hover { color: var(--down); border-color: var(--down); }
.bl-btn.is-file { opacity: 0.55; cursor: not-allowed; }

.bl-drawer { margin-top: 10px; padding: 10px; border: 1px dashed var(--line);
  border-radius: var(--radius); background: var(--surface-2); }
.bl-drawer[hidden] { display: none; }
.bl-drawer .bl-legs td { padding: 4px 8px 4px 0; border-bottom: 1px solid var(--line-soft); }
.bl-drawer .bl-legs td.num { text-align: right; font-family: var(--mono); }

.bl-field { display: inline-flex; flex-direction: column; gap: 3px; }
.bl-field > span { font: 400 10.5px/1.2 var(--sans); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.04em; }
.bl-field input, .bl-field select {
  font: 400 12.5px/1.2 var(--mono); color: var(--ink);
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 5px; padding: 5px 7px; min-width: 0;
}
.bl-field input:focus, .bl-field select:focus { outline: 1px solid var(--muted); }
.bl-form { display: flex; flex-wrap: wrap; gap: 10px 12px; align-items: flex-end; }
.bl-form .bl-field.wide { flex: 1 1 220px; }
.bl-form .bl-field.wide input { width: 100%; }
.bl-form .bl-field input.money, .bl-form .bl-field input.odds { width: 8ch; }
.bl-msg { margin: 8px 0 0; font: 400 12px/1.45 var(--sans); color: var(--ink-2); }
.bl-msg.bad { color: var(--down); }
.bl-msg.good { color: var(--up); }

.bl-slip + .bl-slip { margin-top: 10px; border-top: 1px solid var(--line-soft); padding-top: 12px; }
.bl-slip .bl-top { display: flex; flex-wrap: wrap; gap: 6px 12px;
  align-items: baseline; justify-content: space-between; }
.bl-slip .bl-top b { font: 600 14px/1.3 var(--sans); }
.bl-slip .bl-meta { font: 400 12px/1.4 var(--sans); color: var(--ink-2); }
.bl-slip .bl-kpis { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 6px 0 8px;
  font: 400 12px/1.45 var(--sans); color: var(--ink-2); }
.bl-slip .bl-kpis strong { color: var(--ink); font-family: var(--mono);
  font-variant-numeric: tabular-nums; }
.bl-slip table.bl-legs th {
  text-align: left; font: 500 10.5px/1.2 var(--sans); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.04em;
  padding: 0 8px 5px 0; border-bottom: 1px solid var(--line);
}
.bl-slip table.bl-legs td { padding: 6px 8px 6px 0; border-bottom: 1px solid var(--line-soft);
  vertical-align: middle; }
.bl-slip table.bl-legs td.num { font-family: var(--mono); text-align: right; padding-right: 0;
  font-variant-numeric: tabular-nums; }
.bl-slip input.bl-text { width: min(18ch, 100%); min-width: 8ch; }
.bl-slip textarea.bl-note { display: block; width: 100%; min-height: 2.6em; resize: vertical;
  margin-top: 7px; padding: 5px 7px; border: 1px solid var(--line); border-radius: 4px;
  color: var(--ink); background: var(--surface-2); font: 400 12px/1.4 var(--sans); }
.bl-slip .bl-acts { display: flex; flex-wrap: wrap; gap: 5px; justify-content: flex-end; }
.bl-status {
  display: inline-block; padding: 1px 6px; border-radius: 999px;
  font: 500 10.5px/1.6 var(--sans); border: 1px solid var(--line); color: var(--ink-2);
}
.bl-status.won { color: var(--up); border-color: var(--up); }
.bl-status.lost { color: var(--down); border-color: var(--down); }
.bl-status.pending { color: var(--muted); }
.bl-pl.up { color: var(--up); }
.bl-pl.down { color: var(--down); }
.bl-note { margin: 6px 0 0; font: 400 12px/1.45 var(--sans); color: var(--muted); }
.bl-books { display: flex; flex-wrap: wrap; gap: 6px 14px; font: 400 12px/1.5 var(--sans);
  color: var(--ink-2); }
.bl-books span { display: inline-flex; gap: 6px; align-items: baseline; }
.bl-books > span.picked { color: var(--ink); text-decoration: underline; text-underline-offset: 3px; }
.bl-books b { font-family: var(--mono); color: var(--ink); font-weight: 600; }

.card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden;
}
.card + .card { margin-top: 12px; }
.card-head {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  gap: 8px; padding: 9px 12px; border-bottom: 1px solid var(--line-soft);
  background: transparent;
}
.card-head h3 { margin: 0; font: 600 12.5px/1.3 var(--sans); }
.card-head .eyebrow { font-size: 11px; }
.card-body { padding: 12px; }
.card-body.flush { padding: 0; }

/* ── explainers ────────────────────────────────────────────────────────────
   A novice reader's difficulty is rarely the numbers themselves; it is not
   knowing what a block of numbers is answering.  So every block is broken into
   named compartments and captioned where it sits, rather than in one long
   preamble nobody scrolls back to. */

/* Quiet compartments inside a single card. */
.tiles {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0; border-top: 1px solid var(--line-soft);
}
.tile {
  display: flex; flex-direction: column; gap: 4px; padding: 12px;
  background: var(--surface); border-right: 1px solid var(--line-soft);
  border-bottom: 1px solid var(--line-soft);
}
.tile .ord { font: 500 11px/1.2 var(--sans); color: var(--muted); }
.tile h4 { margin: 0; font: 600 13px/1.25 var(--sans); }
.tile p { margin: 0; font-size: 12px; color: var(--ink-2); max-width: 44ch; }
.tile b.big { font: 600 15px/1.2 var(--mono); font-variant-numeric: tabular-nums; color: var(--ink); }
.tile code { font: 400 11.5px/1.45 var(--mono); color: var(--muted); }
.how-tiles .tile .ord {
  display: inline; width: auto; height: auto; margin-bottom: 0;
  border-radius: 0; background: transparent; color: var(--muted);
  font: 500 11px/1.2 var(--sans);
}
.how-card + .card { margin-top: 12px; }
.home-more { margin: 12px 2px 0; font-size: 12px; max-width: 72ch; }

/* Clickable games — path into comparing books. */
.game-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 8px;
}
a.game-card {
  display: flex; flex-direction: column; gap: 5px; padding: 11px 12px;
  background: var(--surface); color: var(--ink); text-decoration: none;
  border: 1px solid var(--line); border-radius: var(--radius); min-height: 104px;
}
a.game-card:hover { border-color: var(--muted); background: var(--surface-2); }
a.game-card .when { font: 400 11px/1.3 var(--mono); color: var(--muted); }
a.game-card b { font: 600 13.5px/1.3 var(--sans); text-wrap: balance; }
a.game-card .meta { font: 400 12px/1.35 var(--sans); color: var(--ink-2); }
a.game-card .mlines {
  display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 2px;
}
a.game-card .mlines span {
  display: flex; flex-direction: column; gap: 1px; padding: 5px 6px;
  border-radius: 3px; background: var(--surface-2); font-size: 11.5px;
}
a.game-card .mlines i { font: 400 10px/1.2 var(--sans); font-style: normal; color: var(--muted); }
a.game-card .mlines b { font: 600 13px/1.2 var(--mono); }
a.game-card .cta { margin-top: auto; font: 500 11.5px/1.3 var(--sans); color: var(--accent); }

/* Captions pinned above the data they describe. */
.brief {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 6px 18px; padding: 8px 12px;
  background: transparent;
  border-bottom: 1px solid var(--line-soft);
}
.brief p { margin: 0; font-size: 12px; color: var(--muted); max-width: 62ch; }
.brief b {
  display: block; font: 500 11px/1.4 var(--sans); color: var(--ink-2);
}
.brief em { font-style: normal; font-weight: 550; color: var(--ink); }
.brief.solo { border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 12px; }

/* ── numbers ───────────────────────────────────────────────────────────── */

.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 0; border-top: 1px solid var(--line-soft);
}
.stat {
  background: var(--surface); padding: 10px 12px;
  display: flex; flex-direction: column; gap: 2px;
  border-right: 1px solid var(--line-soft); border-bottom: 1px solid var(--line-soft);
}
.stat b { font: 600 18px/1.15 var(--mono); font-variant-numeric: tabular-nums; }
.stat span { font: 500 11px/1.25 var(--sans); color: var(--muted); }
.stat small { font: 400 11px/1.35 var(--sans); color: var(--muted); }
.stat.is-good b { color: var(--up); }
.stat.is-bad b { color: var(--down); }
.stat.is-warn b { color: var(--warn); }

.pill {
  display: inline-flex; align-items: center; gap: 5px; padding: 1px 0;
  border-radius: 0; font: 500 11.5px/1.45 var(--sans); border: 0; background: transparent;
}
.pill.ok   { color: var(--up); }
.pill.bad  { color: var(--down); }
.pill.warn { color: var(--warn); }
.pill.flat { color: var(--muted); }
.pill.accent { color: var(--accent); }
.pill i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }

.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.num  { font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.dim  { color: var(--muted); }
.up   { color: var(--up); }
.down { color: var(--down); }
.plain { font-weight: 500; white-space: nowrap; }

/* ── tables ────────────────────────────────────────────────────────────── */

.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--line-soft); white-space: nowrap; }
thead th {
  position: sticky; top: 0; z-index: 1; background: var(--surface);
  font: 500 11px/1.35 var(--sans);
  color: var(--muted); border-bottom: 1px solid var(--line);
}
/* A grouping row above the headings, so a wide table reads as three or four
   labelled bands instead of a dozen equal columns.  Fixed height, because the
   headings below stick to exactly that offset when the body scrolls. */
thead tr.grouped th {
  top: 0; z-index: 2; height: 22px; padding: 0 10px;
  font: 500 11px/22px var(--sans);
  color: var(--ink-2); border-bottom: 1px solid var(--line-soft);
}
thead tr.grouped + tr th { top: 22px; }
th.gsep, td.gsep { border-left: 1px solid var(--line); }
thead th.sortable { cursor: pointer; user-select: none; }
thead th.sortable:hover { color: var(--ink); }
thead th[aria-sort]:not([aria-sort="none"]) { color: var(--ink); }
tbody tr:hover { background: var(--surface-2); }
tbody tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; }
td.wrap { white-space: normal; min-width: 22ch; }
.tall { max-height: 470px; overflow: auto; }
.best {
  display: inline-block; min-width: 3.4ch; padding: 1px 5px; border-radius: 2px;
  background: var(--best); color: var(--best-ink); font-weight: 600;
}
.odds-cell { font-family: var(--mono); font-variant-numeric: tabular-nums; font-weight: 550; }
.odds-cell.dim { font-weight: 400; }
.empty { padding: 22px 12px; text-align: center; color: var(--muted); font-size: 12.5px; }
/* Rows held back by chunked filling. Says how many, and is itself the way to get
   the next chunk without scrolling — so held-back rows are never silent. */
.chunk-more {
  display: block; width: 100%; padding: 10px 12px; border: 0;
  border-top: 1px solid var(--line-soft); background: var(--surface-2);
  color: var(--muted); font: 400 12px/1.4 var(--sans); text-align: center; cursor: pointer;
}
.chunk-more:hover { color: var(--ink); background: var(--surface-3, var(--surface-2)); }
.chunk-more:empty { display: none; }

/* ── odds screen ───────────────────────────────────────────────────────── */

.market-tabs {
  display: flex; flex-wrap: wrap; gap: 0; margin: 0 0 10px;
  border-bottom: 1px solid var(--line);
}
.market-tabs button {
  appearance: none; border: 0; border-bottom: 2px solid transparent;
  background: transparent; color: var(--muted); border-radius: 0;
  padding: 7px 12px 6px; margin-bottom: -1px; cursor: pointer;
  font: 500 12.5px/1.2 var(--sans);
}
.market-tabs button:hover { color: var(--ink); }
.market-tabs button.on {
  background: transparent; border-bottom-color: var(--ink); color: var(--ink);
}
.screen-toolbar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-bottom: 10px;
}
.screen-toolbar select,
.screen-toolbar input[type="search"] { width: auto; min-width: 140px; }
.screen-toolbar input[type="search"] { flex: 1 1 180px; min-width: 180px; }
.screen-toolbar .eyebrow { margin-right: 2px; }
.screen-toolbar .eyebrow.trail { margin-left: auto; }
.oj-board { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12.5px; }
.oj-board thead th {
  position: sticky; top: 0; z-index: 2; background: var(--surface);
  font: 500 11px/1.3 var(--sans);
  color: var(--muted); border-bottom: 1px solid var(--line); padding: 8px 8px;
}
/* The sportsbook picker's columns, led and marked — the same underline the
   market tabs use for the active pick. */
.oj-board th.book.picked, .oj-board td.book.picked { background: var(--surface-2); }
.oj-board th.book.picked { color: var(--ink); box-shadow: inset 0 -2px 0 var(--ink); }
.oj-board th.book, .oj-board td.book {
  text-align: center; min-width: 68px; border-left: 1px solid var(--line-soft);
}
.oj-board tbody tr:hover { background: var(--surface-2); }
.oj-board tbody tr { cursor: pointer; }
/* Deliberately NOT content-visibility: auto on these rows. It reads as the
   obvious fix for a 1,702-row board, and the computed style even reports it as
   applied — but CSS containment does not apply to internal table boxes, so it
   does nothing here. Measured on the live board: 1,085ms of layout before,
   1,196ms after. Rows are appended a chunk at a time instead (see
   ``fillInChunks``), which is what actually bounds the cost. */
.oj-board td { padding: 8px; border-bottom: 1px solid var(--line-soft); vertical-align: middle; }
.oj-game { min-width: 200px; }
.oj-game b { display: block; font: 600 13px/1.25 var(--sans); }
.oj-game .when { display: block; margin-top: 2px; font: 400 11px/1.3 var(--mono); color: var(--muted); }
.oj-side {
  display: flex; flex-direction: column; gap: 4px; align-items: stretch;
}
.oj-side .row {
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
  min-height: 20px;
}
.oj-side .lbl { color: var(--ink-2); font-size: 11.5px; white-space: nowrap; }
.oj-side .price { text-align: right; min-width: 4.5ch; }
.scroll-wrap { max-height: calc(100vh - 200px); overflow: auto; }
.oj-board th.oj-game, .oj-board td.oj-game {
  position: sticky; left: 0; z-index: 1; background: var(--surface);
  box-shadow: 1px 0 0 var(--line-soft);
}
.oj-board thead th.oj-game { z-index: 3; background: var(--surface); }
.oj-board tbody tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

/* ── the offshore switch ───────────────────────────────────────────────────
   A checkbox with its box drawn as a track, so the two states are legible at a
   glance from the rail without reading the label. */

.switch { display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 12px; }
.switch input {
  appearance: none; -webkit-appearance: none; margin: 0; flex: none;
  width: 30px; height: 17px; border-radius: 999px;
  background: var(--line); border: 1px solid var(--line-soft);
  position: relative; transition: background .12s ease;
}
.switch input::after {
  content: ''; position: absolute; top: 1px; left: 1px; width: 13px; height: 13px;
  border-radius: 50%; background: var(--surface); transition: transform .12s ease;
}
.switch input:checked { background: var(--warn); }
.switch input:checked::after { transform: translateX(13px); }
.switch input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.switch span { color: var(--muted); }
.switch input:checked ~ span { color: var(--ink); }

/* Marks a venue the reader cannot reach from the US, wherever one is named. */
.us-off {
  font-size: 10px; font-weight: 600; letter-spacing: .02em; text-transform: uppercase;
  padding: 1px 5px; border-radius: 999px; margin-left: 6px; white-space: nowrap;
  background: var(--warn-soft); color: var(--warn); border: 1px solid currentColor;
}
.src.is-us-off { border-style: dashed; }

/* ── source cards ──────────────────────────────────────────────────────── */

.sources { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; }
.src {
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 12px; display: flex; flex-direction: column; gap: 8px;
  color: inherit; text-decoration: none;
}
.src:hover { border-color: var(--muted); background: var(--surface-2); }
/* The picked book's own cards, marked rather than the rest being hidden —
   Books deliberately lists every venue whatever the sportsbook picker says. */
.src.picked { border-left: 3px solid var(--ink); padding-left: 10px; }
.src-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.src-top b { font: 600 13.5px/1.25 var(--sans); }
.src-top code { display: block; font: 400 10.5px/1.45 var(--mono); color: var(--muted); word-break: break-all; }
.src-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px 10px; }
.src-grid div { display: flex; flex-direction: column; }
.src-grid b { font: 600 14px/1.2 var(--mono); font-variant-numeric: tabular-nums; }
.src-grid span { font: 500 11px/1.3 var(--sans); color: var(--muted); }
.src p { margin: 0; font-size: 12px; color: var(--ink-2); }

/* ── pipeline ──────────────────────────────────────────────────────────── */

.flow { display: flex; flex-wrap: wrap; align-items: stretch; gap: 6px; }
.flow-step { flex: 1 1 140px; border: 1px solid var(--line); border-radius: var(--radius); padding: 9px 10px; background: var(--surface); }
.flow-step b { display: block; font: 600 16px/1.2 var(--mono); font-variant-numeric: tabular-nums; }
.flow-step span { font: 500 11px/1.3 var(--sans); color: var(--muted); }
.flow-step small { display: block; margin-top: 2px; font-size: 11.5px; color: var(--ink-2); }
.flow-arrow { align-self: center; color: var(--muted); font-family: var(--mono); }

/* ── coverage grid ─────────────────────────────────────────────────────── */

.cov td.cell { text-align: center; font-family: var(--mono); font-variant-numeric: tabular-nums; padding: 5px 8px; }
.cov td.cell.zero { color: color-mix(in srgb, var(--muted) 50%, transparent); }
.cov tbody tr { cursor: pointer; }
.cov tbody tr.sel { background: var(--surface-2); }
.heat { display: inline-block; min-width: 34px; padding: 1px 5px; border-radius: 2px; }

/* ── charts ────────────────────────────────────────────────────────────── */

.chart { width: 100%; height: auto; display: block; }
.spark { display: block; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font: 400 11.5px/1.4 var(--sans); color: var(--muted); }
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.swatch { width: 8px; height: 8px; border-radius: 1px; }

/* ── misc ──────────────────────────────────────────────────────────────── */

.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.controls select, .controls input { width: auto; min-width: 130px; }
.note { margin: 8px 0 0; font-size: 12px; color: var(--muted); max-width: 88ch; }
.note code { font-family: var(--mono); }
.bars { display: flex; flex-direction: column; gap: 4px; }
.bar-row { display: grid; grid-template-columns: minmax(120px, 210px) 1fr 56px; gap: 10px; align-items: center; font-size: 12px; }
.bar-row .track { height: 6px; border-radius: 2px; background: var(--surface-2); overflow: hidden; }
.bar-row .fill { height: 100%; background: var(--muted); border-radius: 2px; }
.bar-row .val { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--ink-2); }

.kv { display: grid; grid-template-columns: max-content 1fr; gap: 3px 14px; font-size: 12.5px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; font-family: var(--mono); }

.gloss { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px 24px; }
.gloss > div { display: flex; flex-direction: column; gap: 2px; }
.gloss dt { font: 600 12.5px/1.3 var(--sans); color: var(--ink); }
.gloss dd { margin: 0; font-size: 12px; color: var(--ink-2); max-width: 52ch; }

.notice {
  display: none; gap: 8px; align-items: flex-start; margin-bottom: 14px;
  padding: 8px 10px; border: 1px solid var(--line);
  border-radius: var(--radius); background: var(--warn-soft); color: var(--ink-2); font-size: 12px;
}
.notice.on { display: flex; }
.notice b { color: var(--warn); font-weight: 600; }

@media (max-width: 860px) {
  .shell { grid-template-columns: minmax(0, 1fr); }
  .rail { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
  .nav { flex-direction: row; flex-wrap: wrap; }
  main { padding: 0 14px 56px; }
}
"""
BODY = """
<div class="shell">
  <aside class="rail">
    <div class="brand">
      <b>Line shop</b>
      <span id="brand-sub">odds board</span>
    </div>

    <nav class="nav" id="nav" aria-label="Sections">
      <a href="#arb">Arbitrage <i id="nav-arb"></i></a>
      <a href="#screen">Odds <i id="nav-screen"></i></a>
      <a href="#promos">Promos <i id="nav-promos"></i></a>
      <a href="#bets">My bets <i id="nav-bets"></i></a>
      <a href="#events">Games <i id="nav-events"></i></a>
      <details class="nav-more" id="nav-more">
        <summary>More</summary>
        <div class="nav">
          <a href="#history">History <i id="nav-history"></i></a>
          <a href="#run">This scrape</a>
          <a href="#odds">All prices <i id="nav-odds"></i></a>
          <a href="#sources">Books <i id="nav-sources"></i></a>
          <a href="#sports">Coverage <i id="nav-sports"></i></a>
          <a href="#movement">Movement <i id="nav-move"></i></a>
          <a href="#quality">Checks <i id="nav-quality"></i></a>
          <a href="#raw">Saved pages <i id="nav-raw"></i></a>
          <a href="#overview">How to use</a>
          <a href="#glossary">Glossary</a>
          <a href="#schema">Schema</a>
        </div>
      </details>
    </nav>

    <div class="rail-block" id="scrape-block">
      <label>Scrape odds</label>
      <select id="scrape-scope" title="What to ask the venues for">
        <option value="league:MLB" selected>MLB baseball (fast)</option>
        <option value="sport:baseball">All baseball</option>
        <option value="all">Everything (slower)</option>
      </select>
      <span class="eyebrow">States (detected one is pre-checked)</span>
      <div class="state-picks" id="scrape-states">
        <label><input type="checkbox" value="IL">IL</label>
        <label><input type="checkbox" value="PA">PA</label>
        <label><input type="checkbox" value="NJ">NJ</label>
        <label><input type="checkbox" value="DC">DC</label>
      </div>
      <button type="button" id="scrape-btn" class="scrape-btn">Scrape now</button>
      <div class="scrape-progress" id="scrape-progress" aria-live="polite">
        <div class="scrape-bar" aria-hidden="true"><i id="scrape-bar-fill"></i></div>
        <span class="scrape-msg" id="scrape-msg">Scraping…</span>
        <span class="scrape-meta" id="scrape-meta"></span>
      </div>
      <span class="rail-foot" id="scrape-status">Open via --serve to enable scraping.</span>
    </div>

    <div class="rail-block" id="promo-scrape-block">
      <label>Scrape bonuses</label>
      <button type="button" id="promo-scrape-btn" class="scrape-btn">Scrape promos</button>
      <div class="scrape-progress" id="promo-scrape-progress" aria-live="polite">
        <div class="scrape-bar" aria-hidden="true"><i id="promo-scrape-bar-fill"></i></div>
        <span class="scrape-msg" id="promo-scrape-msg">Scraping promos…</span>
        <span class="scrape-meta" id="promo-scrape-meta"></span>
      </div>
      <span class="rail-foot" id="promo-scrape-status">Open via --serve to scrape bonuses.</span>
    </div>

    <div class="rail-block">
      <label for="sport-pick">Sport</label>
      <select id="sport-pick"></select>
      <span class="rail-foot" id="sport-meta" style="margin:0"></span>
    </div>

    <div class="rail-block">
      <label for="book-pick">Sportsbook</label>
      <select id="book-pick" aria-label="Narrow the page to one sportsbook"></select>
      <span class="rail-foot" id="book-meta" style="margin:0"></span>
    </div>

    <div class="rail-block">
      <!-- The block heading deliberately carries no `for`. Pointing it at the
           checkbox made it win the accessible name, so the switch announced
           itself as "Where you can bet" — the section it sits in — rather than
           as what flipping it does. -->
      <label>Where you can bet</label>
      <!-- No `for` here either, and this one is not cosmetic: a label that both
           contains a control *and* points at it fires its activation behaviour on
           top of the control's own, so a real click toggled twice and landed back
           where it started. Containment alone is the association; `aria-label`
           carries the name because the heading above is a bare <label>. -->
      <label class="switch">
        <input type="checkbox" id="offshore-toggle"
               aria-label="Include books you can't bet from the US">
        <span>Include books you can't bet from the US</span>
      </label>
      <span class="rail-foot" id="offshore-meta" style="margin:0"></span>
    </div>

    <div class="rail-block">
      <!-- Display only. Nothing here changes what was detected, what is
           takeable, or how anything is ranked — it adds a second figure beside
           the pre-tax one, and at the default of no tax it adds nothing at all.
           Informational, not tax advice. -->
      <label for="tax-fed">After tax</label>
      <select id="tax-fed" aria-label="Federal rate on taxable gambling income"></select>
      <select id="tax-state" aria-label="State rate on gross winnings"></select>
      <!-- Same two rules as the switch above: the block heading is a bare
           <label> so it cannot steal the control's accessible name, and this
           label contains the checkbox rather than pointing at it. -->
      <label class="switch">
        <input type="checkbox" id="tax-cap"
               aria-label="Deduct only 90% of losses">
        <span>90% loss-deduction cap</span>
      </label>
      <span class="rail-foot" id="tax-meta" style="margin:0"></span>
    </div>

    <div class="rail-foot" id="built"></div>
  </aside>

  <main>
    <header class="masthead">
      <div>
        <h1 id="page-title">Arbitrage</h1>
        <p id="scrape-stamp" class="scrape-stamp"></p>
        <p id="lede"></p>
      </div>
      <div id="masthead-pills" class="controls"></div>
    </header>

    <nav class="crumbs" id="crumbs" aria-label="Where you are"></nav>

    <div class="notice" id="run-notice"></div>

    <section id="arb">
      <header>
        <h2>Arbitrage</h2>
        <p>Risk-free cross-book positions in the latest scrape — same detector as <code>collector arb</code>.</p>
      </header>

      <div class="card">
        <div class="card-head">
          <h3>Latest scrape</h3>
          <span class="eyebrow" id="arb-summary">scanning…</span>
        </div>
        <div class="card-body flush">
          <div class="stats" id="arb-stats"></div>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-head">
          <h3>Positions</h3>
          <span class="eyebrow" id="arb-note">latest scrape only</span>
        </div>
        <div class="card-body" id="arb-list"></div>
      </div>

      <details class="fold" id="arb-rejected-fold">
        <summary>Why other markets were refused <span class="eyebrow">near-misses, optional</span></summary>
        <div class="card-body" id="arb-rejected"></div>
      </details>
    </section>

    <section id="screen">
      <header>
        <h2>Odds</h2>
        <p>Games down the left, books across. American odds; highlighted cell is the best takeable price on that line (Open is context only).</p>
      </header>

      <div class="market-tabs" id="market-tabs" role="tablist" aria-label="Market"></div>

      <div class="screen-toolbar">
        <label class="eyebrow" for="league-pick">League</label>
        <select id="league-pick" aria-label="Filter by league">
          <option value="">every league</option>
        </select>
        <span class="eyebrow" id="screen-note">latest scrape</span>
      </div>

      <div class="card">
        <div class="card-body flush scroll-wrap">
          <div id="odds-screen" class="scroll"></div>
        </div>
      </div>
    </section>

    <section id="promos">
      <header>
        <h2>Promos</h2>
        <p>Signup bonuses, free bets, boosts, and other public free-EV offers — separate from priced odds.</p>
      </header>

      <div class="card">
        <div class="card-head">
          <h3>Latest promo scrape</h3>
          <span class="eyebrow" id="promo-summary">no scrape yet</span>
        </div>
        <div class="card-body flush">
          <div class="stats" id="promo-stats"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Campaign</h3>
          <span class="eyebrow" id="promo-campaign-note">offers ranked by expected value</span>
        </div>
        <div class="card-body flush">
          <div class="scroll"><table class="campaign-table" id="promo-campaign"></table></div>
        </div>
      </div>

      <div class="promo-toolbar">
        <label>
          <span class="eyebrow" for="promo-kind">Kind</span>
          <select id="promo-kind" aria-label="Filter by promo kind">
            <option value="">every kind</option>
          </select>
        </label>
        <label>
          <span class="eyebrow" for="promo-source">Book</span>
          <select id="promo-source" aria-label="Filter by book">
            <option value="">every book</option>
          </select>
        </label>
        <label>
          <span class="eyebrow" for="promo-region">Region</span>
          <select id="promo-region" aria-label="Filter by eligible region">
            <option value="">all regions</option>
          </select>
        </label>
        <label style="flex:1; min-width:180px">
          <span class="eyebrow" for="promo-q">Search</span>
          <input type="search" id="promo-q" placeholder="title, summary, or description" autocomplete="off"/>
        </label>
        <span class="eyebrow" id="promo-note">latest promo scrape</span>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Offers</h3>
          <span class="eyebrow" id="promo-list-note"></span>
        </div>
        <div class="card-body" id="promo-list"></div>
      </div>

      <details class="fold" id="promo-health-fold">
        <summary>Per-book promo health <span class="eyebrow">last scrape</span></summary>
        <div class="card-body">
          <div class="promo-health" id="promo-health"></div>
        </div>
      </details>
    </section>

    <section id="bets">
      <header>
        <h2>My bets</h2>
        <p>What you actually placed, and how it settled. Stored in its own file
        (<code>data/bets.sqlite3</code>) — never touched by a scrape, never deleted by one.</p>
      </header>

      <div class="card">
        <div class="card-head">
          <h3>Bankroll</h3>
          <span class="eyebrow" id="bets-summary">nothing logged yet</span>
        </div>
        <div class="card-body flush">
          <div class="stats" id="bets-stats"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Log a bet by hand</h3>
          <span class="eyebrow" id="bets-add-note">or use “Log this bet” on Arbitrage</span>
        </div>
        <div class="brief">
          <p><b>the fast way is the other one</b> Every arbitrage card has a Log this
          bet button that carries the teams, market, books, prices and stakes straight
          across. Use this form for a bet placed somewhere the scrape did not see.</p>
        </div>
        <div class="card-body">
          <div class="bl-form">
            <label class="bl-field"><span>Book</span>
              <input id="bl-book" list="bl-books" placeholder="fanduel" autocomplete="off"/>
              <datalist id="bl-books"></datalist>
            </label>
            <label class="bl-field wide"><span>What you bet</span>
              <input id="bl-what" placeholder="Cubs ML vs Cardinals" autocomplete="off"/>
            </label>
            <label class="bl-field"><span>US odds</span>
              <input id="bl-odds" class="odds" inputmode="numeric" placeholder="-110"/>
            </label>
            <label class="bl-field"><span>Stake $</span>
              <input id="bl-stake" class="money" inputmode="decimal" placeholder="50"/>
            </label>
            <label class="bl-field"><span>Status</span>
              <select id="bl-status"></select>
            </label>
            <button type="button" class="bl-btn primary" id="bl-add">Log it</button>
          </div>
          <p class="bl-msg" id="bl-add-msg"></p>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Placed</h3>
          <span class="eyebrow" id="bets-list-note">newest first</span>
        </div>
        <div class="card-body" id="bets-list"></div>
      </div>

      <details class="fold" id="bets-books-fold">
        <summary>By book <span class="eyebrow">where the money is</span></summary>
        <div class="card-body">
          <div class="bl-books" id="bets-books"></div>
        </div>
      </details>
    </section>

    <section id="history">
      <header>
        <h2>History</h2>
        <p>Past scrapes. Pick one to inspect niche panels below; Arbitrage, Odds, and Games always show the latest.</p>
      </header>
      <div class="card">
        <div class="card-head">
          <h3>Saved scrapes</h3>
          <span class="eyebrow" id="run-meta">newest on top</span>
        </div>
        <div class="card-body">
          <label class="sr-only" for="run-pick">Which scrape to inspect</label>
          <select id="run-pick" aria-label="Which scrape to inspect"></select>
          <div id="run-list" class="run-list" role="listbox" aria-label="Collections by time" style="margin-top:10px"></div>
        </div>
      </div>
    </section>

    <section id="overview">
      <header>
        <h2>How to use</h2>
        <p>Scrape → Arbitrage for free-money positions → Odds to line-shop the latest board.</p>
      </header>

      <div class="card how-card">
        <div class="card-head"><h3>How to use this</h3><span class="eyebrow">three steps</span></div>
        <div class="card-body flush">
          <div class="tiles how-tiles">
            <div class="tile">
              <span class="ord">1</span>
              <b>Scrape now</b>
              <p>Left sidebar. Pulls live prices from the books into this computer.</p>
            </div>
            <div class="tile">
              <span class="ord">2</span>
              <b>Check arbitrage</b>
              <p>Front page lists risk-free multi-book positions with stakes and payouts — latest scrape only.</p>
            </div>
            <div class="tile">
              <span class="ord">3</span>
              <b>Line-shop the board</b>
              <p>Odds: games × books. Highlighted cell is the best takeable price for that side.</p>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3>Games in this scrape</h3>
          <span class="eyebrow" id="browse-games-note">click one for every market</span>
        </div>
        <div class="card-body">
          <div id="browse-games" class="game-grid"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>At a glance</h3><span class="eyebrow">this scrape only</span></div>
        <div class="card-body flush"><div class="stats" id="home-stats"></div></div>
      </div>

      <p class="dim home-more">
        <a href="#arb">Arbitrage</a> ·
        <a href="#screen">Odds</a> ·
        <a href="#history">History</a> ·
        <a href="#glossary">Glossary</a></p>
    </section>

    <section id="run">
      <header>
        <h2>This scrape</h2>
        <p>One scrape is one pass that asked every book for prices. Use the sidebar to
        switch between scrapes you already saved.</p>
      </header>

      <div class="card">
        <div class="card-head"><h3>The headline numbers</h3><span class="eyebrow">this scrape only</span></div>
        <div class="brief">
          <p><b>a price vs a bet</b> One <em>bet</em> — say who wins tonight&rsquo;s game at one
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
        <h2>Who we asked</h2>
        <p>Each card is one sportsbook, exchange, or prediction market we pulled from.
        Click a card for what it returned this scrape.</p>
      </header>
      <div class="brief solo">
        <p><b>what each card shows</b> What kind of venue it is, what it charges, how much
        it returned this time, how long it took, and whether the reply was byte-for-byte
        the same as last time.</p>
        <p><b>click a card</b> to open that venue on its own — what it published, what it
        skipped, and every page saved from it.</p>
      </div>
      <p class="note" id="sources-note"></p>
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
        <h2>Games</h2>
        <p>Click a game for every market side by side. Prefer Odds for the full board.
        Sport lives in the sidebar. <b>Search a team to find one game</b> &mdash; type
        &ldquo;Dodgers&rdquo; and only their games remain &mdash; or narrow by league,
        feed and book.</p>
      </header>
      <div class="screen-toolbar" id="events-toolbar">
        <label class="eyebrow" for="events-q">Search</label>
        <input type="search" id="events-q" placeholder="team or game&hellip;" aria-label="Search games by team" />
        <label class="eyebrow" for="events-league">League</label>
        <select id="events-league" aria-label="Filter games by league">
          <option value="">every league</option>
        </select>
        <label class="eyebrow" for="events-feed">Feed</label>
        <select id="events-feed" aria-label="Filter games by feed type"
                title="First-party is the venue's own feed. Republished is somebody else's board — a copy, never a counterparty.">
          <option value="">every feed</option>
          <option value="first">first-party only</option>
          <option value="republished">republished only</option>
        </select>
        <label class="eyebrow" for="events-book">Book</label>
        <select id="events-book" aria-label="Filter games by book">
          <option value="">any book</option>
        </select>
        <span class="eyebrow trail" id="events-filter-note"></span>
      </div>
      <div class="card">
        <div class="card-head">
          <h3>Pick a game</h3>
          <span class="eyebrow" id="events-games-note">click to compare books</span>
        </div>
        <div class="card-body flush"><div id="events-games" class="game-grid"></div></div>
      </div>
      <div class="card">
        <div class="card-head">
          <h3>Same games, as a coverage grid</h3>
          <div class="controls">
            <label class="eyebrow" for="cov-mode">Break down</label>
            <select id="cov-mode">
              <option value="source">by book</option>
              <option value="market">by kind of bet</option>
            </select>
          </div>
        </div>
        <div class="brief">
          <p><b>optional detail</b> One row per game. Darker cells mean more prices from that
          book. A dash means that book had nothing for the game.</p>
          <p><b>click any row</b> for the same side-by-side view as the cards above.</p>
        </div>
        <div class="card-body flush scroll"><table class="cov" id="coverage"></table></div>
      </div>
    </section>

    <section id="fixture">
      <div class="headline">
        <b id="event-title">Pick a game</b>
        <span id="event-sub"></span>
      </div>
      <div class="card">
        <div class="card-head"><h3>Every price for this game</h3><span class="eyebrow" id="event-count"></span></div>
        <div class="brief">
          <p><b>read left to right</b> Each row is one bet, then each book&rsquo;s
          American odds for that bet.</p>
          <p><b>highlight means best price</b> for that row. Click a row to zoom into one bet —
          every book, and whether the price moved.</p>
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
        <p>Every price from this scrape in one table. <b>Search a team to find one
        game</b> &mdash; the box below matches team, fixture, league and kind of bet
        &mdash; or use Games if you want one matchup laid out side by side.</p>
      </header>
      <div class="card">
        <div class="card-head">
          <div class="controls">
            <label class="eyebrow" for="q">Search</label>
            <input type="search" id="q" placeholder="team, game, kind of bet&hellip;" aria-label="Search prices by team, game or kind of bet" />
            <select id="f-feed" aria-label="Filter prices by feed type"
                    title="First-party is the venue's own feed. Republished is somebody else's board — a copy, never a counterparty.">
              <option value="">every feed</option>
              <option value="first">first-party only</option>
              <option value="republished">republished only</option>
            </select>
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
        <h2>Did prices move?</h2>
        <p>Same bet across scrapes. If a number changed, the feed is alive — not a stuck
        old page.</p>
      </header>
      <div class="card">
        <div class="card-head"><h3>Every scrape so far</h3><span class="eyebrow">prices found, and how long fetching took</span></div>
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
        <h2>Plain-English glossary</h2>
        <p>Skip this until you need it. The game cards work without knowing any of these words.</p>
      </header>
      <div class="card">
        <div class="card-head"><h3>Price numbers in one minute</h3><span class="eyebrow">optional</span></div>
        <div class="card-body flush">
          <div class="tiles">
            <div class="tile">
              <span class="ord">1 &middot; the price</span>
              <b class="big">2.30 &rarr; $230 back</b>
              <p>Bet $100 at 2.30 and a win returns $230 total: your $100 back, plus $130 profit.</p>
              <code>a US book writes this as +130</code>
            </div>
            <div class="tile">
              <span class="ord">2 &middot; the chance</span>
              <b class="big">1 &divide; 2.30 &asymp; 43%</b>
              <p>Flip the price and you get roughly how likely the book thinks it is.</p>
              <code>&minus;150 means stake $150 to profit $100</code>
            </div>
            <div class="tile">
              <span class="ord">3 &middot; the book's cut</span>
              <b class="big">43% + 61% = 104%</b>
              <p>Both sides add up to over 100%. The extra is the sportsbook&rsquo;s margin.</p>
              <code>under 100% would mean free money</code>
            </div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>The four kinds of bet this page stores</h3><span class="eyebrow">nothing else</span></div>
        <div class="card-body flush">
          <div class="tiles">
            <div class="tile"><h4>Who wins</h4>
              <p>Pick the winning team. Nothing else matters.</p><code>Reds win</code></div>
            <div class="tile"><h4>Winner with a handicap</h4>
              <p>One team starts ahead or behind so a mismatch is closer.</p>
              <code>Reds win by 2 or more</code></div>
            <div class="tile"><h4>Combined total</h4>
              <p>Both scores added, over or under a number (runs, goals, points&hellip;).</p>
              <code>9 or more runs in the game</code></div>
            <div class="tile"><h4>One side&rsquo;s total</h4>
              <p>Just one team&rsquo;s score, over or under a number.</p>
              <code>Reds score 5 or more</code></div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>Word list</h3><span class="eyebrow">sportsbook phrasing in tooltips</span></div>
        <div class="card-body"><div class="gloss" id="glossary-list"></div></div>
      </div>
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

const DATA = (() => {
  const island = document.getElementById('report-data');
  const parsed = JSON.parse(island.textContent);
  // Drop the source text once it is parsed. It is the largest single thing on the
  // page — 15.6MB of JSON for a 16-scrape dashboard — and holding both the text and
  // the object doubles that for the life of the tab, for a string nothing reads
  // again. Nothing does: this is the only reference to the island at runtime.
  island.remove();
  return parsed;
})();
const S = DATA.strings;
const Q = DATA.quotes;              // { columns, rows } — rows across several runs
const COL = {};
Q.columns.forEach((name, i) => { COL[name] = i; });
const NET_ODDS = 'net_decimal_odds';
let PROMOS = DATA.promos
  || { run: null, offers: [], health: [], kinds: [], plans: {}, plan_meta: null };

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
const SOURCE_INFO_BY_STATE = new Map(Object.entries(DATA.sources_by_jurisdiction || {})
  .map(([state, entries]) => [state, new Map((entries || []).map((s) => [s.key, s]))]));
function sourceInfo(key) {
  const run = typeof runById !== 'undefined' ? runById.get(currentRunId) : null;
  // The runs payload spells a legacy run's jurisdiction "UNKNOWN"; the source
  // map spells the same column "".  Without the normalization the lookup
  // missed, fell back to the top-level array built for the *latest* run's
  // state, and a legacy run's badges were graded by whatever run happened to
  // be newest — the exact fallback the "" column was added to remove, alive
  // one spelling away.
  const state = run && (run.jurisdiction === 'UNKNOWN' ? '' : (run.jurisdiction || ''));
  const scoped = run && SOURCE_INFO_BY_STATE.get(state);
  return (scoped && scoped.get(key)) || SOURCE_INFO.get(key) || {};
}
const book = (key) => sourceInfo(key).label || key;

// What kind of counterparty each venue is.  Not decoration: it decides whether the
// quoted price is the price you are paid, whether there is a real amount behind it,
// and what happens to the stake if the game is called off.
const venueKind = (key) => sourceInfo(key).kind || 'sportsbook';
const commissionOf = (key) => sourceInfo(key).commission || '';
const charges = (key) => Boolean(commissionOf(key));
/** Consensus / opening columns shown for context — never "best" and never arb. */
const isViewOnly = (key) => Boolean(sourceInfo(key).view_only);
/** Whether the venue will not take a bet from somebody sitting in the US.
 *
 *  A different question from view-only, and the reason it is a separate flag: a
 *  republished mirror is unstakeable because it is a copy of somebody else's
 *  board, while these are real order books that refuse a US customer. Pinnacle
 *  is the sharpest line on the page and worth reading even when it cannot be
 *  bet, so the page filters on this rather than dropping it. */
const isUsUnavailable = (key) => Boolean(sourceInfo(key).us_unavailable);
/** Whether these rows are a copy of somebody else's board rather than the
 *  venue's own feed.
 *
 *  Read off the payload's `diagnostic_only`, which the server sets from
 *  `registry.REPUBLISHED_SOURCE_KEYS` — the same set the arbitrage detector
 *  refuses as a leg — so the filter and the detector cannot disagree about what
 *  counts as first-party. Not the same question as view-only or US-unavailable,
 *  and deliberately not inferred from the key's prefix: `an_`/`vi_` is a naming
 *  convention, and a filter built on one would quietly mis-sort the first
 *  republisher that did not follow it. */
const isRepublished = (key) => Boolean(sourceInfo(key).diagnostic_only);
/** `"first"` or `"republished"` — the values the two feed pickers use. */
const feedKind = (key) => (isRepublished(key) ? 'republished' : 'first');
/** Whether the venue's rows exist only because somebody offered liquidity — an
 *  exchange or a prediction market, as the source registry defines it. A sportsbook
 *  quotes both sides itself, so its two sides summing below 1.0 means the rows are
 *  mispaired, not that it is paying out more than it takes. */
const orderDriven = (key) => Boolean(sourceInfo(key).order_driven);
/** Which counterparty a feed reads — `draftkings` for all three DraftKings
 *  feeds, `''` for a consensus column that names no venue at all.
 *
 *  Read off the payload's `brand`, which the server sets from
 *  `betlinks.book_for` — the same fold that decides where a bet link points —
 *  so the sportsbook picker and the bet slip cannot disagree about which
 *  company a row belongs to. Never derived from the `an_`/`vi_` spelling of
 *  the key, for the same reason `isRepublished` refuses the prefix. */
const brandOf = (key) => sourceInfo(key).brand || '';
const BRAND_NAME = new Map((DATA.sources || [])
  .map((s) => [s.brand || s.key, s.brand_label || s.label || s.key]));
const brandLabel = (b) => BRAND_NAME.get(b) || b;
/** The one place the sportsbook picker's comparison is spelled. Every panel
 *  asks this rather than comparing `brandOf(x)` with the picked brand itself,
 *  so the filter cannot mean different things on different tabs. */
const keepBrand = (key) => !currentBrand || brandOf(key) === currentBrand;
/** Which counterparty a hand-typed ledger book belongs to. Exact arms only: a
 *  registered source key folds to its brand; any other string is its own
 *  bucket, shown verbatim (a string that happens to be a brand key IS that
 *  brand's bucket). Never a guess — no lowercasing, no prefixes, no substring
 *  match — because the ledger records real money, and "DK app" silently
 *  becoming DraftKings would re-attribute profit to the wrong book. */
const ledgerBrand = (name) => brandOf(name) || String(name || '');
/** The picker's comparison for the ledger's key space — `keepBrand`'s third
 *  twin, spelled once so three key spaces cannot drift into three meanings. */
const keepLedgerBook = (name) => !currentBrand || ledgerBrand(name) === currentBrand;

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
/** Can this scoring window end level at all?  Read from the same table
 * ``src/vocab.py`` states it in, because "or draw" is a claim about the sport
 * and printing it where a tie cannot happen describes an outcome that does not
 * exist. */
function tiePossible(sport, period) {
  const facts = SPORT_FACTS[sport];
  const window = facts && facts.periods && facts.periods[period];
  return !!(window && window.tie_possible);
}

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
// The status half of that rule lived in one of the two call sites, not in here.
// ``validation.py`` requires ``len(active) == len(rows)``: a price that is
// showing is not being offered, so a market with a suspended leg is exactly as
// unsummable as one with a missing leg. The quality strip filtered to active
// rows before calling; the bet panel did not, and read a book "keeping −17.9%"
// off a three-way whose draw was suspended — on the same page whose quality
// strip had correctly excluded that market. One rule, in one place, so the two
// panels cannot disagree about the same market again.
function sumsToAMargin(rows) {
  if (rows.length < 2) return false;
  if (rows.some((r) => str(r[COL.status]) !== 'active')) return false;
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
/* A skip reason as the reader sees it: the two panels with a skips table
   spelled this chain inline, so a new prefix had to be added in both. */
const skipReasonLabel = (reason) =>
  String(reason).replace(/^criterion:/, '').replace(/^matchup_type:/, '').replace(/_/g, ' ');

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

/** Does the *backed* side half-win at the landing point, or half-lose?
 *
 * A quarter line splits the stake across the two neighbouring half-lines, so at
 * the one score between them one half pushes and the other settles — and which
 * one settles depends on which side of the quarter the line sits.  On the
 * giving side (a negative spread, or an over) a ``.75`` line half-*wins* and a
 * ``.25`` line half-*loses*; on the receiving side (a positive spread, or an
 * under) it is the other way round.
 *
 * This was assumed rather than computed: both halves of every quarter line were
 * told the middle outcome "pays half".  ``src/arb.py`` gives one side
 * ``HALF_WIN`` and the other ``HALF_LOSE`` — never both — so on 127 of the 254
 * quarter-line rows in the committed captures the page described a payout to
 * the reader who was, at that score, losing half the stake.  The two sentences
 * sat next to each other on the same fixture panel.
 */
const quarterHalfWins = (line, giving) => {
  const quarter = Math.abs(Math.abs(line % 1) - 0.75) < 1e-9;
  return giving ? quarter : !quarter;
};

/** The one score a quarter line lands on: always a whole number. */
const quarterLanding = (line) => Math.abs(Math.round(line));

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
      // Half the stake on each neighbouring line, so at the one score between
      // them half the stake pushes and half settles.  Which way it settles is
      // computed, not assumed — see ``quarterHalfWins``.
      const [lo, hi] = quarterHalves(size);
      const at = quarterLanding(bet.line);
      const wins = quarterHalfWins(bet.line, bet.line < 0);
      const verb = wins ? 'pays half' : 'loses half the stake';
      if (bet.line < 0) {
        // Full win needs to beat the *upper* half-line, so the threshold is one
        // clear of it: at −0.75 the halves are −0.5 and −1.0, a one-goal win is
        // the split, and only a two-goal win collects both.  This said "win by
        // 1 or more" beside "a 1-goal win pays half" — two claims about the same
        // score, one of them wrong.
        // A "0-goal win" is a draw; say the word rather than describe it.
        const landing = at === 0 ? 'a draw' : `a ${at}-${unit} win`;
        text = `${picked} win by ${Math.floor(hi) + 1} or more; ${landing} ${verb}`;
      } else {
        // Mirror image: everything up to one short of the *lower* half-line is a
        // full win, and ``at`` is a loss by that many — or a draw, when it is 0.
        // ``room === 0`` means a level game is still a *full* win — both
        // half-lines cover it — so the sentence has to say so where that can
        // happen.  "AWAY win" alone silently dropped the draw from the winning
        // set on every +0.75 and +1.25 line.
        const room = Math.ceil(lo) - 1;
        const core = room > 0 ? `${picked} win, or ${margin(room)}`
          : room === 0 && tiePossible(bet.sport, bet.period) ? `${picked} win, or draw`
          : `${picked} win`;
        const landing = at === 0 ? 'a draw' : `a ${at}-${unit} loss`;
        text = `${core}; ${landing} ${verb}`;
      }
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
      // Same split, same landing score, and the same rule for which half of the
      // stake settles: an over is the giving side, an under the receiving one.
      const [lo, hi] = quarterHalves(n);
      const at = quarterLanding(n);
      const over = bet.selection === 'over';
      const verb = quarterHalfWins(n, over) ? 'pays half' : 'loses half the stake';
      text = over
        ? `${scorer} score ${Math.floor(hi) + 1} ${units} or more; exactly ${at} ${verb}`
        : `${scorer} score ${Math.ceil(lo) - 1} ${units} or fewer; exactly ${at} ${verb}`;
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

/* ── chunked row filling ────────────────────────────────────────────────────
   How many rows go in before the reader has to scroll for more. Comfortably
   more than a tall screen holds, so the first chunk is never visibly short. */
const ROW_CHUNK = 120;

/** Fill `tbody` with `make(row)`, a chunk at a time, appending as the reader
 *  scrolls toward the end.
 *
 *  The alternative — every row up front — is what made the odds board cost
 *  399,055 DOM nodes and 1,143ms of layout: 1,702 games across 22 books, in a
 *  table 131,883px tall, of which a screen shows about ten rows. Building the
 *  markup for a row is not free either, so a chunk that is never reached is
 *  never built.
 *
 *  `content-visibility: auto` would be the tidier fix and does not work: CSS
 *  containment does not apply to internal table boxes. This does.
 *
 *  Rows held back are always *stated*, and always reachable by a click as well as
 *  by scrolling. A page that quietly showed 120 of 1,702 games would be a worse
 *  bug than the slowness this fixes — the count in the nav would be right and the
 *  board under it would be a lie — so the scroll trigger is never the only way to
 *  reach a row. */
/* The "show more" control has to sit *beside* its container, because a button is
   not valid inside a table — which means replacing the container does not take the
   control with it. ``table`` replaces its node outright when a filter empties it,
   so the control was surviving into the empty state and claiming "Showing 120 of
   1,702 rows" above the words "No games match these filters", and a second one
   appeared every time the filter was cleared again.

   What fixes that is dropping the control *before* anything replaces the node —
   see the call at the top of ``table``. The id-keyed map is a second net for a
   caller that replaces a node without going through ``table``; every current
   caller does, so removing the map changes nothing observable today. */
const chunkControls = new Map();

function dropChunkControl(host) {
  if (!host) return;
  // Retire every fill on this host, here rather than in ``fillInChunks``: dropping the
  // control IS the retirement, and most callers drop it and then return without
  // starting a new fill. `disconnect()` unobserves every target but is not specified to
  // discard records already queued for delivery, so an observer retired by a filter
  // change can still call back once — and for the two game grids the host is its own
  // tbody, so nothing detaches under a late append. Bumping this in ``fillInChunks``
  // instead covered only the refill case and left every empty state open: filtering
  // Games down to nothing, then one queued record, put 120 cards of the previous slate
  // under "No games match these filters" — and because the callback re-arms itself,
  // further intersections brought back 1,582 of them.
  host.__chunkGen = (host.__chunkGen || 0) + 1;
  if (host.__chunkIO) { host.__chunkIO.disconnect(); host.__chunkIO = null; }
  const existing = host.id ? chunkControls.get(host.id) : host.__chunkMore;
  if (existing) {
    existing.remove();
    if (host.id) chunkControls.delete(host.id);
  }
  host.__chunkMore = null;
}

/** Empty a region by id, taking any chunk control with it.
 *
 *  The one way to blank a region that might be chunked. A bare ``innerHTML = ''``
 *  leaves the control behind — it is a *sibling*, so emptying the table cannot take it
 *  with it — and leaves the retired fill able to append into what was just cleared.
 *  Both failures have shipped, in two different renderers, so the safe form has a
 *  name. */
function blankRegion(id) {
  const node = el(id);
  if (!node) return;
  dropChunkControl(node);
  node.innerHTML = '';
}

function fillInChunks(host, tbody, rows, make, opts = {}) {
  dropChunkControl(host);

  // Which fill this is — read, not bumped: the ``dropChunkControl`` above already
  // retired anything that came before. Every entry point below checks the mark, so a
  // late callback from a retired fill is a no-op rather than a race.
  const mark = host.__chunkGen;
  const current = () => host.__chunkGen === mark;

  let at = 0;
  const total = rows.length;
  const label = opts.noun || 'rows';

  const more = () => {
    if (!host.__chunkMore) return;
    if (at >= total) { dropChunkControl(host); return; }
    host.__chunkMore.textContent =
      `Showing ${at.toLocaleString()} of ${total.toLocaleString()} ${label} — scroll for more, or click here`;
  };

  const step = () => {
    if (!current()) return false;
    if (at >= total) return false;
    const upto = Math.min(at + ROW_CHUNK, total);
    let html = '';
    for (let i = at; i < upto; i += 1) html += make(rows[i], i);
    tbody.insertAdjacentHTML('beforeend', html);
    at = upto;
    more();
    return true;
  };

  step();
  if (at >= total) return;
  if (typeof IntersectionObserver !== 'function') {
    while (step());
    return;
  }

  // The click path, and the reason a held-back row is never a silent one. Placed
  // after the container so it works for a table and for a card grid alike.
  if (typeof document.createElement === 'function' && host.insertAdjacentElement) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chunk-more';
    button.addEventListener('click', () => { step(); watchTail(); });
    host.insertAdjacentElement('afterend', button);
    host.__chunkMore = button;
    if (host.id) chunkControls.set(host.id, button);
    more();
  }

  // Watching the last row that exists, rather than a sentinel: a sentinel inside
  // a tbody has to be a row itself, and a fake row is a row that can be clicked.
  const io = new IntersectionObserver((entries) => {
    if (!current()) { io.disconnect(); return; }
    if (!entries.some((e) => e.isIntersecting)) return;
    io.disconnect();
    if (step()) watchTail();
    else host.__chunkIO = null;
  }, { rootMargin: '600px' });
  // Held on the host only while it is actually watching something, so a re-render
  // never has to disconnect an observer that observes nothing.
  const watchTail = () => {
    if (!current()) return;
    const tail = tbody.lastElementChild;
    if (tail && at < total) { io.observe(tail); host.__chunkIO = io; }
    else host.__chunkIO = null;
  };
  watchTail();
}

/* Sort state per table, keyed on the region id rather than the node: `table()`
   replaces its node outright when a filter empties it, so a node-keyed map
   would forget the sort exactly while the reader refines filters. One entry
   per table — the four sortable tables (`findings` and `rejections` opt in
   through `opts.sort`; `odds-table` and `promo-campaign` call `applySort`
   themselves) sharing one key/dir pair would sort each other. Not persisted,
   and not reset on a run, sport or book change: unlike a league or a book, a
   column cannot be made impossible by a new scope. */
const SORTS = new Map();
const sortOf = (id) => SORTS.get(id) || { key: null, dir: 1 };

/** Rows in the table's remembered order — the comparator `renderOdds` carried
 *  when it was the page's only sortable table. */
function applySort(id, columns, rows) {
  const state = sortOf(id);
  if (!state.key) return rows;
  const col = columns.find((c) => c && c.key === state.key && c.sort);
  if (!col) return rows;
  return rows.slice().sort((a, b) => {
    const x = col.sort(a), y = col.sort(b);
    return (x < y ? -1 : x > y ? 1 : 0) * state.dir;
  });
}

/** Clickable headings on the last header row — the one holding the columns;
 *  above it sits the band row. Columns without a `sort` extractor are skipped,
 *  so a heading is never clickable and inert. The handler re-reads `sortOf`
 *  rather than closing over wire-time state, because `rerender` rebuilds the
 *  table and this wiring with it. */
function wireSort(node, columns, id, rerender) {
  if (!node) return; // table() returns undefined when a filter emptied it
  node.querySelectorAll('thead tr:last-child th').forEach((th, i) => {
    const col = columns[i];
    if (!col || !col.sort) return;
    th.classList.add('sortable');
    const state = sortOf(id);
    th.setAttribute('aria-sort',
      state.key === col.key ? (state.dir === 1 ? 'ascending' : 'descending') : 'none');
    th.tabIndex = 0;
    const activate = () => {
      const now = sortOf(id);
      SORTS.set(id, now.key === col.key
        ? { key: col.key, dir: -now.dir }
        : { key: col.key, dir: 1 });
      rerender();
    };
    th.addEventListener('click', activate);
    th.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') activate(); });
  });
}

function table(node, columns, rows, opts = {}) {
  // Before anything replaces this node: the control lives beside it and would
  // otherwise outlive it, describing rows that are no longer on screen.
  dropChunkControl(node);
  // Opt-in sorting: pass `sort` (the table's region id) and `rerender`, and
  // every column carrying a `sort` extractor becomes a clickable heading.
  if (opts.sort) rows = applySort(opts.sort, columns, rows);
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
  // opts.go turns each row into a link to whatever that row is about. Written into
  // the row's own markup here rather than wired per row afterwards, so a table that
  // drills down cannot forget the keyboard path or the class that makes it look
  // clickable — and so the coverage table's 1,704 rows cost one listener instead of
  // 3,408 plus a class and a tabIndex write each.
  const makeRow = (r) => {
    const target = opts.go ? opts.go(r) : null;
    const attrs = target ? ` class="go" tabindex="0" data-go="${escapeHtml(target)}"` : '';
    return '<tr' + attrs + '>' + columns.map((c, i) => {
      const cell = c.cell(r);
      const cls = [c.num ? 'num' : '', seps.has(i) ? 'gsep' : '', cell.cls || ''].filter(Boolean).join(' ');
      return `<td class="${cls}"${cell.title ? ` title="${escapeHtml(cell.title)}"` : ''}>${
        cell.html !== undefined ? cell.html : escapeHtml(cell.text)}</td>`;
    }).join('') + '</tr>';
  };
  node.innerHTML = `<thead>${bandRow}<tr>${head}</tr></thead><tbody></tbody>`;
  fillInChunks(node, node.querySelector('tbody') || node, rows, makeRow,
    { noun: opts.noun || 'rows' });
  if (opts.go) wireRowLinks(node);
  if (opts.sort && opts.rerender) wireSort(node, columns, opts.sort, opts.rerender);
  return node;
}

/** One delegated handler for every drill-down row under `host`.
 *
 *  Attached to the container rather than to each row: a per-row listener on the
 *  odds board and the coverage table meant thousands of closures held for rows
 *  nobody clicks, and re-attaching them was part of what made a filter keystroke
 *  cost 50-95ms. Idempotent, so a re-render does not stack handlers. */
function wireRowLinks(host) {
  if (!host || host.dataset.rowLinks === '1') return;
  host.dataset.rowLinks = '1';
  const targetOf = (ev) => {
    let node = ev.target;
    while (node && node !== host) {
      if (node.dataset && node.dataset.go) return node.dataset.go;
      node = node.parentNode;
    }
    return null;
  };
  host.addEventListener('click', (ev) => {
    const target = targetOf(ev);
    if (target) go(target);
  });
  host.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const target = targetOf(ev);
    if (!target) return;
    ev.preventDefault();
    go(target);
  });
}
const cell = (text, cls, title) => ({ text: txt(text), cls, title });
const html = (markup, cls, title) => ({ html: markup, cls, title });
function escapeHtml(s) {
  return txt(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** One leg's "where to place it" cell.
 *
 * The label states the precision rather than hiding it. A link that says "bet"
 * but lands on a league index costs the reader the seconds an edge is made of,
 * and they would blame the page rather than the book. `mirrored` is called out
 * for the same reason: the price came off an aggregator, so the book's own
 * number needs checking before the stake goes on.
 */
function betLink(link) {
  if (!link || !link.url) return '<span class="dim">—</span>';
  const exact = link.precision === 'event';
  const label = exact ? 'bet' : (link.precision === 'league' ? 'league' : 'site');
  const title = exact
    ? `${link.book} — this game`
    : `${link.book} — ${link.precision} page; find the game from there`
      + (link.mirrored ? ' (price read from an aggregator)' : '');
  const flag = link.mirrored ? '<span class="dim" title="price via an aggregator">*</span>' : '';
  return `<a href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer"`
    + ` title="${escapeHtml(title)}"${exact ? ' class="bet-exact"' : ''}>`
    + `${escapeHtml(label)}</a>${flag}`;
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

/* ── books you cannot bet from the US ──────────────────────────────────────
   Off by default, which is the whole point: a position is only worth reading if
   both legs can be placed, and Pinnacle or Bovada standing in for one of them
   makes a 4% margin that cannot be taken. Turning it on answers the separate
   question of what the offshore market was pricing, and every count, board and
   arb list on the page moves together when it flips.

   The choice is remembered, because it is a fact about where the reader lives
   rather than about the scrape they happen to be looking at. `localStorage`
   throws on some file:// configurations, so both sides are guarded — a browser
   that refuses storage still gets a working toggle, just not a sticky one. */
/* Every `localStorage` read and write on this page goes through these two.
   Storage throws outright on some file:// configurations and in some private
   modes, so a browser that refuses it still gets a working page — just not a
   sticky one. Six hand-written try/catch pairs spelled this same guard before
   they were folded here, and the newest of them was a copy of the oldest. */
function storedValue(key, fallback) {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch (err) {
    return fallback;
  }
}

function storeValue(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (err) { /* private mode or file://; the control still works this session */ }
}

const OFFSHORE_KEY = 'sportarb.showOffshore';

let showOffshore = storedValue(OFFSHORE_KEY, '') === '1';

/* The sportsbook picker's choice — a *brand* key from `brandOf`, '' for every
   sportsbook. Remembered like the offshore switch, because which books the
   reader holds accounts at is a fact about the reader; `buildBookPicker`
   forgets a stored brand this page cannot offer, so a pick made against last
   month's scrape cannot silently narrow this one forever. */
const BOOK_KEY = 'sportarb.book';

let currentBrand = storedValue(BOOK_KEY, '');

/* ── after tax ──────────────────────────────────────────────────────────────
   The one place on this page — or anywhere in this repository — where a tax
   rate is applied to anything.

   Python computes the *basis* of every settlement outcome (`src/tax.py`): the
   gross winnings and the deductible losses it produces. It stops there, because
   the answer has to move when the reader moves these pickers, and because the
   after-tax floor of a position is the minimum over its outcomes *after* the
   rate is applied — and which outcome is worst changes with the rate. So there
   is no second implementation to drift from: the basis rule is Python's alone,
   the rate rule is this file's alone.

   Why the pair rather than the profit: tax is charged on gross winnings, and
   losses are only a capped deduction against them. A $100 profit made of a
   $1,100 win against a $1,000 loss and a $100 profit made of a $150 win against
   a $50 loss are taxed on very different numbers. That is also why an arb is
   *not* risk-free after tax even though it is before: every outcome has its own
   basis, so the outcomes stop being equal.

   Stored as strings, and read back through `Number` — a value written in the
   wrong format reads as "not set" and is indistinguishable from a browser that
   refuses storage, which is the defect `storedValue` exists to make impossible
   to reintroduce quietly. */
const TAX_FED_KEY = 'sportarb.taxFed';
const TAX_STATE_KEY = 'sportarb.taxState';
const TAX_CAP_KEY = 'sportarb.taxCap';

const TAX = DATA.tax || {};
/* 0.90 from tax year 2026. Falls back to 1.0 — no cap — rather than to a
   guess, so a payload written before this existed under-states nothing. */
const DEDUCTIBLE_SHARE = Number(TAX.deductible_share ?? 1);

let taxFed = Number(storedValue(TAX_FED_KEY, '0')) || 0;
let taxState = Number(storedValue(TAX_STATE_KEY, '0')) || 0;
/* The cap is on unless it was explicitly turned off: it is the rule in force,
   and the switch exists to show what it costs, not to opt out of it. */
let taxCap = storedValue(TAX_CAP_KEY, '1') !== '0';

/* Is any rate set at all? When not, every after-tax figure is suppressed and
   the page is exactly what it was before this feature existed. */
function taxOn() {
  return taxFed > 0 || taxState > 0;
}

/* The bill on one basis. `winnings` and `losses` are gross and non-negative.

   The federal layer taxes winnings less the capped loss deduction; the
   deduction can never exceed the winnings, because a losing year is not a
   refund. The state layer taxes gross winnings with no offset at all — the
   harshest arrangement in use (Illinois is one) and the strict reading, on the
   same reasoning `src/commission.py` gives for its rates: a charge understated
   manufactures an edge that is not there, while one overstated only costs a
   position. A reader whose state lets winnings and losses net sets it to
   none. */
function taxBill(basis) {
  if (!basis) return null;
  const winnings = Number(basis.winnings) || 0;
  const losses = Number(basis.losses) || 0;
  const deductible = Math.min(losses * (taxCap ? DEDUCTIBLE_SHARE : 1), winnings);
  return Math.max(0, winnings - deductible) * taxFed + winnings * taxState;
}

/* Profit after tax, for a basis paired with the profit already computed for it.
   Returns null when there is no basis — a position built without a settlement
   grid — so callers omit the figure rather than printing the pre-tax number
   under an "after tax" label, which is the one wrong answer here. */
function afterTax(profit, basis) {
  const bill = taxBill(basis);
  return bill === null ? null : Number(profit) - bill;
}

/* The floor of a position after tax, from rows of {profit, winnings, losses}.

   Taken as a fresh minimum rather than as "the pre-tax floor, taxed": the
   outcome that pays least before tax is often not the one that keeps least
   after it, because the outcome with the larger gross win carries the larger
   bill. Reading the wrong one overstates what a position actually keeps. */
function afterTaxFloor(rows) {
  let worst = null;
  for (const row of rows || []) {
    if (row.winnings === undefined || row.winnings === null) return null;
    const net = afterTax(row.profit, row);
    if (net === null) return null;
    if (worst === null || net < worst) worst = net;
  }
  return worst;
}

// '' means every sport.  The filter is applied at the one place the rest of the
// page reads its rows from, so no section can forget to honour it and show a
// different sport's numbers under the same heading.  The offshore filter rides
// along here for exactly that reason: the board, the games list, the coverage
// grid, movement and quality all read this, and a panel that reached past it
// would quietly disagree with the count in the nav beside it.
//
// The sportsbook picker is the third layer, and the split into `sportRows` /
// `currentRows` is load-bearing: a surface whose *subject* is "compare the
// books" — the odds board, the coverage grid, the games lists, one game —
// cannot be narrowed to one book's rows, because a comparison of one thing is
// not a comparison. Those surfaces read `sportRows()` and narrow *games*
// through `brandGames` instead, keeping every book's column for the games the
// picked book prices. Everything else reads `currentRows()` and inherits the
// brand cut row by row.
let currentSport = '';
const rawRunRows = () => rowsByRun.get(currentRunId) || [];
const runRows = () =>
  showOffshore ? rawRunRows() : rawRunRows().filter((r) => !isUsUnavailable(str(r[COL.source])));
const sportRows = () =>
  currentSport ? runRows().filter((r) => str(r[COL.sport]) === currentSport) : runRows();
const currentRows = () =>
  currentBrand ? sportRows().filter((r) => keepBrand(str(r[COL.source]))) : sportRows();
/** Games the picked book priced, with every other book's column kept. */
const brandGames = (events) => (currentBrand
  ? events.filter((e) => [...e.bySource.keys()].some(keepBrand))
  : events);

function runOptionLabel(r, i) {
  const when = fmtClock(r.started_at);
  const prices = (r.quote_count || 0).toLocaleString();
  const flag = r.ok ? '' : ' — problems';
  const kept = rowsByRun.has(r.id) ? '' : ' — not in this page';
  return `${i === 0 ? 'Latest · ' : ''}${when} · ${prices} prices${flag}${kept}`;
}

function bindRunSelect(pick) {
  if (!pick || pick.dataset.bound) return;
  pick.dataset.bound = '1';
  pick.addEventListener('change', () => selectRun(+pick.value));
}

function buildRunPicker() {
  const pick = el('run-pick');
  const list = el('run-list');
  const options = runs.map((r, i) =>
    `<option value="${r.id}">${escapeHtml(runOptionLabel(r, i))}</option>`).join('');
  if (pick) {
    pick.innerHTML = options || '<option value="">No scrapes yet</option>';
    pick.value = currentRunId != null ? String(currentRunId) : '';
    pick.disabled = !runs.length;
    bindRunSelect(pick);
  }
  if (list) {
    list.innerHTML = runs.map((r, i) => {
      const thin = !rowsByRun.has(r.id);
      const secs = r.duration_ms != null ? (r.duration_ms / 1000).toFixed(1) + 's' : '—';
      const prices = (r.quote_count || 0).toLocaleString() + ' prices';
      const games = (r.event_count || 0) + ' games';
      const flag = r.ok ? '' : ' · problems';
      const kept = thin ? ' · prices not embedded' : '';
      const cls = [
        'run-item',
        r.id === currentRunId ? 'on' : '',
        r.ok ? '' : 'is-bad',
        thin ? 'is-thin' : '',
      ].filter(Boolean).join(' ');
      return `<button type="button" role="option" class="${cls}" data-run-id="${r.id}"
        aria-selected="${r.id === currentRunId ? 'true' : 'false'}">
        <b>${i === 0 ? 'Latest · ' : ''}${fmtTime(r.started_at)}</b>
        <span class="when">${fmtClock(r.started_at)} · ${ago(r.started_at)}</span>
        <span class="bits">${escapeHtml(r.jurisdiction || 'legacy')} · ${escapeHtml(r.route_scope || 'legacy')} · ${prices} · ${games} · took ${secs}${flag}${kept}</span>
      </button>`;
    }).join('');
    list.querySelectorAll('[data-run-id]').forEach((node) => {
      node.addEventListener('click', () => selectRun(+node.getAttribute('data-run-id')));
    });
  }
  const run = runById.get(currentRunId);
  const meta = el('run-meta');
  if (meta) {
    meta.textContent = run
      ? `${run.jurisdiction || 'legacy'} · ${fmtClock(run.started_at)} · ${ago(run.started_at)} · ${runs.length} saved`
      : 'no scrapes yet';
  }
  const histNav = el('nav-history');
  if (histNav) histNav.textContent = runs.length ? String(runs.length) : '';
  paintScrapeStamp();
}

function paintScrapeStamp() {
  const stamp = el('scrape-stamp');
  if (!stamp) return;
  const run = runById.get(currentRunId);
  if (!run) {
    stamp.innerHTML = '';
    return;
  }
  stamp.innerHTML = `Scrape from <b>${escapeHtml(fmtClock(run.started_at))}</b>`
    + ` <span class="ago">· ${escapeHtml(ago(run.started_at))}</span>`;
}

function selectRun(id) {
  if (!runById.has(id)) return;
  currentRunId = id;
  buildRunPicker();
  buildSportPicker();
  renderRunScoped();
}

/** Why a non-comparable sport cannot be compared, said truthfully.
    "one book only" was false in both directions on an all-republisher run:
    zero books a bet could be placed at, ten view-only feeds — and it pointed
    the operator at the collector, the exact misdirection the CLI's VIEW-ONLY
    label was added to remove.  `books` is counterparty-only; `per_source`
    counts every feed with rows. */
function sportGap(entry) {
  const feeds = Object.keys(entry.per_source || {}).length;
  const counterparties = (entry.books || []).length;
  if (!counterparties && feeds) {
    return { flag: `  — view-only feeds only`,
             meta: `${feeds} view-only feed(s) priced it — a mirror is not a ` +
                   `counterparty, so nothing can be compared` };
  }
  if (!counterparties) {
    return { flag: '  — no books', meta: 'no book priced it' };
  }
  return { flag: '  — one book only',
           meta: 'only one book priced it — nothing to compare it with' };
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
        : (entry.meets_bar ? '  — no shared fixture' : sportGap(entry).flag);
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
            : sportGap(chosen).meta))
    : `${offered.filter((e) => e.comparable).length} of ${offered.length} sport(s) comparable across books`;
}

/** Sportsbooks the page can narrow to: every counterparty this scrape priced,
 *  every brand with a promo offer, and every book a bet was logged at — one
 *  control spanning all three, because "what is there at DraftKings?" does not
 *  care which subsystem answers.
 *
 *  The pool reads `runRows()` rather than `currentRows()` for the same reason
 *  `buildSportPicker` does: two global filters must not strangle each other.
 *  Consensus feeds fold to no brand and are skipped — a picker offering "Open"
 *  as a peer of DraftKings would narrow the arbitrage panel by a feed that can
 *  never be a leg. */
function buildBookPicker() {
  const pick = el('book-pick');
  if (!pick) return;
  const feedsByBrand = new Map();
  for (const key of new Set(runRows().map((r) => str(r[COL.source])))) {
    const b = brandOf(key);
    if (!b) continue;
    if (!feedsByBrand.has(b)) feedsByBrand.set(b, []);
    feedsByBrand.get(b).push(key);
  }
  const offered = new Set(feedsByBrand.keys());
  for (const o of (PROMOS.offers || [])) {
    const b = promoBrandKey(o.source);
    if (b) offered.add(b);
  }
  for (const slip of betSlips()) {
    for (const leg of (slip.legs || [])) {
      if (leg.book) offered.add(ledgerBrand(leg.book));
    }
  }
  // A stored pick this page cannot offer is forgotten, not kept: the choice is
  // persisted, and a brand picked against last month's scrape must not narrow
  // every future page invisibly.
  if (currentBrand && !offered.has(currentBrand)) { currentBrand = ''; storeValue(BOOK_KEY, ''); }
  const names = [...offered].sort((a, b) => brandLabel(a).localeCompare(brandLabel(b)));
  pick.innerHTML = ['<option value="">every sportsbook</option>'].concat(
    names.map((b) => {
      const feeds = (feedsByBrand.get(b) || []).length;
      const flag = feeds ? `  — ${feeds} feed${feeds === 1 ? '' : 's'}` : '';
      return `<option value="${escapeHtml(b)}">${escapeHtml(brandLabel(b))}${flag}</option>`;
    })
  ).join('');
  pick.value = currentBrand;
  paintBookMeta(feedsByBrand);
}

/** The rail's line under the sportsbook picker: what the page is narrowed to,
 *  named feed by feed — scanned from `runRows()`, upstream of its own filter,
 *  exactly as `paintOffshoreMeta` reads `rawRunRows()`. */
function paintBookMeta(feedsByBrand) {
  const node = el('book-meta');
  if (!node) return;
  if (!feedsByBrand.size) {
    node.textContent = 'no prices in this scrape yet';
    return;
  }
  if (!currentBrand) {
    const feeds = [...feedsByBrand.values()].reduce((n, list) => n + list.length, 0);
    node.textContent = `${feedsByBrand.size} sportsbook${feedsByBrand.size === 1 ? '' : 's'} in this scrape, across ${feeds} feed${feeds === 1 ? '' : 's'}`;
    return;
  }
  const mine = (feedsByBrand.get(currentBrand) || []).map(book).sort();
  if (!mine.length) {
    // A promo- or ledger-only pick: nothing in this scrape's price rows.
    node.textContent = `${brandLabel(currentBrand)} only · no price feeds in this scrape`;
    return;
  }
  const list = mine.slice(0, 3).join(', ') + (mine.length > 3 ? `, +${mine.length - 3} more` : '');
  node.textContent = `${brandLabel(currentBrand)} only · ${mine.length} feed${mine.length === 1 ? '' : 's'}: ${list}`;
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
  arb:       { title: 'Arbitrage' },
  screen:    { title: 'Odds' },
  promos:    { title: 'Promos' },
  bets:      { title: 'My bets' },
  events:    { title: "Today's games" },
  history:   { title: 'History' },
  overview:  { title: 'How to use' },
  run:       { title: 'This scrape' },
  sports:    { title: 'Coverage' },
  sources:   { title: 'Books' },
  book:      { title: 'One book', parent: 'sources' },
  fixture:   { title: 'One game', parent: 'events' },
  bet:       { title: 'One bet', parent: 'events' },
  odds:      { title: 'All prices' },
  movement:  { title: 'Movement' },
  quality:   { title: 'Checks' },
  raw:       { title: 'Saved pages' },
  glossary:  { title: 'Glossary' },
  schema:    { title: 'Schema' },
};

/** Primary panels always show the newest scrape; History and niche panels may pin an older one. */
const PRIMARY_PANELS = new Set(['arb', 'screen', 'promos', 'events', 'fixture', 'bet']);
const LATEST_RUN_ID = DATA.meta.latest_run_id ?? (runs[0] && runs[0].id);

let here = { panel: 'arb', arg: null };
/** The most recent list panel — the one a drill-down goes back to, and the one
 *  unloaded when the reader moves to a different list. */
let lastList = null;
let currentMarket = 'moneyline';
let currentLeague = '';

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

function panelOf(name) { return PANELS[name] ? name : 'arb'; }

function ensureLatestOnPrimary(panel) {
  if (!PRIMARY_PANELS.has(panel)) return false;
  if (LATEST_RUN_ID == null || currentRunId === LATEST_RUN_ID) return false;
  currentRunId = LATEST_RUN_ID;
  buildRunPicker();
  buildSportPicker();
  renderRunScoped();
  return true;
}

function applyRoute() {
  const raw = (typeof location === 'undefined' ? '' : (location.hash || '')).replace(/^#/, '');
  const cut = raw.indexOf('/');
  const panel = panelOf(cut < 0 ? raw : raw.slice(0, cut));
  const arg = cut < 0 ? null : decodeURIComponent(raw.slice(cut + 1));
  here = { panel, arg };

  // Front views always show the newest scrape; History is where older ones live.
  ensureLatestOnPrimary(panel);

  // #events/<sport> narrows the whole page to one sport, so a sport is linkable
  // rather than only reachable through a sidebar control.
  if (panel === 'events' && arg !== null && arg !== currentSport) {
    currentSport = arg === 'all' ? '' : arg;
    selectedEvent = null;
    buildSportPicker();
    renderRunScoped();
  }

  // Throw away the panel being left *before* building the one being arrived at.
  // Building on arrival bounds what arriving costs but not what the document holds:
  // without this, browsing five panels and scrolling their lists reached 407,670
  // elements — what the eager render used to reach on load, just spread over a
  // session instead of a page load.
  //
  // The order is load-bearing, not a preference. `overview` and `run` share one
  // renderer and therefore one set of regions, so evicting afterwards blanked the
  // panel that had just been revealed: arriving at This scrape from How to use ran
  // `ensurePanel('run')`, which did nothing because rendering How to use had already
  // cleared `run` from `stalePanels`, and then `evictPanel('overview')` emptied
  // `matrix` — the one region This scrape shows. The reader got an empty card under a
  // note reading "1702 games · click one to compare books", and toggling between the
  // two panels never recovered. Evicting first means `markStale` re-marks both names
  // before `ensurePanel` decides, so the arriving panel rebuilds what it owns.
  //
  // What gets unloaded is the last *list* panel, not simply the last panel. A game, a
  // book and a bet are all reached by clicking a row in a list and left by going back
  // to it, so unloading the list on the way in meant coming back to its first 120
  // rows: a reader who had scrolled the 1,702-game board, opened one game and pressed
  // Back needed thirteen more "show more" clicks. The detail panels are small, so
  // keeping the list behind them costs little.
  //
  // Tracking the last list rather than the last panel matters for leaving sideways:
  // board -> one game -> Games would otherwise unload the game panel and leave the
  // board resident for the rest of the session.
  if (!PANELS[panel].parent) {
    if (lastList !== null && lastList !== panel) evictPanel(lastList);
    lastList = panel;
  }

  // Now build what is being navigated to, before it is revealed below. Detail panels
  // re-render for the thing being asked for; a list panel is built the first time it
  // is shown and whenever the run or sport has moved under it.
  showCurrentPanel();

  for (const id of Object.keys(PANELS)) {
    const node = el(id);
    if (node) node.classList.toggle('on', id === panel);
  }
  const inRail = PANELS[panel].parent || panel;
  const more = el('nav-more');
  const primaryHrefs = new Set(['#arb', '#screen', '#promos', '#bets', '#events']);
  if (more && !primaryHrefs.has('#' + inRail)) more.open = true;
  for (const link of navLinks()) {
    link.setAttribute('aria-current', String(link.getAttribute('href') === '#' + inRail));
  }
  const title = el('page-title');
  if (title) title.textContent = PANELS[panel].title;
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
  if (!row) return txt(key) || 'Game';
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
  // Group by the sportsbook's own id for the bet, exactly as the pipeline does.
  // The id-less fallback mirrors Quote.market_key's rule precisely: a spread
  // keys on the UNSIGNED line with no side — its two rows differ on exactly
  // those fields (home -1.5 / away +1.5, side naming the team) — while every
  // other market keeps its own line and side (a team total's side scopes the
  // market).  The first version of this fallback used the signed line, so the
  // page tore every tracker spread into two groups of one while the pipeline
  // grouped them whole: the reader surface and the run disagreeing about the
  // same collection, which is the exact class an earlier loop already fixed
  // elsewhere.  If the Python rule changes, change this WITH it.
  const groups = new Map();
  for (const r of rows) {
    // The spread test rides the same alias the rest of the page uses: a
    // legacy database stores ``run_line``, and comparing the raw string to
    // 'spread' would quietly reopen the signed-line tear for exactly the
    // vocabulary the alias table exists to support.
    const isSpread = mkt(str(r[COL.market])) === 'spread';
    const lineVal = r[COL.line];
    const fallbackLine = isSpread && lineVal != null ? Math.abs(lineVal) : lineVal;
    const fallbackSide = isSpread ? '' : str(r[COL.side]);
    const key = [str(r[COL.source]), str(r[COL.source_event_id]), str(r[COL.source_market_id]) ||
      [str(r[COL.market]), str(r[COL.period]), fallbackSide, fallbackLine, r[COL.is_alternate]].join('|')].join('\x1f');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  return groups;
}

/** The masthead, lede and pills, which are on screen whatever panel you are on.
 *
 *  Split out of renderOverview because the panels are now built on arrival: the
 *  chrome would otherwise be painted only by a panel that may never be opened.
 *  Everything here reads the run summary rather than the quote rows, so it is
 *  cheap enough to repaint on every run and sport change. */
function renderChrome() {
  const run = runById.get(currentRunId);
  // First, before anything reads the choice: rebuilding the picker forgets a
  // stored brand this page cannot offer, and renderChrome is the first line of
  // renderRunScoped — so the impossible pick is cleared before the per-panel
  // filters rebuild off it and before the nav counts read it.
  buildBookPicker();
  // Before the early return: the rail lines describe the controls, which are on
  // screen and meaningful even on a page with no scrape to show yet.
  paintOffshoreMeta();
  paintTaxMeta();
  if (!run) {
    el('lede').textContent = 'No scrapes yet. Hit Scrape now (via --serve) to pull prices.';
    el('brand-sub').textContent = DATA.meta.db_name || '';
    el('masthead-pills').innerHTML = '';
    el('run-notice').classList.remove('on');
    el('run-notice').innerHTML = '';
    paintScrapeStamp();
    return;
  }
  const health = run.sources;
  // Counterparty feeds only, because the validator grades the run that way and
  // two numbers for one run on one page is worse than either alone: this pill
  // read "32 of 38 feeds answered" beside a Checks row reading "15 of 38 … 39%,
  // which is a broken pipeline", and nothing on the page told the reader which
  // population each was counting. Quiet mirrors are named in the title rather
  // than dropped — a republisher going dark is worth seeing, it just is not
  // evidence that collection collapsed.
  const books = health.filter((h) => !isViewOnly(h.key));
  const producing = books.filter((h) => h.quote_count > 0);
  const quietMirrors = health.filter((h) => isViewOnly(h.key) && !(h.quote_count > 0));
  const feedsTitle = `${producing.length} of ${books.length} book(s) you could bet at answered`
    + (quietMirrors.length
      ? `; ${quietMirrors.length} view-only mirror(s) were quiet too (${
          quietMirrors.map((h) => book(h.key)).join(', ')}), but a mirror is not a counterparty`
      : '');
  const usableSports = (run.sports || []).filter((entry) => entry.comparable);
  const singleSports = (run.sports || []).filter((entry) => !entry.comparable);

  el('lede').textContent = DATA.meta.lede;
  el('brand-sub').textContent = `${DATA.meta.db_name} · ${run.jurisdiction || 'legacy jurisdiction'}`;
  el('built').innerHTML = `page built ${escapeHtml(fmtClock(DATA.meta.generated_at))}<br>from ${escapeHtml(DATA.meta.db_path)}`;
  const took = run.duration_ms !== null ? (run.duration_ms / 1000).toFixed(1) + 's' : 'unknown';
  el('run-meta').textContent = `${run.jurisdiction || 'legacy'} · ${fmtClock(run.started_at)} · ${ago(run.started_at)} · took ${took} · ${runs.length} saved`;
  paintScrapeStamp();

  el('masthead-pills').innerHTML = [
    `<span class="pill ${run.ok ? 'ok' : 'bad'}"><i></i>${run.ok ? 'looks healthy' : 'something looked wrong'}</span>`,
    `<span class="pill ${producing.length >= 2 ? 'flat' : 'bad'}" title="${escapeHtml(feedsTitle)}">${
      producing.length} of ${books.length} books answered</span>`,
    `<span class="pill ${usableSports.length ? 'ok' : 'bad'}"><i></i>${usableSports.length} sport${
      usableSports.length === 1 ? '' : 's'} you can compare</span>`,
    singleSports.length
      ? `<span class="pill warn"><i></i>${singleSports.length} sport${
          singleSports.length === 1 ? '' : 's'} not comparable</span>`
      : '',
    currentSport ? `<span class="pill accent">showing ${escapeHtml(sportLabel(currentSport))}</span>` : '',
    // Loud on purpose. The default view is the one whose prices can all be
    // acted on, so the exception is what needs saying on every panel.
    showOffshore
      ? `<span class="pill warn" title="Pinnacle, Bovada, Cloudbet, 1xBet, LeoVegas, the offshore exchanges and the offshore Polymarket are included. Some positions shown cannot be placed from the US."><i></i>including books you can't bet</span>`
      : '',
    `<span class="pill flat">${runs.length} scrape${runs.length === 1 ? '' : 's'} saved</span>`,
    ...(DATA.meta.jurisdiction_warnings || []).map((warning) =>
      `<span class="pill bad" title="${escapeHtml(warning)}">jurisdiction warning</span>`),
  ].filter(Boolean).join('');

  const notice = el('run-notice');
  const thin = !rowsByRun.has(currentRunId) && (run.quote_count || 0) > 0 && !detailLoaded(currentRunId);
  notice.classList.toggle('on', thin);
  notice.innerHTML = thin
    ? `<b>&#9432;</b><span>This scrape's ${run.quote_count.toLocaleString()} prices are not
     included in this page — only the ${DATA.meta.runs_with_rows} most recent scrape(s) carry
     prices, to keep the file small. The totals below come from its own summary, so the board
     is empty on purpose. Rebuild with
     <code>--quote-runs ${DATA.meta.runs_recorded}</code> to include it.</span>`
    : '';
}

function renderOverview() {
  const run = runById.get(currentRunId);
  if (!run) {
    el('home-stats').innerHTML = '';
    el('stat-strip').innerHTML = '';
    el('flow').innerHTML = '';
    blankRegion('matrix');
    dropChunkControl(el('browse-games'));
    el('browse-games').innerHTML = '<div class="empty">No scrapes yet.</div>';
    return;
  }
  const rows = currentRows();
  const hasRows = rowsByRun.has(currentRunId);
  // Active only: a suspended price is showing, not offering, so it is not a
  // "bet you could place" and cannot fill a side of a market the page claims
  // is complete.  The Checks panel already filtered this way; the overview
  // strip did not, and the two disagreed about the same collection —
  // "separate bets 35 — each with every side priced" beside "bets fully
  // priced 21".
  const live = rows.filter((r) => str(r[COL.status]) === 'active');
  const events = new Set(live.map((r) => str(r[COL.event_key])));
  const groups = marketGroups(live);
  let complete = 0;
  for (const group of groups.values()) if (sumsToAMargin(group)) complete += 1;
  const health = run.sources;
  const producing = health.filter((h) => h.quote_count > 0);
  // "venues" below counts counterparties, to agree with the masthead pill and
  // with the validator. A republished mirror answering is not a venue that
  // answered; it is somebody else's price arriving second-hand.
  const producingBooks = producing.filter((h) => !isViewOnly(h.key));
  const producingMirrors = producing.length - producingBooks.length;

  const usableSports = (run.sports || []).filter((entry) => entry.comparable);

  const scoped = currentSport ? ` · ${sportLabel(currentSport)} only` : '';
  const stats = [
    ['prices collected', (hasRows ? live.length : run.quote_count).toLocaleString(),
      'one per bet you could place' + scoped],
    ['fixtures', hasRows ? events.size : run.event_count, `playing ${DATA.meta.slate_dates}`],
    ['separate bets', hasRows ? complete.toLocaleString() : '—', 'each with every side priced'],
    ['sports', (run.sports || []).length,
      usableSports.length
        ? `${usableSports.length} comparable: ${usableSports.map((e) => sportLabel(e.sport)).join(', ')}`
        : 'none comparable across books',
      usableSports.length ? '' : 'is-warn'],
    ['venues', producingBooks.length, producingBooks.map((h) => book(h.key)).join(', ')
      + (producingMirrors ? ` · plus ${producingMirrors} republished mirror(s)` : '')],
    ['problems', run.error_count, run.error_count ? 'see Checks' : 'nothing flagged', run.error_count ? 'is-bad' : 'is-good'],
    ['worth a look', run.warning_count, run.warning_count ? 'see Checks' : 'nothing flagged', run.warning_count ? 'is-warn' : 'is-good'],
  ];
  // Keep the dense strip on This scrape (tests pin "separate bets" here).
  el('stat-strip').innerHTML = stats.map(([name, value, sub, cls]) =>
    `<div class="stat ${cls || ''}"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b><small>${escapeHtml(sub)}</small></div>`
  ).join('');

  // Beginner home strip — same numbers, friendlier labels.
  const homeStats = [
    ['games', hasRows ? events.size : run.event_count, `on ${DATA.meta.slate_dates}`],
    ['prices to compare', (hasRows ? live.length : run.quote_count).toLocaleString(),
      'from every feed that answered' + scoped],
    ['feeds that answered', producing.length,
      producing.length ? producing.map((h) => book(h.key)).join(', ') : 'none yet'],
    ['sports you can compare', usableSports.length,
      usableSports.length
        ? usableSports.map((e) => sportLabel(e.sport)).join(', ')
        : 'need 2+ books on the same game',
      usableSports.length ? '' : 'is-warn'],
  ];
  el('home-stats').innerHTML = homeStats.map(([name, value, sub, cls]) =>
    `<div class="stat ${cls || ''}"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b><small>${escapeHtml(sub)}</small></div>`
  ).join('');
  renderBrowseGames(el('browse-games'), el('browse-games-note'));

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
    // "All feeds": the per-column set includes view-only mirrors and the
    // consensus column, none of which is a book you can bet at.
    { band: 'prices found', label: 'All feeds', num: true, cell: (r) => cell(r.total) },
  ], matrixRows, { empty: 'No prices stored for this collection.' });
}

/** Best full-game moneyline price per side for a game card snippet. */
function moneylineBest(event) {
  const sides = { home: null, away: null, draw: null };
  for (const r of event.rows) {
    if (mkt(str(r[COL.market])) !== 'moneyline') continue;
    if (str(r[COL.period]) !== 'full_game') continue;
    if (str(r[COL.status]) !== 'active') continue;
    const sel = str(r[COL.selection]);
    if (!(sel in sides)) continue;
    const net = netOdds(r);
    const prev = sides[sel];
    if (!prev || net > prev.net) {
      sides[sel] = { net, american: americanOf(r), src: str(r[COL.source]), row: r };
    }
  }
  return sides;
}

/** Format a market line without rounding quarter lines to one decimal. */
function fmtLine(line, market) {
  if (line === null || line === undefined || !isFinite(+line)) return '';
  const n = +line;
  const text = String(n);
  if (mkt(market) === 'total') return text;
  return (n > 0 ? '+' : '') + text;
}

/** Consensus main line for a selection: modal line among non-alternate full-game quotes. */
function consensusLine(event, market, selection) {
  const counts = new Map();
  for (const r of event.rows) {
    if (mkt(str(r[COL.market])) !== market) continue;
    if (str(r[COL.period]) !== 'full_game') continue;
    if (r[COL.is_alternate]) continue;
    if (str(r[COL.selection]) !== selection) continue;
    if (r[COL.line] === null || r[COL.line] === undefined) continue;
    const key = String(+r[COL.line]);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  let best = null, bestN = -1;
  for (const [key, n] of counts) {
    if (n > bestN || (n === bestN && Math.abs(+key) < Math.abs(+best))) {
      best = key; bestN = n;
    }
  }
  return best === null ? null : +best;
}

/** Main-line full-game quotes for one market on one event, keyed by book then selection.
 *
 *  Moneyline: best takeable price per book.  Spread/total: the book's quote on the
 *  consensus main line (not the juiciest alternate-looking number an adapter left
 *  unmarked).  Best-price highlight is computed later and only across the same line. */
function boardQuotes(event, market) {
  const byBook = new Map();
  const targets = new Map();
  if (market !== 'moneyline') {
    for (const sel of new Set(event.rows.map((r) => str(r[COL.selection])))) {
      targets.set(sel, consensusLine(event, market, sel));
    }
  }
  for (const r of event.rows) {
    if (mkt(str(r[COL.market])) !== market) continue;
    if (str(r[COL.period]) !== 'full_game') continue;
    if (r[COL.is_alternate]) continue;
    const src = str(r[COL.source]);
    const sel = str(r[COL.selection]);
    if (!byBook.has(src)) byBook.set(src, new Map());
    if (market !== 'moneyline') {
      const want = targets.get(sel);
      if (want !== null && want !== undefined) {
        if (r[COL.line] === null || r[COL.line] === undefined) continue;
        if (+r[COL.line] !== want) continue;
      } else if (r[COL.line] !== null && r[COL.line] !== undefined) {
        // No consensus yet — keep the line closest to zero (spreads) / first seen.
        const held = byBook.get(src).get(sel);
        if (held && Math.abs(+held[COL.line]) <= Math.abs(+r[COL.line])) continue;
        byBook.get(src).set(sel, r);
        continue;
      }
    }
    const held = byBook.get(src).get(sel);
    if (!held || _betterPrice(r, held)) byBook.get(src).set(sel, r);
  }
  return byBook;
}

function boardSideLabels(event, market) {
  if (market === 'total') return [['over', 'Over'], ['under', 'Under']];
  const away = nick(event.awayRaw);
  const home = nick(event.homeRaw);
  if (market === 'spread') return [['away', away], ['home', home]];
  const sides = [['away', away], ['home', home]];
  if ([...event.rows].some((r) => mkt(str(r[COL.market])) === 'moneyline'
      && str(r[COL.period]) === 'full_game'
      && str(r[COL.selection]) === 'draw')) {
    sides.push(['draw', 'Draw']);
  }
  return sides;
}

/** American odds implied by a decimal payout — always agrees with `$100 returns`. */
function americanFromDecimal(net) {
  if (!(net > 1)) return 0;
  return net >= 2 ? Math.round((net - 1) * 100) : -Math.round(100 / (net - 1));
}

function priceCell(q, bestNet, comparable) {
  if (!q) return '<span class="dim">—</span>';
  const suspended = str(q[COL.status]) !== 'active';
  const net = netOdds(q);
  const viewOnly = isViewOnly(str(q[COL.source]));
  const isBest = !viewOnly && !!comparable && !suspended && bestNet !== null && net === bestNet;
  // Derive American from the same net the board ranks on, so a stored American
  // that disagrees with the decimal cannot paint a self-contradictory tip.
  const american = americanFromDecimal(net);
  const lineBit = (q[COL.line] === null || q[COL.line] === undefined) ? ''
    : ` <span class="dim">${escapeHtml(fmtLine(q[COL.line], str(q[COL.market])))}</span>`;
  const cls = `odds-cell price${isBest ? ' best' : ''}${suspended ? ' dim' : ''}`;
  const tip = `${fmtAmerican(american)} · $100 returns ${fmtReturn(net)}${
    suspended ? ' · not taking bets' : ''}`;
  return `<span class="${cls}" title="${escapeHtml(tip)}">${escapeHtml(fmtAmerican(american))}${lineBit}</span>`;
}

function buildLeaguePicker() {
  const leagues = [...new Set(runRows().map((r) => str(r[COL.league])).filter(Boolean))].sort();
  const sportFiltered = currentSport
    ? leagues.filter((lg) => runRows().some((r) =>
        str(r[COL.league]) === lg && str(r[COL.sport]) === currentSport))
    : leagues;
  // Clear when the chosen league is not offered under the current sport filter —
  // otherwise the select shows "every league" while rows stay filtered empty.
  if (currentLeague && !sportFiltered.includes(currentLeague)) currentLeague = '';
  const html = ['<option value="">every league</option>'].concat(
    sportFiltered.map((lg) =>
      `<option value="${escapeHtml(lg)}">${escapeHtml(leagueLabel(lg))}</option>`)
  ).join('');
  // Odds and Games share one league choice so narrowing the slate sticks when
  // you flip between the two boards.
  for (const id of ['league-pick', 'events-league']) {
    const pick = el(id);
    if (!pick) continue;
    pick.innerHTML = html;
    pick.value = currentLeague;
    if (!pick.dataset.bound) {
      pick.dataset.bound = '1';
      pick.addEventListener('change', () => {
        currentLeague = pick.value;
        buildLeaguePicker();
        // Both boards are scoped by the league, so both are now out of date — but
        // only the one being looked at is worth building.
        // The rail counts both boards, and the one left unbuilt has no renderer to
        // write its own count — `refreshPanel` reschedules them for exactly that case.
        refreshPanel('screen');
        refreshPanel('events');
      });
    }
  }
}

function wireMarketTabs() {
  const tabs = el('market-tabs');
  const markets = [
    ['moneyline', 'Moneyline'],
    ['spread', 'Spread'],
    ['total', 'Total'],
  ];
  tabs.innerHTML = markets.map(([key, label]) =>
    `<button type="button" role="tab" data-market="${key}" aria-selected="${
      key === currentMarket ? 'true' : 'false'}" class="${
      key === currentMarket ? 'on' : ''}">${label}</button>`).join('');
  tabs.querySelectorAll('[data-market]').forEach((btn) => {
    btn.addEventListener('click', () => {
      currentMarket = btn.getAttribute('data-market') || 'moneyline';
      refreshPanel('screen');
    });
  });
}

function renderOddsScreen() {
  const host = el('odds-screen');
  const note = el('screen-note');
  const nav = el('nav-screen');
  // `sportRows()` + `brandGames`, never the row-narrowed accessor: the board's
  // subject is the comparison, so the sportsbook picker narrows which *games*
  // show and keeps every book's column on the games that survive.
  const rows = sportRows().filter((r) =>
    !currentLeague || str(r[COL.league]) === currentLeague);
  const events = brandGames(eventSummaries(rows));
  if (nav) nav.textContent = events.length;
  buildLeaguePicker();
  wireMarketTabs();

  if (!events.length) {
    dropChunkControl(host);
    const run = runById.get(currentRunId);
    const thin = run && (run.quote_count || 0) > 0 && !detailLoaded(currentRunId);
    if (note) {
      note.textContent = thin
        ? 'this scrape has prices, but they are not embedded in this page'
        : 'scrape first to fill the board';
    }
    const brandEmptied = currentBrand && eventSummaries(rows).length > 0;
    host.innerHTML = thin
      ? `<div class="empty">This scrape's ${(run.quote_count || 0).toLocaleString()} prices are not
         embedded here — only the newest few scrapes carry the board. Rebuild with
         <code>--quote-runs</code> larger, or open <a href="#history">History</a> for an embedded scrape.</div>`
      : (brandEmptied
        ? `<div class="empty">${escapeHtml(brandLabel(currentBrand))} priced nothing in this scrape
           under these filters. Pick another sportsbook in the left sidebar to fill the board.</div>`
        : `<div class="empty">No games in the latest scrape. Hit <b>Scrape now</b>.</div>`);
    return;
  }

  // Columns come from the surviving games' own rows, so a book that priced none
  // of them gets no empty column; the picked book's feeds lead, the rest keep
  // their alphabetical order.
  const sources = [...new Set(events.flatMap((e) => e.rows.map((r) => str(r[COL.source]))))].sort();
  if (currentBrand) {
    const rank = (s) => (keepBrand(s) ? 0 : 1);
    sources.sort((a, b) => rank(a) - rank(b) || (a < b ? -1 : a > b ? 1 : 0));
  }
  if (note) {
    note.textContent = `${events.length} game${events.length === 1 ? '' : 's'}${
      currentBrand ? ` ${brandLabel(currentBrand)} prices` : ''} · ${
      sources.length} feed${sources.length === 1 ? '' : 's'} · ${
      currentMarket} · American odds · highlight = best on the same line${
      currentBrand ? ` · ${brandLabel(currentBrand)} first` : ''}`;
  }

  const head = ['<th class="oj-game">Game</th>']
    .concat(sources.map((s) => `<th class="book${currentBrand && keepBrand(s) ? ' picked' : ''}">${
      escapeHtml(book(s))}</th>`))
    .join('');
  const boardRow = (event) => {
    const byBook = boardQuotes(event, currentMarket);
    const sides = boardSideLabels(event, currentMarket);
    const bestBySide = new Map();
    const comparableBySide = new Map();
    for (const [sel] of sides) {
      const live = [];
      for (const [src, bookMap] of byBook) {
        if (isViewOnly(src)) continue;
        const q = bookMap.get(sel);
        if (q && str(q[COL.status]) === 'active') live.push(q);
      }
      // Highlight only when 2+ bettable books offer the *same* contract (same line).
      // View-only feeds (AN Open) stay on the board but never set the best mark.
      const lineKey = (q) => (q[COL.line] === null || q[COL.line] === undefined)
        ? '' : String(+q[COL.line]);
      const byLine = new Map();
      for (const q of live) {
        const k = lineKey(q);
        if (!byLine.has(k)) byLine.set(k, []);
        byLine.get(k).push(q);
      }
      let best = null, comparable = false;
      for (const group of byLine.values()) {
        if (group.length < 2) continue;
        comparable = true;
        const peak = Math.max(...group.map(netOdds));
        if (best === null || peak > best) best = peak;
      }
      bestBySide.set(sel, best);
      comparableBySide.set(sel, comparable);
    }
    const cells = sources.map((src) => {
      const bookMap = byBook.get(src) || new Map();
      const stack = sides.map(([sel, label]) => {
        const q = bookMap.get(sel);
        return `<div class="row"><span class="lbl">${escapeHtml(label)}</span>${
          priceCell(q, bestBySide.get(sel), comparableBySide.get(sel))}</div>`;
      }).join('');
      return `<td class="book${currentBrand && keepBrand(src) ? ' picked' : ''}"><div class="oj-side">${stack}</div></td>`;
    }).join('');
    return `<tr data-go="${escapeHtml(href('fixture', event.key))}" tabindex="0">
      <td class="oj-game">
        <b>${escapeHtml(nick(event.awayRaw))} <span class="dim">@</span> ${escapeHtml(nick(event.homeRaw))}</b>
        <span class="when">${escapeHtml(fmtClock(event.commence))} · ${
          escapeHtml(leagueLabel(event.league))} · ${escapeHtml(sportLabel(event.sport))}</span>
      </td>${cells}</tr>`;
  };

  host.innerHTML = `<table class="oj-board"><thead><tr>${head}</tr></thead><tbody></tbody></table>`;
  fillInChunks(host, host.querySelector('tbody') || host, events, boardRow, { noun: 'games' });
  wireRowLinks(host);
}

function renderBrowseGames(node, noteNode, events) {
  events = events || brandGames(eventSummaries(sportRows()));
  dropChunkControl(node);
  if (!events.length) {
    const run = runById.get(currentRunId);
    const thin = run && (run.quote_count || 0) > 0 && !detailLoaded(currentRunId);
    // Upstream of the sportsbook picker as well as the panel's own filters, so
    // a brand that emptied the list still reads as "filtered", not "no games".
    const filteredOut = !thin && eventSummaries(sportRows()).length > 0;
    if (noteNode) {
      noteNode.textContent = thin
        ? 'prices not embedded in this page'
        : (filteredOut ? 'nothing matches these filters' : 'scrape first, or pick a newer scrape');
    }
    node.innerHTML = thin
      ? `<div class="empty">This scrape's ${(run.quote_count || 0).toLocaleString()} prices are not
         embedded here. Rebuild with a larger <code>--quote-runs</code>, or pick a newer scrape.</div>`
      : (filteredOut
        ? `<div class="empty">No games match these filters. Clear the search, league, or book filter above${
            currentBrand ? ', or the sportsbook picker in the left sidebar' : ''}.</div>`
        : `<div class="empty">No games in this scrape yet. Hit <b>Scrape now</b> in the left sidebar, then pick the newest scrape.</div>`);
    return;
  }
  if (noteNode) {
    noteNode.textContent = `${events.length} game${events.length === 1 ? '' : 's'} · click one to compare books`;
  }
  const gameCard = (e) => {
    const ml = moneylineBest(e);
    const line = (sel, label) => {
      const hit = ml[sel];
      if (!hit) return '';
      return `<span><i>${escapeHtml(label)}</i><b>${escapeHtml(fmtAmerican(hit.american))}</b></span>`;
    };
    const lines = [line('away', nick(e.awayRaw)), line('home', nick(e.homeRaw)),
      line('draw', 'Draw')].filter(Boolean).join('');
    const league = e.league ? leagueLabel(e.league) : '';
    return `<a class="game-card" href="${escapeHtml(href('fixture', e.key))}">
      <span class="when">${escapeHtml(fmtClock(e.commence))} · ${escapeHtml(sportLabel(e.sport))}${
        league ? ` · ${escapeHtml(league)}` : ''}</span>
      <b>${escapeHtml(nick(e.awayRaw))} <span class="dim">@</span> ${escapeHtml(nick(e.homeRaw))}</b>
      <span class="meta">${e.bySource.size} feed${e.bySource.size === 1 ? '' : 's'} · ${e.rows.length} prices</span>
      ${lines ? `<div class="mlines">${lines}</div>` : ''}
      <span class="cta">Open game &rarr;</span>
    </a>`;
  };
  // One card per game, and moneylineBest walks the game's rows to build each: on a
  // full slate that is 1,702 cards nobody scrolls to the end of.
  node.innerHTML = '';
  fillInChunks(node, node, events, gameCard, { noun: 'games' });
}

/* ── promos / bonuses ────────────────────────────────────────────────────── */

const PROMO_KIND_LABEL = {
  signup_bonus: 'Signup bonus',
  deposit_match: 'Deposit match',
  bonus_bet: 'Bonus bet',
  free_bet: 'Free bet',
  no_sweat: 'No sweat',
  odds_boost: 'Odds boost',
  profit_boost: 'Profit boost',
  parlay_boost: 'Parlay boost',
  referral: 'Referral',
  loyalty: 'Loyalty',
  risk_free: 'Risk free',
  contest: 'Contest',
  other: 'Other',
};

function promoKindLabel(kind) {
  return PROMO_KIND_LABEL[kind] || String(kind || 'other').replace(/_/g, ' ');
}

/** A promo source's display name: the payload's ``labels`` (one entry per
 *  promo key, folded to the brand by the server from `BRAND_LABELS`), then the
 *  odds side's own brand name for a key it also trades.  No table of its own —
 *  the one this replaced had drifted and never learned ``thescore``. */

let selectedPromoKey = null;

function promoBrandKey(key) {
  const k = String(key || '');
  return k.startsWith('tl_') ? k.slice(3) : k;
}

function promoBookLabel(key) {
  const k = String(key || '');
  const brand = promoBrandKey(k);
  const base = (PROMOS.labels || {})[k]
    || (brandLabel(brand) !== brand ? brandLabel(brand) : null)
    || brand.replace(/_/g, ' ');
  return k.startsWith('tl_') ? `${base} (via TheLines)` : base;
}

/** The sportsbook picker's comparison for the promo key space — `keepBrand`'s
 *  twin, spelled once for the same reason. The promo key space already meets
 *  the odds side's brand keys (`promoBrandKey('tl_betrivers_kambi')` is
 *  `betrivers_kambi`, exactly `brandOf`'s key), which is what makes one picker
 *  able to span both. */
function keepPromoBrand(key) {
  return !currentBrand || promoBrandKey(key) === currentBrand;
}

/** Offers inside the sportsbook picker's scope — the one list the nav badge,
 *  the panel headline and the browsing list all count, so they cannot drift. */
function promoOffers() {
  const offers = PROMOS.offers || [];
  return currentBrand ? offers.filter((o) => keepPromoBrand(o.source)) : offers;
}

function promoBrandCoverage(healthRows) {
  const byBrand = new Map();
  for (const h of (healthRows || [])) {
    const brand = promoBrandKey(h.source_key);
    byBrand.set(brand, !!(byBrand.get(brand) || h.ok));
  }
  let ok = 0;
  for (const v of byBrand.values()) if (v) ok += 1;
  return { ok, total: byBrand.size };
}

function promoOfferKey(o) {
  return `${o.source || ''}|${o.offer_id || ''}`;
}

function ensurePromoFilters() {
  const kindSel = el('promo-kind');
  const sourceSel = el('promo-source');
  const regionSel = el('promo-region');
  if (!kindSel || !sourceSel) return;
  const kinds = new Set((PROMOS.kinds || []).concat((PROMOS.offers || []).map((o) => o.kind)));
  // The book list follows the sportsbook picker, so the two compose: under a
  // DraftKings pick this select lists DraftKings' own feeds, and a feed left
  // selected from before cannot silently empty the panel. Health keys join
  // only when no brand narrows them — a failed feed has no offers to find.
  const sources = new Set(promoOffers().map((o) => o.source));
  for (const h of (PROMOS.health || [])) {
    if (keepPromoBrand(h.source_key)) sources.add(h.source_key);
  }
  const regions = new Set();
  for (const o of (PROMOS.offers || [])) {
    for (const r of (o.eligible_regions || [])) regions.add(r);
    for (const r of (o.ineligible_regions || [])) regions.add(r);
  }
  const kindVal = kindSel.value;
  const sourceVal = sourceSel.value;
  const regionVal = regionSel ? regionSel.value : '';
  kindSel.innerHTML = '<option value="">every kind</option>' +
    [...kinds].filter(Boolean).sort().map((k) =>
      `<option value="${escapeHtml(k)}">${escapeHtml(promoKindLabel(k))}</option>`).join('');
  sourceSel.innerHTML = '<option value="">every book</option>' +
    [...sources].filter(Boolean).sort().map((k) =>
      `<option value="${escapeHtml(k)}">${escapeHtml(promoBookLabel(k))}</option>`).join('');
  if (regionSel) {
    regionSel.innerHTML = '<option value="">all regions</option>' +
      [...regions].filter(Boolean).sort().map((k) =>
        `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`).join('');
    if ([...regions].includes(regionVal)) regionSel.value = regionVal;
  }
  if ([...kinds].includes(kindVal)) kindSel.value = kindVal;
  if ([...sources].includes(sourceVal)) sourceSel.value = sourceVal;
}

/* Concrete usage plans, computed in Python from the same run the arb panel
   prices.  This code renders the stored numbers and computes none of them —
   the movement table taught what happens when the page re-derives a figure
   the pipeline also derives. */
const PROMO_STRATEGY_LABEL = {
  bonus_conversion: 'Convert the credit through a real market',
  qualify_then_convert: 'Qualify, then convert the credit',
  no_sweat_hedge: 'Protected bet, hedged elsewhere',
  boost_locked: 'This boost locks a profit at current prices',
  boost_breakeven: 'Smallest boost that would lock a profit',
  rollover_grind: 'Grind the rollover at minimum vig',
};

function promoMoney(v) {
  const n = Number(v);
  if (!isFinite(n)) return '—';
  return (n < 0 ? '−$' : '$') + Math.abs(n).toFixed(2);
}

function promoAge(seconds) {
  const n = Number(seconds);
  if (!isFinite(n) || n < 0) return '';
  if (n < 90) return `${Math.round(n)}s`;
  if (n < 5400) return `${Math.round(n / 60)}m`;
  return `${(n / 3600).toFixed(1)}h`;
}

function promoSelectionLabel(plan, leg) {
  if (leg.selection === 'home') return plan.home_team || 'Home';
  if (leg.selection === 'away') return plan.away_team || 'Away';
  if (leg.selection === 'draw') return 'Draw';
  return String(leg.selection || '').replace(/^./, (c) => c.toUpperCase());
}

/** The ledger slip a promo plan would log: the planner's legs as placed —
 *  the promo leg as credit (or a boosted stake) and the hedges as cash — with
 *  the offer named on the slip so My bets can say which promotion a position
 *  was spending. ``expected_profit`` is the settled floor, the figure the
 *  Campaign table ranks by for these strategies. */
function promoSlipFor(o, plan) {
  const meta = PROMOS.plan_meta || {};
  return {
    kind: 'promo',
    promo_source: o.source,
    promo_offer_id: o.offer_id,
    sport: plan.sport,
    league: plan.league,
    event_key: plan.event_key,
    home_team: plan.home_team,
    away_team: plan.away_team,
    commence_time: plan.commence_time,
    market: plan.market,
    period: plan.period,
    side: plan.side || '',
    line: plan.line,
    expected_profit: plan.settled_cash,
    source_run_id: meta.odds_run_id === undefined ? null : meta.odds_run_id,
    note: `${o.summary || o.title || ''}`.slice(0, 200),
    legs: (plan.legs || []).map((leg) => ({
      book: leg.source,
      selection: leg.selection,
      line: leg.line,
      american_odds: leg.american_odds,
      decimal_odds: leg.decimal_odds,
      stake: leg.stake,
      // The ledger says which stake kinds it takes; the page keeps no list of
      // its own.  With no ledger vocabulary on the page, the planner's kind is
      // posted as-is and the ledger's own refusal is the answer.
      stake_kind: BETS && BETS.stake_kinds && !BETS.stake_kinds.includes(leg.stake_kind)
        ? 'cash' : leg.stake_kind,
      link_url: (leg.link && leg.link.url) || '',
    })),
  };
}

/** A drawer id that survives ``querySelector``: the offer key can hold any
 *  character a venue put in an id, so only the plan's position and a slug of
 *  the key are used. */
function promoDrawerIndex(o, planIndex) {
  return `promo-${planIndex}-${promoOfferKey(o).replace(/[^a-z0-9]+/gi, '-')}`;
}

function promoPlanCardHtml(plan, o, planIndex) {
  const stepBadge = plan.step
    ? `<span class="pill flat">${plan.step === 'qualify' ? 'step 1 · qualify' : 'step 2 · convert'}</span> `
    : '';
  const marketBits = [String(plan.market || '').replace(/_/g, ' ')];
  // Whose total this is.  ``side`` is non-null for exactly one market — a team
  // total — and without it the two sides of one fixture rendered as two
  // character-for-character identical cards.  Neither could be placed: nothing
  // on screen said whether the over belonged to the home or the away team, and
  // taking one card's promo leg with the other's hedge is two uncorrelated
  // bets that can both lose against a printed guarantee.
  if (plan.side) {
    const team = plan.side === 'home' ? plan.home_team : plan.away_team;
    marketBits.push(team ? `${team}` : String(plan.side));
  }
  if (plan.line !== null && plan.line !== undefined) marketBits.push(fmtLine(plan.line, plan.market));
  marketBits.push(String(plan.period || '').replace(/_/g, ' '));
  const legs = (plan.legs || []).map((leg) => {
    const line = (leg.line !== null && leg.line !== undefined && plan.market !== 'moneyline')
      ? ` ${fmtLine(leg.line, plan.market)}` : '';
    return `<tr>
      <td><span class="pill flat">${leg.role === 'promo' ? 'promo' : 'hedge'}</span></td>
      <td>${escapeHtml(promoBookLabel(leg.source))}${originPill(leg)}</td>
      <td>${escapeHtml(promoSelectionLabel(plan, leg))}${escapeHtml(line)}</td>
      <td>${escapeHtml(fmtAmerican(leg.american_odds))}</td>
      <td>${promoMoney(leg.stake)}${leg.stake_kind === 'bonus' ? ' <span class="dim">credit</span>' : ''}</td>
      <td>${betLink(leg.link)}</td>
    </tr>`;
  }).join('');
  const metrics = [];
  if (plan.conversion_pct !== null && plan.conversion_pct !== undefined) {
    metrics.push(`${Number(plan.conversion_pct).toFixed(1)}% conversion`);
  }
  if (plan.breakeven_boost_pct !== null && plan.breakeven_boost_pct !== undefined) {
    metrics.push(`needs a ${Number(plan.breakeven_boost_pct).toFixed(1)}%+ boost`);
  }
  if (plan.cost_per_100_wagered !== null && plan.cost_per_100_wagered !== undefined) {
    metrics.push(`${promoMoney(plan.cost_per_100_wagered)} cost per $100 wagered`);
  }
  if (plan.qualifying_cost !== null && plan.qualifying_cost !== undefined) {
    metrics.push(`qualifying round-trip ${promoMoney(plan.qualifying_cost)}`);
  }
  metrics.push(`worst case ${promoMoney(plan.guaranteed_cash)}`);
  if (plan.settled_cash !== plan.guaranteed_cash) {
    metrics.push(`if it settles ${promoMoney(plan.settled_cash)}`);
  }
  /* `outcome_bases` is aligned with `outcome_profits` by position and by label;
     it is a separate key because a dozen readers destructure the rows above as
     exactly two-wide pairs. A promo plan's basis is the one most unlike its
     profit: a bonus stake is never a deductible loss, so a converted credit is
     winnings with nothing to set against it. */
  if (taxOn() && (plan.outcome_bases || []).length) {
    const rows = plan.outcome_profits.map(([label, profit], i) => {
      const basis = plan.outcome_bases[i];
      return { profit, winnings: basis && basis[1], losses: basis && basis[2] };
    });
    const net = afterTaxFloor(rows);
    if (net !== null) metrics.push(`worst case ${promoMoney(net)} after tax`);
  }
  const age = promoAge(plan.quote_age_seconds);
  const outcomes = (plan.outcome_profits || [])
    .map(([label, profit]) => `${String(label).replace(/_/g, ' ')} ${promoMoney(profit)}`)
    .join(' · ');
  const notes = (plan.notes || [])
    .map((n) => `<p class="plan-sub">${escapeHtml(n)}</p>`).join('');
  // Logging is the plan's last step: every leg the card shows, pre-filled,
  // with the offer on the slip.  Only when the page is served, like every
  // other ledger control — a file:// page is view-only.
  const index = promoDrawerIndex(o, planIndex);
  const logging = BETS_WRITABLE
    ? `<div class="bl-acts" style="justify-content:flex-start;margin-top:8px">
        <button type="button" class="bl-btn" data-bl="toggle" data-bl-index="${escapeHtml(index)}"
          title="Pre-fill these legs into My bets">Log this plan</button>
      </div>${slipDrawerHtml(promoSlipFor(o, plan), index, { total: null })}`
    : '';
  // The same verdict the arbitrage card carries, from the same Python
  // marking: a plan whose hedge sits at Smarkets is the best execution on the
  // board and not one this reader can place. Absent on an ungoverned run.
  const verdict = plan.takeable === undefined ? ''
    : plan.takeable
      ? '<span class="arb-pill ok">takeable</span> '
      : `<span class="arb-pill warn">not takeable · ${escapeHtml(arbBlockers(plan).join(', '))} out of reach</span> `;
  return `<div class="plan-card">
    <h5>${stepBadge}${verdict}${escapeHtml(plan.away_team || '')} at ${escapeHtml(plan.home_team || '')}</h5>
    <p class="plan-sub">${escapeHtml(marketBits.filter(Boolean).join(' · '))}
      · ${escapeHtml(fmtClock(plan.commence_time))}${age ? ` · quotes ${escapeHtml(age)} old at build` : ''}</p>
    <table><tbody>${legs}</tbody></table>
    <div class="pill-row">${metrics.map((m) => `<span class="pill flat">${escapeHtml(m)}</span>`).join('')}</div>
    ${outcomes ? `<p class="plan-outcomes">outcomes: ${escapeHtml(outcomes)}</p>` : ''}
    ${notes}
    ${logging}
  </div>`;
}

function promoPlanHtml(o) {
  const entry = (PROMOS.plans || {})[promoOfferKey(o)];
  if (!entry) return '';
  const caveats = (entry.caveats || [])
    .map((c) => `<p class="dim">${escapeHtml(c)}</p>`).join('');
  const skippedText = Object.entries(entry.skipped || {})
    .map(([reason, count]) => `${String(reason).replace(/_/g, ' ')} ×${count}`)
    .join(' · ');
  const skippedHtml = skippedText
    ? `<p class="dim">gated out: ${escapeHtml(skippedText)}</p>` : '';
  const label = PROMO_STRATEGY_LABEL[entry.strategy];
  if (!label || !(entry.plans || []).length) {
    // No concrete plan — the caveats say why (no odds feed, every market
    // gated out, refund unmeasurable), and the text playbook below stands.
    //
    // The counts ship with them.  One of those caveats is "the counts in
    // 'skipped' say what was refused and why", and this branch used to return
    // the caveats alone — so the ordinary "nothing to do today" state pointed
    // the reader at evidence the page never rendered.  The counts were only
    // reachable in the with-plans branch below, which is the one case where
    // they explain the least.
    return caveats + skippedHtml;
  }
  const bits = [];
  if (entry.unit && entry.unit.amount !== null && entry.unit.amount !== undefined) {
    bits.push(`sized for ${promoMoney(entry.unit.amount)}${entry.unit.assumed ? ' (assumed)' : ''}`);
  }
  if (entry.refund_conversion_pct !== null && entry.refund_conversion_pct !== undefined) {
    bits.push(`refund valued at ${Number(entry.refund_conversion_pct).toFixed(1)}%`);
  }
  if (entry.expected_value !== null && entry.expected_value !== undefined) {
    bits.push(`net value ${promoMoney(entry.expected_value)}`);
    // The overall figure can be priced off a hedge the reader cannot reach;
    // when the takeable pass says a different number, both are said. The
    // Campaign table's ranking is a separate, planned change.
    const local = entry.takeable_expected_value;
    if (local !== null && local !== undefined
        && Number(local).toFixed(2) !== Number(entry.expected_value).toFixed(2)) {
      bits.push(`takeable from here ${promoMoney(local)}`);
    }
  }
  return `<h4>${escapeHtml(label)}</h4>
    ${bits.length ? `<p class="plan-sub">${escapeHtml(bits.join(' · '))}</p>` : ''}
    ${(entry.plans || []).map((plan, i) => promoPlanCardHtml(plan, o, i)).join('')}
    ${promoTakeableHtml(entry, o)}
    ${caveats}
    ${skippedHtml}`;
}

/** The best execution using only books reachable from this state.
 *
 *  The cards above are the best on the whole board and may hedge at a venue
 *  the reader cannot open an account with. `takeable_plans` is a second
 *  planner pass with every such venue removed, built in Python beside the
 *  first; this says which of three things is true — the best overall plan is
 *  itself takeable, a different takeable plan exists, or none does — rather
 *  than leaving the reader to pick the first card without an offshore pill. */
function promoTakeableHtml(entry, o) {
  if (!Array.isArray(entry.takeable_plans)) return '';
  // Two shapes share `plans`: ranked alternatives (best first) and the
  // qualify-then-convert *step sequence*, where plans[0] is step 1 of an
  // execution, not the best of anything. A two-step play is takeable only if
  // every step is — judging it on the qualifying leg alone called a
  // Smarkets-hedged conversion "takeable from here".
  const overall = entry.plans || [];
  const stepped = overall.some((plan) => plan.step);
  const overallTakeable = stepped
    ? overall.length > 0 && overall.every((plan) => plan.takeable === true)
    : Boolean(overall[0] && overall[0].takeable);
  if (overallTakeable) {
    return `<p class="plan-sub">best overall plan above is takeable from here</p>`;
  }
  const plans = entry.takeable_plans;
  if (!plans.length) {
    const why = Object.entries(entry.takeable_skipped || {})
      .map(([reason, count]) => `${String(reason).replace(/_/g, ' ')} ×${count}`)
      .join(' · ');
    return `<h4>Best takeable from here</h4>
      <p class="dim">no plan can be built from books you can bet at from this state${
        why ? ` — ${escapeHtml(why)}` : ''}</p>`;
  }
  const value = entry.takeable_expected_value;
  return `<h4>Best takeable from here</h4>
    ${value !== null && value !== undefined
      ? `<p class="plan-sub">${escapeHtml(`net value ${promoMoney(value)}`)}</p>` : ''}
    ${plans.map((plan, i) => promoPlanCardHtml(plan, o, `local-${i}`)).join('')}`;
}

function promoDetailHtml(o) {
  const summary = o.summary || o.title || '';
  const eligible = (o.eligible_regions || []).join(', ') || 'not stated';
  const ineligible = (o.ineligible_regions || []).join(', ');
  const bits = [];
  if (o.bonus_amount != null) bits.push(`reward $${Number(o.bonus_amount)}`);
  if (o.min_deposit != null) bits.push(`min deposit $${Number(o.min_deposit)}`);
  if (o.min_odds) bits.push(`min odds ${o.min_odds}`);
  if (o.wagering_requirement) bits.push(`wagering ${o.wagering_requirement}`);
  if (o.reward_type) bits.push(String(o.reward_type).replace(/_/g, ' '));
  if (!o.is_specific) bits.push('incomplete public details');
  const pills = bits.length
    ? `<div class="pill-row">${bits.map((b) => `<span class="pill flat">${escapeHtml(String(b))}</span>`).join('')}</div>`
    : '';
  const link = o.url
    ? `<p><a class="promo-link" href="${escapeHtml(o.url)}" target="_blank" rel="noopener noreferrer">Open offer</a></p>`
    : '';
  const terms = o.terms
    ? `<h4>Terms</h4><p class="terms">${escapeHtml(o.terms.slice(0, 4000))}</p>`
    : '';
  const notes = o.eligibility_notes
    ? `<p><b>Eligibility notes:</b> ${escapeHtml(o.eligibility_notes)}</p>`
    : '';
  const planHtml = promoPlanHtml(o);
  // Asked of the data, not sniffed out of the rendered string.  ``escapeHtml``
  // escapes the quotes around ``class="plan-card"`` but not the substring
  // itself, so any offer whose caveat, note or team name happened to contain
  // "plan-card" flipped this true with no cards on screen — which suppressed
  // the "No strategy generated for this offer." fallback entirely and inverted
  // the block order.
  const planEntry = (PROMOS.plans || {})[promoOfferKey(o)];
  const hasCards = !!(planEntry && (planEntry.plans || []).length
                      && PROMO_STRATEGY_LABEL[planEntry.strategy]);
  // Concrete legs first when the planner produced them; the generic playbook
  // stays underneath (it covers the parts prices cannot: opt-ins, expiry,
  // account state).  Without cards, the playbook leads and the planner's
  // caveats say why nothing concrete was possible.
  const playbook = o.usage_guidance
    ? `${hasCards ? '<h4>Playbook</h4>' : '<h4>Best way to use</h4>'}<p>${escapeHtml(o.usage_guidance)}</p>`
    : (hasCards ? '' : '<h4>Best way to use</h4><p class="dim">No strategy generated for this offer.</p>');
  const guidance = hasCards ? planHtml + playbook : playbook + planHtml;
  return `<div class="promo-detail" id="promo-detail">
    <h4>${escapeHtml(summary)}</h4>
    ${pills}
    <p><b>Eligible:</b> ${escapeHtml(eligible)}${
      ineligible ? ` · <b>Excluded:</b> ${escapeHtml(ineligible)}` : ''}</p>
    ${notes}
    ${o.description ? `<p>${escapeHtml(o.description)}</p>` : ''}
    ${guidance}
    ${terms}
    ${link}
  </div>`;
}

/* ── the campaign table ──────────────────────────────────────────────────────
   One row per offer, ranked by a single normalized EV dollar figure so the
   operator can spend welcome offers best-first.  Every number is read from the
   payload the plan cards already render — this table computes nothing new
   except which of the planner's own figures is the comparable one:
     qualify_then_convert / rollover_grind -> entry.expected_value (computed);
     bonus_conversion -> unit x best conversion (flagged when the unit is the
     $100 placeholder); no_sweat / boost_locked -> best plan's settled floor.
   Anything unpriceable this run sinks to an unranked tail rather than being
   dressed up with a number. */

/* One spelling of the stored key. The reader built it from an offer and the
   writer pasted the prefix onto a row's `data-campaign-key`; they agreed, and
   nothing made them. */
const promoClaimKeyFor = (key) => 'promoClaimed:' + key;
function promoClaimKey(o) { return promoClaimKeyFor(promoOfferKey(o)); }

/** The ledger slips that spent this offer — kind ``promo`` with the offer
 *  named on them.  Unlike the checkbox, this is true in every browser. */
function promoLoggedSlips(o) {
  return betSlips().filter((s) => s.kind === 'promo'
    && s.promo_source === o.source && String(s.promo_offer_id) === String(o.offer_id));
}

function promoIsClaimed(o) {
  return storedValue(promoClaimKey(o), '') === '1' || promoLoggedSlips(o).length > 0;
}

function promoCampaignEV(o) {
  const entry = (PROMOS.plans || {})[promoOfferKey(o)];
  if (!entry) return null;
  const plans = entry.plans || [];
  if (entry.expected_value !== null && entry.expected_value !== undefined) {
    return { ev: Number(entry.expected_value), assumed: !!(entry.unit && entry.unit.assumed) };
  }
  if (!plans.length) return null;
  if (entry.strategy === 'bonus_conversion') {
    const best = plans.reduce((top, p) => {
      const pct = p.conversion_pct === null || p.conversion_pct === undefined ? null : Number(p.conversion_pct);
      return pct !== null && (top === null || pct > top) ? pct : top;
    }, null);
    const unit = entry.unit && entry.unit.amount ? Number(entry.unit.amount) : null;
    if (best === null || unit === null) return null;
    return { ev: unit * best / 100, assumed: !!(entry.unit && entry.unit.assumed) };
  }
  if (entry.strategy === 'no_sweat_hedge' || entry.strategy === 'boost_locked') {
    const best = plans.reduce((top, p) => {
      const floor = p.settled_cash === null || p.settled_cash === undefined ? null : Number(p.settled_cash);
      return floor !== null && (top === null || floor > top) ? floor : top;
    }, null);
    if (best === null) return null;
    return { ev: best, assumed: !!(entry.unit && entry.unit.assumed) };
  }
  return null;
}

function renderPromoCampaign(offers) {
  const table = el('promo-campaign');
  const note = el('promo-campaign-note');
  if (!table) return;
  const entryFor = (o) => (PROMOS.plans || {})[promoOfferKey(o)] || null;
  const rows = offers.map((o) => {
    const entry = entryFor(o);
    const value = promoCampaignEV(o);
    return { o, entry, value, claimed: promoIsClaimed(o) };
  });
  // Ranked first (claimed sink, then EV descending), unpriceable tail last.
  rows.sort((a, b) => {
    if (a.claimed !== b.claimed) return a.claimed ? 1 : -1;
    const av = a.value ? a.value.ev : null;
    const bv = b.value ? b.value.ev : null;
    if ((av === null) !== (bv === null)) return av === null ? 1 : -1;
    return (bv || 0) - (av || 0);
  });
  // Rank over the whole set, then narrow which ranked rows render: spending
  // order is a question about every offer, so under the sportsbook picker #4
  // stays #4 — renumbering a filtered row to #1 would assert it is the best
  // offer available, which it is not.
  rows.forEach((r, index) => {
    r.rank = r.claimed || r.value === null ? null : index + 1;
  });
  const shown = currentBrand
    ? rows.filter((r) => keepPromoBrand(r.o.source))
    : rows;
  // Positional, aligned with the handwritten <thead> below — this renderer
  // shadows the shared `table` helper with its own node, so it wires headings
  // through `wireSort` directly. Only Book and EV sort; a click reorders the
  // display while the campaign's own ranking above stays what it was.
  const campaignColumns = [
    null,
    { key: 'book', sort: (r) => promoBookLabel(r.o.source).toLowerCase() },
    null, null, null,
    { key: 'ev', sort: (r) => (r.value ? r.value.ev : -Infinity) },
    null, null, null, null,
  ];
  const ordered = applySort('promo-campaign', campaignColumns, shown);
  const soon = Date.now() + 7 * 24 * 3600 * 1000;
  const runState = (PROMOS.run && PROMOS.run.jurisdiction ? String(PROMOS.run.jurisdiction) : '').toUpperCase();
  const body = ordered.map(({ o, entry, value, claimed, rank }) => {
    const key = promoOfferKey(o);
    const regions = (o.eligible_regions || []);
    const states = regions.length ? regions.slice(0, 4).join(' ') + (regions.length > 4 ? '…' : '') : '—';
    const strategy = entry && PROMO_STRATEGY_LABEL[entry.strategy] ? PROMO_STRATEGY_LABEL[entry.strategy] : '—';
    const evCell = value === null
      ? '<td class="num dim">not priceable</td>'
      : `<td class="num ev-pos">$${value.ev.toFixed(2)}${value.assumed ? ' <span class="dim">/ $100</span>' : ''}</td>`;
    const qual = entry && entry.plans && entry.plans.length && entry.plans[0].qualifying_cost !== undefined
      ? `$${Number(entry.plans[0].qualifying_cost || 0).toFixed(2)}` : '—';
    const conv = (() => {
      if (!entry || !(entry.plans || []).length) return '—';
      const pcts = entry.plans.map((p) => p.conversion_pct).filter((v) => v !== null && v !== undefined);
      return pcts.length ? `${Math.max(...pcts.map(Number)).toFixed(1)}%` : '—';
    })();
    const ends = o.ends_at
      ? `<span class="${Date.parse(o.ends_at) < soon ? 'expiry-soon' : ''}">${escapeHtml(fmtClock(o.ends_at))}</span>`
      : '<span class="dim">—</span>';
    const confirmed = o.state_confirmed || !runState ? '' : ' <span class="pill warn">?</span>';
    return `<tr class="${claimed ? 'is-claimed' : ''}" data-campaign-key="${escapeHtml(key)}">
      <td class="num dim">${rank === null ? '' : rank}</td>
      <td>${escapeHtml(promoBookLabel(o.source))}${confirmed}</td>
      <td class="dim">${escapeHtml(states)}</td>
      <td>${escapeHtml((o.summary || o.title).slice(0, 60))}</td>
      <td class="dim">${escapeHtml(strategy)}</td>
      ${evCell}
      <td class="num dim">${escapeHtml(qual)}</td>
      <td class="num">${escapeHtml(conv)}</td>
      <td>${ends}</td>
      <td class="claim-cell">${promoLoggedSlips(o).length
        ? '<input type="checkbox" aria-label="claimed" checked disabled title="Logged in My bets"/> <span class="pill flat">logged</span>'
        : `<input type="checkbox" aria-label="claimed" ${claimed ? 'checked' : ''}/>`}</td>
    </tr>`;
  }).join('');
  const emptyBody = shown.length ? '' : `<tr><td colspan="10" class="dim">${
    currentBrand && rows.length
      ? `No ${escapeHtml(brandLabel(currentBrand))} offers in this promo scrape.`
      : 'No offers in this promo scrape.'}</td></tr>`;
  table.innerHTML = `<thead><tr>
      <th>#</th><th>Book</th><th>States</th><th>Offer</th><th>Strategy</th>
      <th>EV</th><th>Qual cost</th><th>Conv</th><th>Ends</th><th>Done</th>
    </tr></thead><tbody>${body || emptyBody}</tbody>`;
  if (note) {
    const ranked = shown.filter((r) => !r.claimed && r.value !== null).length;
    const claimed = shown.filter((r) => r.claimed).length;
    const against = currentBrand && rows.length !== shown.length
      ? ` · ranked against all ${rows.length} offers`
      : '';
    note.textContent = `${ranked} priceable · ${claimed} done${against} · checkbox is per-browser, "logged" is from My bets · click Book or EV to sort`;
  }
  wireSort(table, campaignColumns, 'promo-campaign', renderPromos);
  table.querySelectorAll('tbody tr').forEach((tr) => {
    const key = tr.getAttribute('data-campaign-key');
    const box = tr.querySelector('input[type="checkbox"]');
    if (box) {
      box.addEventListener('click', (ev) => {
        ev.stopPropagation();
        storeValue(promoClaimKeyFor(key), box.checked ? '1' : '0');
        renderPromos();
      });
    }
    tr.addEventListener('click', () => {
      selectedPromoKey = key;
      renderPromos();
      const row = document.querySelector(`.promo-row[data-promo-key="${CSS.escape(key)}"]`);
      if (row) row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  });
}

function renderPromos() {
  const nav = el('nav-promos');
  const summary = el('promo-summary');
  const stats = el('promo-stats');
  const list = el('promo-list');
  const listNote = el('promo-list-note');
  const health = el('promo-health');
  const note = el('promo-note');
  if (!list || !stats) return;

  ensurePromoFilters();
  const run = PROMOS.run;
  const offers = PROMOS.offers || [];
  // The sportsbook picker's cut. The campaign below still receives the whole
  // set — spending order is ranked against every offer — while the counts, the
  // browsing list and the nav badge describe what the reader is narrowed to.
  const scoped = promoOffers();
  const healthRows = PROMOS.health || [];
  const brands = promoBrandCoverage(healthRows);

  if (nav) {
    nav.textContent = String(scoped.length);
  }
  if (summary) {
    summary.textContent = run
      ? `${scoped.length.toLocaleString()} offer${scoped.length === 1 ? '' : 's'}${
          currentBrand ? ` at ${brandLabel(currentBrand)}` : ''}`
      : 'no scrape yet';
  }
  if (note) {
    note.textContent = run
      ? `promo run #${run.id}${run.ok ? '' : ' (degraded)'} · ${brands.ok}/${brands.total} brands`
      : 'hit Scrape promos';
  }

  if (!run) {
    const campaign = el('promo-campaign');
    if (campaign) campaign.innerHTML = '';
    const campaignNote = el('promo-campaign-note');
    if (campaignNote) campaignNote.textContent = 'no scrape yet';
    stats.innerHTML = `
      <div class="stat"><b>0</b><span>offers</span></div>
      <div class="stat"><b>—</b><span>last scrape</span></div>
      <div class="stat"><b>0</b><span>brands ok</span></div>`;
    list.innerHTML = `<div class="empty">No promo scrape yet. Use <b>Scrape promos</b> in the left rail
      (via <code>--serve</code>) to pull signup bonuses, boosts, and free bets
      (first-party catalogs + TheLines failover).</div>`;
    if (listNote) listNote.textContent = 'no scrape yet';
    if (health) health.innerHTML = '<span class="dim">No promo scrape yet.</span>';
    return;
  }

  stats.innerHTML = `
    <div class="stat"><b>${scoped.length.toLocaleString()}</b><span>offers</span></div>
    <div class="stat"><b>${escapeHtml(fmtClock(run.started_at))}</b><span>scraped</span></div>
    <div class="stat"><b>${brands.ok}/${brands.total || 0}</b><span>brands ok</span></div>`;

  // The campaign ranks every offer, always: the toolbar never narrows the
  // ranking, and the sportsbook picker narrows only which ranked rows the
  // table shows — a book the reader holds no account at is not somewhere they
  // can spend, but hiding it must not renumber what remains.
  renderPromoCampaign(offers);

  const kindFilter = (el('promo-kind') && el('promo-kind').value) || '';
  const sourceFilter = (el('promo-source') && el('promo-source').value) || '';
  const regionFilter = (el('promo-region') && el('promo-region').value) || '';
  const q = ((el('promo-q') && el('promo-q').value) || '').trim().toLowerCase();
  const filtered = scoped.filter((o) => {
    if (kindFilter && o.kind !== kindFilter) return false;
    if (sourceFilter && o.source !== sourceFilter) return false;
    if (regionFilter) {
      const eligible = o.eligible_regions || [];
      const ineligible = new Set(o.ineligible_regions || []);
      if (ineligible.has(regionFilter)) return false;
      // Known eligible list: must include the selected region.
      // Unknown eligibility (empty list) stays visible — public copy often omits geo.
      if (eligible.length && !eligible.includes(regionFilter)) return false;
    }
    if (!q) return true;
    const code = (o.metadata && o.metadata.promo_code) || '';
    const hay = `${o.title} ${o.summary || ''} ${o.description || ''} ${o.raw_kind || ''} ${code} ${(o.eligible_regions || []).join(' ')}`.toLowerCase();
    return hay.includes(q);
  });

  if (listNote) {
    const meta = PROMOS.plan_meta;
    // Say where the concrete plans came from — or why there are none.  A
    // panel that silently mixes "no plans computed" with "computed and all
    // gated out" hides the difference that matters.
    // ``reason`` is tested first.  It was tested second, behind a truthy
    // ``odds_run_id`` — and ``empty_odds_run`` is the one failure that carries
    // a run id, so the single state meaning "the odds run this priced against
    // held no quotes" reported itself as "plans priced from odds run #N".  The
    // note exists precisely to separate "computed, all gated out" from "never
    // computed", and it got that backwards on the case where it mattered.
    const planNote = !meta
      ? ''
      : (meta.reason
        ? ` · no plans: ${String(meta.reason).replace(/_/g, ' ')}`
        : (meta.odds_run_id
          ? ` · plans priced from odds run #${meta.odds_run_id}`
          : ''));
    listNote.textContent = (filtered.length === scoped.length
      ? `${scoped.length} total · click a row for usage tips`
      : `${filtered.length} of ${scoped.length} · click a row for usage tips`) + planNote;
  }

  if (!filtered.length) {
    list.innerHTML = `<div class="empty">${offers.length
      ? 'Nothing matches these filters.'
      : 'This promo scrape stored no offers.'}</div>`;
  } else {
    list.innerHTML = `<div class="promo-list">${filtered.map((o) => {
      const key = promoOfferKey(o);
      const open = key === selectedPromoKey;
      const end = o.ends_at
        ? `ends ${escapeHtml(fmtClock(o.ends_at))}`
        : 'no end date';
      const link = o.url
        ? `<a class="promo-link" href="${escapeHtml(o.url)}" target="_blank" rel="noopener noreferrer">Open offer</a>`
        : '';
      const teaser = o.summary || o.description || '';
      const desc = teaser
        ? `<p class="desc">${escapeHtml(teaser.slice(0, 280))}${teaser.length > 280 ? '…' : ''}</p>`
        : '';
      const login = o.requires_login ? ' · login for details' : '';
      const vague = o.is_specific ? '' : ' · needs details';
      const regions = (o.eligible_regions || []).length
        ? ` · ${(o.eligible_regions || []).slice(0, 6).join(', ')}${(o.eligible_regions || []).length > 6 ? '…' : ''}`
        : '';
      const code = o.metadata && o.metadata.promo_code
        ? ` · code ${escapeHtml(o.metadata.promo_code)}`
        : '';
      const via = (o.metadata && o.metadata.feed === 'thelines') || String(o.source || '').startsWith('tl_')
        ? ' · via TheLines'
        : '';
      /* The state verdict, in the odds side's "not reachable from {ST}" voice.
         False means the offer's own copy never named the run's state — it is
         stored and shown under that label rather than silently dropped. */
      const runState = (PROMOS.run && PROMOS.run.jurisdiction ? String(PROMOS.run.jurisdiction) : '').toUpperCase();
      const unconfirmed = !o.state_confirmed && runState && runState !== 'GLOBAL'
        ? ` · <span class="pill warn">not confirmed for ${escapeHtml(runState)}</span>`
        : '';
      return `<article class="promo-row${open ? ' is-open' : ''}" data-promo-key="${escapeHtml(key)}" tabindex="0" role="button" aria-expanded="${open ? 'true' : 'false'}">
        <div>
          <p class="title">${escapeHtml(o.summary || o.title)}</p>
          <p class="meta">${escapeHtml(promoBookLabel(o.source))} · ${escapeHtml(promoKindLabel(o.kind))}${
            o.product ? ` · ${escapeHtml(o.product)}` : ''}${login}${vague}${regions}${code}${via}${unconfirmed}</p>
          ${desc}
        </div>
        <div class="side">
          <span>${end}</span>
          ${link}
        </div>
        ${open ? promoDetailHtml(o) : ''}
      </article>`;
    }).join('')}</div>`;
    list.querySelectorAll('.promo-row').forEach((node) => {
      const activate = (ev) => {
        if (ev.target.closest && (ev.target.closest('a') || ev.target.closest('.promo-detail'))) return;
        const key = node.getAttribute('data-promo-key');
        selectedPromoKey = selectedPromoKey === key ? null : key;
        renderPromos();
      };
      node.addEventListener('click', activate);
      node.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          activate(ev);
        }
      });
    });
  }

  if (health) {
    health.innerHTML = healthRows.length
      ? healthRows.map((h) => {
        const cls = h.ok ? 'pill flat' : 'pill bad';
        const detail = h.ok
          ? `${h.offer_count} offer${h.offer_count === 1 ? '' : 's'}`
          : (h.error_kind || 'failed');
        return `<span class="${cls}" title="${escapeHtml(h.error_message || '')}">${
          escapeHtml(promoBookLabel(h.source_key))}: ${escapeHtml(String(detail))}</span>`;
      }).join('')
      : '<span class="dim">No per-book health for this run.</span>';
  }
}

/* ── arbitrage ───────────────────────────────────────────────────────────── */

/** The arbitrage bundle for the run being viewed, in the view being asked for.
 *
 *  Detection is Python and this page is a static file, so the offshore variant
 *  cannot be recomputed here — `_arb_payload` precomputes both and this picks.
 *  A payload built before the toggle existed has no `with_offshore`, so turning
 *  the switch on falls back to the bundle it does have rather than blanking the
 *  panel; that older bundle was computed with the offshore books allowed, which
 *  is exactly what the switch is asking for. */
function arbBundle() {
  const bags = DATA.arbs || {};
  const bag = bags[String(currentRunId)] || bags[currentRunId] || null;
  if (!bag) return null;
  return showOffshore ? (bag.with_offshore || bag) : bag;
}

/** Every leg placeable from this jurisdiction — the money question.
 *  `takeable` is composed in Python beside the locality labels; the fallback
 *  is for a payload written before the key existed, where the best available
 *  answer was the weaker "has at least one reachable leg". */
function arbTakeable(o) {
  // null is Python's "no verdict" (an ungoverned run has no state to be
  // takeable from) and falls back like an absent key.
  return o.takeable !== undefined && o.takeable !== null
    ? Boolean(o.takeable) : !o.no_local_leg;
}

/** One line per leg the reader cannot place from here, for the card's pill. */
function arbBlockers(o) {
  const legs = o.non_local_legs !== undefined
    ? o.non_local_legs
    : (o.legs || []).filter((leg) => leg.non_local_label)
        .map((leg) => ({ source: leg.source, origin_label: leg.origin_label || '' }));
  // Once per book: a three-way hedge at one exchange is one blocker, not two.
  return [...new Set(legs.map((leg) =>
    `${book(leg.source)}${leg.origin_label ? ` (${leg.origin_label})` : ''}`))];
}

function arbKey(o) {
  return [o.event_key, o.market, o.period, o.side, o.line].join('|');
}

/** The positions the panel lists for the run being viewed.
 *
 *  The US-only bundle and the offshore-admitted one are two detector runs, not
 *  a list and its filter: with Bovada admitted the best price on a side moves
 *  there and a DraftKings/FanDuel position becomes a DraftKings/Bovada one. So
 *  with the switch off the panel shows the US-only positions *and* the
 *  offshore-admitted positions that have no US-only twin, each marked by
 *  `arbTakeable` — a position needing a book you cannot bet from the US is
 *  shown and labelled, not dropped. With the switch on the offshore bundle is
 *  the whole answer, as before. Takeable positions sort first. */
function arbPositions(bag) {
  if (!bag) return [];
  const base = bag.opportunities || [];
  let all = base;
  // Only when the locality rule governs the run: on an ungoverned (GLOBAL)
  // run the offshore extras carry no takeable verdict, and merging them put
  // green pills and "guaranteed $" on Bovada/Pinnacle positions with the
  // offshore switch off. Ungoverned runs keep the old behaviour — the switch
  // is the only door to the offshore view.
  if (!showOffshore && bag.with_offshore && bag.governed) {
    const seen = new Set(base.map(arbKey));
    const extra = (bag.with_offshore.opportunities || [])
      .filter((o) => !seen.has(arbKey(o)))
      .filter((o) => (o.legs || []).some((leg) => isUsUnavailable(leg.source)));
    all = base.concat(extra);
  }
  return all.slice().sort((a, b) =>
    (arbTakeable(b) - arbTakeable(a)) || ((b.margin_pct || 0) - (a.margin_pct || 0)));
}

/** The watchlist table: the detector computes every compared market's
 *  distance from arbitrage and used to discard it at the gate. These are the
 *  closest few, whole-run (no sport/book filter — a near-cross is worth
 *  seeing even when the board is narrowed), each saying whether its legs are
 *  takeable from here (no pill on an ungoverned run — there is no state to be
 *  takeable from) and whether its prices were even simultaneous. −0.20%
 *  crosses when one book moves a tick: that is the "run a fresh scrape now"
 *  signal — unless the legs were captured minutes apart, which the row then
 *  says instead of claiming a market state that never existed. */
function nearMissHtml(misses) {
  if (!misses.length) return '';
  const rows = misses.map((m) => {
    const legs = (m.legs || []).map((leg) =>
      `${escapeHtml(book(leg.source))} ${escapeHtml(leg.selection)} ${Number(leg.decimal_odds).toFixed(3)}${
        leg.local === false ? ` <span class="pill warn">${escapeHtml(leg.origin_label || 'not takeable')}</span>` : ''}`
    ).join(' · ');
    const away = nick(m.away_team || '');
    const home = nick(m.home_team || '');
    const gap = Number(m.margin_pct);
    return `<tr>
      <td><b>${escapeHtml(away)}</b> <span class="dim">@</span> <b>${escapeHtml(home)}</b>
        <div class="dim">${escapeHtml(sportLabel(m.sport || ''))} · ${escapeHtml(m.market)}/${escapeHtml(m.period)}${
          m.line !== null && m.line !== undefined ? ` · ${escapeHtml(fmtLine(m.line, m.market))}` : ''}</div></td>
      <td class="num"><b>${gap.toFixed(2)}%</b><div class="dim">${
        m.simultaneous === false
          ? `prices ${Math.round((m.observed_spread_seconds || 0) / 60)}m apart when scraped — never one market state`
          : gap >= 0 ? 'crosses at this size' : `short by ${Math.abs(gap).toFixed(2)}%`}</div></td>
      <td>${legs}</td>
      <td>${m.takeable === false
        ? '<span class="pill warn">not takeable</span>'
        : m.takeable === true
        ? '<span class="pill">takeable books</span>'
        : ''}</td>
    </tr>`;
  }).join('');
  return `<p class="dim" style="margin:0 0 8px">Closest to crossing in this scrape — the whole run,
       no sport or book filter. A market a fraction short becomes an arbitrage when one book
       moves a tick, so these are what a fresh scrape is for.</p>
      <div class="scroll-wrap"><table class="arb-legs"><thead><tr>
        <th>Market</th><th>Best margin</th><th>Legs (best assignment)</th><th></th>
      </tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderArb() {
  const bag = arbBundle();
  const nav = el('nav-arb');
  const summary = el('arb-summary');
  const stats = el('arb-stats');
  const list = el('arb-list');
  const rejected = el('arb-rejected');
  const note = el('arb-note');

  if (!detailLoaded(currentRunId)) {
    if (nav) nav.textContent = '';
    summary.textContent = 'not embedded for this scrape';
    stats.innerHTML = '';
    list.innerHTML = `<div class="arb-empty">This scrape's prices are not embedded in the page, so
      arbitrage was not computed for it. Open <a href="#history">History</a> for an embedded scrape,
      or rebuild with a larger <code>--quote-runs</code>.</div>`;
    rejected.innerHTML = `<div class="arb-empty">Nothing to show.</div>`;
    if (note) note.textContent = 'latest scrape only';
    return;
  }

  if (!bag) {
    if (nav) nav.textContent = '—';
    summary.textContent = 'not computed';
    stats.innerHTML = '';
    list.innerHTML = `<div class="arb-empty">No arbitrage payload for this scrape. Rebuild the dashboard.</div>`;
    rejected.innerHTML = `<div class="arb-empty">Nothing to show.</div>`;
    return;
  }

  const opps = arbPositions(bag).filter((o) => {
    if (currentSport && o.sport !== currentSport) return false;
    // "Has a leg at the picked book", never "entirely at it": an arbitrage
    // always spans two counterparties, so the whole position is kept — every
    // leg rendered, origin marks intact — or dropped.
    if (currentBrand && !(o.legs || []).some((leg) => keepBrand(leg.source))) return false;
    return true;
  });
  // The headline numbers are money claims, so they count only positions with a
  // leg the reader can reach from this jurisdiction. A wholly-foreign position
  // is rendered below with its labels, but calling it "takeable" or adding its
  // profit to "guaranteed $" would make the labels a footnote to a lie.
  const takeable = opps.filter(arbTakeable);
  const blocked = opps.length - takeable.length;
  // A $0-floor position protects a push and wins nothing; counting it in the
  // headline as a plain opportunity is how a zero-profit NFL moneyline read
  // as the day's edge.
  const zeroFloor = takeable.filter((o) => Number(o.guaranteed_profit) === 0).length;
  const withLeg = currentBrand ? ` with a ${brandLabel(currentBrand)} leg` : '';
  if (nav) nav.textContent = String(takeable.length);
  const blockedClause = blocked ? ` · ${blocked} not takeable` : '';
  const floorClause = zeroFloor ? ` (${zeroFloor} floor $0)` : '';
  summary.textContent = takeable.length
    ? `${takeable.length} takeable${floorClause}${withLeg}${blockedClause} · ${bag.comparable_group_count} cross-book markets`
    : `none takeable${withLeg}${blockedClause} · ${bag.comparable_group_count} cross-book markets checked`;
  if (note) {
    // A non-takeable position is shown with the leg that blocks it named, so
    // the note says what "not takeable" means rather than inviting a switch.
    const delta = blocked
      ? `${blocked} shown but not takeable — a leg is at a book you cannot bet from here`
      : '';
    const base = takeable.length
      ? `stakes sized to $${Number(bag.stake || 100).toFixed(0)} total · sport filter applies${
          currentBrand ? ` · ${brandLabel(currentBrand)} legs only` : ''}`
      : (opps.length
        ? "nothing takeable from this jurisdiction — the positions below are somewhere else's prices"
        // "no edge today" is a claim about prices and needs something to have
        // been priced. With nothing compared it says the market was tight when
        // the truth is that nothing was measured — but the count alone does not
        // say *why* not, so this reports the fact and leaves the cause to the
        // reasons listed below.
        : ((bag.comparable_group_count || 0) === 0
          ? 'nothing was compared in this scrape — not evidence about prices either way'
          : 'same detector as collector arb — empty usually means no edge today'));
    note.textContent = delta ? `${base} · ${delta}` : base;
  }

  const best = takeable.length
    ? Math.max(...takeable.map((o) => o.margin_pct || 0))
    : 0;
  const profit = takeable.reduce((n, o) => n + (o.guaranteed_profit || 0), 0);
  /* Summed from each position's own after-tax floor, not by taxing the total:
     the deduction cap bites per position, and a position whose floor goes
     negative after tax has to drag the sum down rather than be netted away by a
     better one. Suppressed entirely when no rate is set, and when any position
     lacks a basis, so the tile never mixes taxed and untaxed money. */
  const takeableNet = taxOn()
    ? takeable.map((o) => afterTaxFloor(o.outcome_profits))
    : [];
  const netProfit = takeableNet.length && takeableNet.every((v) => v !== null)
    ? takeableNet.reduce((n, v) => n + v, 0)
    : null;
  const guaranteedSub = netProfit === null
    ? `on $${Number(bag.stake || 100).toFixed(0)} each`
    : `${usdSigned(netProfit)} after tax · on $${Number(bag.stake || 100).toFixed(0)} each`;
  stats.innerHTML = [
    ['positions', takeable.length, 'risk-free right now'],
    ['best margin', takeable.length ? `${best.toFixed(2)}%` : '—', 'headline edge'],
    ['guaranteed $', takeable.length ? profit.toFixed(2) : '—', guaranteedSub],
    ['not takeable', blocked, 'a leg out of reach from here'],
    ['markets checked', bag.comparable_group_count || 0, `${bag.group_count || 0} total groups`],
  ].map(([name, value, sub]) =>
    `<div class="stat"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b><small>${escapeHtml(sub)}</small></div>`
  ).join('');

  // Positions whose every leg is at a venue the operator cannot *reach* from this
  // state. Shown below with labels rather than dropped, but still said out loud in
  // one place, because a reader skimming for a count should not have to read every
  // leg to learn how many positions are wholly somewhere else's. A number shipped
  // in the payload and rendered nowhere is the gap this note exists to close.
  //
  // "Reachable", not "licensed", and the difference is not pedantry: the marking is
  // `registry.takeable_from_state`, which admits Kalshi — a federally regulated
  // venue no state licenses as a sportsbook — so a Kalshi position is unlabelled.
  // Under the word "license" this note invited the reader to conclude their
  // prediction-market edge had been flagged for licensing, which is both false and
  // the exact inversion an earlier round of this rule shipped. Polymarket is the
  // other way round and is labelled: the registered adapter reads the offshore
  // book, a different legal entity from the CFTC-designated Polymarket US.
  // Counted from the cards actually rendered below, not from the bundle total:
  // with a sport filter active the two differ, and "shown below with labels"
  // must be true of what is below. The bundle's own count still matters — any
  // flagged positions the filter hides are named so the note and the whole-run
  // total cannot silently disagree.
  const flagged = opps.filter((o) => o.no_local_leg).length;
  // Which reader-side filter is doing the hiding, named. The whole-run total
  // (`bag.non_local_flagged`, composed in Python) is never recomputed here.
  // `opps` is no longer a subset of the US-only bundle — `arbPositions` can
  // append offshore extras the Python count never saw — so the difference is
  // clamped: it answers "how many flagged positions did a filter hide", and
  // extras can only push it below zero, never hide anything.
  const flaggedHidden = Math.max(0, Number(bag.non_local_flagged || 0) - flagged);
  const narrowedBy = [currentSport ? 'sport' : '', currentBrand ? 'book' : '']
    .filter(Boolean).join(' and ');
  const flaggedNote = flagged
    ? `<div class="arb-empty">${flagged} ${flagged === 1 ? 'position has' : 'positions have'}
       no leg at a venue you can reach from this jurisdiction — no licence here and
       no nationwide US access. They are shown below with labels: informational,
       somewhere else's prices, not an edge you can take from here.${
         flaggedHidden > 0 ? ` (${flaggedHidden} more ${
           narrowedBy ? `outside the ${narrowedBy} filter` : 'in the whole scrape'}.)` : ''}</div>`
    : (flaggedHidden > 0 && narrowedBy
      ? `<div class="arb-empty">${flaggedHidden} flagged position(s) with no reachable leg
         are hidden by the ${narrowedBy} filter.</div>`
      : '');

  if (!opps.length) {
    // "A clean board" is a claim about prices, and it is only honest when
    // something was actually compared. Two ways it was not, and the panel used
    // to assert it in both:
    //
    //  - nothing was comparable at all. A global run whose only US-reachable
    //    venues were two prediction markets drops to one when the offshore half
    //    is set aside, and one venue compares against nothing. `arb.py` also
    //    returns before counting when every fixture has already started, so the
    //    count reaching zero does not say *which* — hence "nothing was
    //    compared" and not a diagnosis.
    //  - a sport filter is on. `comparable_group_count` is the whole bag's while
    //    `opps` is filtered, so the count describes a board the reader is not
    //    looking at, and it cannot speak for this sport at all.
    const checked = bag.comparable_group_count || 0;
    const offshoreWouldHelp =
      !showOffshore && ((bag.with_offshore || {}).comparable_group_count || 0) > checked;
    const sportName = currentSport ? escapeHtml(sportLabel(currentSport)) : '';
    const brandName = currentBrand ? escapeHtml(brandLabel(currentBrand)) : '';
    list.innerHTML = flaggedNote + (checked === 0
      ? `<div class="arb-empty">Nothing was compared in this scrape.
        Arbitrage needs one market priced at two venues you can bet at, at the same
        time — no pair here reached that, for the reasons below. An empty result is
        not evidence that the board is tight.${
          offshoreWouldHelp ? ` Books you can't bet from the US are excluded; turn them on to
          see the comparison as context.` : ''}</div>`
      : currentBrand
        ? `<div class="arb-empty">No takeable arbitrage has a ${brandName} leg${
            sportName ? ` in ${sportName}` : ''} in this scrape.
          An arbitrage always needs two books, so this asks which positions you could
          place part of at ${brandName} — not which are entirely there. The refusals
          below cover every book.</div>`
        : currentSport
          ? `<div class="arb-empty">No takeable arbitrage in ${sportName}.
            The detector looked at ${checked} cross-book markets across the whole scrape;
            how many of those were ${sportName} is not broken out, so this says nothing
            about how tight that board is. The refusals below cover every sport.</div>`
          : `<div class="arb-empty">No takeable arbitrage in this scrape.
            The detector looked at ${checked} cross-book markets and refused the
            rest for the reasons below — that is a clean board, not a missing feature.</div>`);
  } else {
    list.innerHTML = flaggedNote + opps.map((o, i) => arbCard(o, i)).join('');
  }

  const missHtml = nearMissHtml(bag.near_misses || []);

  const diags = bag.diagnostics || [];
  if (!diags.length && !missHtml) {
    rejected.innerHTML = `<div class="arb-empty">No near-misses recorded for this scrape.</div>`;
  } else {
    rejected.innerHTML = missHtml + (diags.length ? `<div class="arb-reject">${diags.map((d) =>
      `<span><code>${escapeHtml(d.code)}</code> <b>${escapeHtml(String(d.count))}</b></span>`
    ).join('')}</div>` : '');
  }
}

/** Where one leg's venue is, as a pill beside the book name.
 *
 *  Every leg gets one, reachable or not. The old pill appeared only on foreign
 *  legs, which left the reader to infer that an unmarked book was local — true,
 *  but an inference from an absence, and the same absence covers a leg on a run
 *  the locality rule does not govern at all. Saying it on every leg costs one
 *  pill and removes the inference.
 *
 *  The text is the payload's, never rebuilt here: `origin_label` and
 *  `origin_detail` are composed in `src.coverage`, beside the one phrase
 *  `collector arb`, `lines` and the SMS print for a foreign leg — so the extra
 *  detail this page shows can never contradict the shorter thing they say.
 *  Those surfaces keep their one-line tag; only the tab was asked to say more.
 *  `non_local_label` is the fallback for a payload written before origins
 *  existed. */
function originPill(leg) {
  const label = leg.origin_label || leg.non_local_label;
  if (!label) return '';
  // `origin_local` is the same answer the position-level flag is computed from,
  // so a pill can never read "reachable" on a leg the headline counted as not.
  const local = leg.origin_local !== undefined
    ? leg.origin_local
    : !leg.non_local_label;
  const suffix = local || !leg.non_local_label
    ? ''
    : ` · ${leg.non_local_label}`;
  const title = leg.origin_detail ? ` title="${escapeHtml(leg.origin_detail)}"` : '';
  return ` <span class="pill ${local ? '' : 'warn'}"${title}>${
    escapeHtml(label + suffix)}</span>`;
}

function arbCard(o, index) {
  const away = nick(o.away_participant || o.away_team);
  const home = nick(o.home_participant || o.home_team);
  const line = o.line === null || o.line === undefined
    ? ''
    : ` · ${escapeHtml(fmtLine(o.line, o.market))}`;
  const side = o.side ? ` · ${escapeHtml(o.side)}` : '';
  const when = o.commence_time ? fmtClock(o.commence_time) : '';
  const legs = (o.legs || []).map((leg) => {
    const selLine = leg.line === null || leg.line === undefined
      ? ''
      : ` ${fmtLine(leg.line, o.market)}`;
    const net = Math.abs((leg.net_decimal_odds || 0) - (leg.decimal_odds || 0)) > 1e-9
      ? ` <span class="dim">net ${Number(leg.net_decimal_odds).toFixed(3)}</span>`
      : '';
    return `<tr>
      <td><b>${escapeHtml(book(leg.source))}</b>${originPill(leg)}</td>
      <td>${escapeHtml(leg.selection)}${escapeHtml(selLine)}</td>
      <td class="num">${escapeHtml(fmtAmerican(leg.american_odds))}${net}</td>
      <td class="num">$${Number(leg.stake).toFixed(2)}</td>
      <td class="num">$${Number(leg.payout).toFixed(2)}</td>
      <td class="num">${betLink(leg.link)}</td>
    </tr>`;
  }).join('');
  /* Per outcome, and after tax per outcome when a rate is set. This is the line
     where the feature earns its place: before tax an arb pays the same in every
     outcome, and after tax it does not, because the outcome with the larger
     gross win carries the larger bill. */
  const outcomes = (o.outcome_profits || []).map((row) => {
    const gross = `${escapeHtml(row.label)} ${
      Number(row.profit) >= 0 ? '+' : ''}${Number(row.profit).toFixed(2)}`;
    if (!taxOn()) return gross;
    const net = afterTax(row.profit, row);
    if (net === null) return gross;
    return `${gross} (${net >= 0 ? '+' : ''}${net.toFixed(2)} after tax)`;
  }).join(' · ');
  const netFloor = taxOn() ? afterTaxFloor(o.outcome_profits) : null;
  /* Beside the guaranteed figure, never instead of it. The pre-tax floor is
     what the position is detected and ranked on; this says what is left of it. */
  const afterTaxKpi = netFloor === null ? '' :
    `<span>after tax <strong>${usdSigned(netFloor)}</strong></span>`;
  const notes = (o.notes || []).length
    ? `<div class="arb-notes">${o.notes.map((n) => escapeHtml(n)).join(' · ')}</div>`
    : '';
  const limit = o.max_total_stake != null
    ? `<span>book limit caps bankroll at <strong>$${Number(o.max_total_stake).toFixed(2)}</strong></span>`
    : '';
  return `<article class="arb-card">
    <div class="arb-top">
      <div>
        <b>${escapeHtml(away)} <span class="dim">@</span> ${escapeHtml(home)}</b>
        <div class="arb-meta">${escapeHtml(sportLabel(o.sport))}
          · ${escapeHtml(o.market)}/${escapeHtml(o.period)}${side}${line}
          ${when ? ` · ${escapeHtml(when)}` : ''}
          · <a href="${escapeHtml(href('fixture', o.event_key))}">open game</a>
        </div>
      </div>
      <span>${Number(o.guaranteed_profit) === 0
        ? '<span class="arb-pill warn">floor $0 · protects a push, wins nothing</span> '
        : ''}${o.takeable === true
        ? '<span class="arb-pill ok">takeable</span> '
        : o.takeable === false
        ? `<span class="arb-pill warn">${o.no_local_leg
            ? `not takeable · no leg reachable from this jurisdiction · ${escapeHtml(arbBlockers(o).join(', '))}`
            : `not takeable · ${escapeHtml(arbBlockers(o).join(', '))} out of reach`}</span> `
        : ''}<span class="arb-pill ok">${Number(o.margin_pct).toFixed(2)}% edge</span></span>
    </div>
    <div class="arb-kpis">
      <span>guaranteed <strong>$${Number(o.guaranteed_profit).toFixed(2)}</strong></span>
      <span>on <strong>$${Number(o.total_stake).toFixed(2)}</strong></span>
      <span>ROI <strong>${Number(o.roi_pct).toFixed(2)}%</strong></span>
      ${afterTaxKpi}
      <span>Σ implied <strong>${Number(o.sum_implied).toFixed(4)}</strong></span>
      ${limit}
    </div>
    <table class="arb-legs">
      <thead><tr>
        <th>Book</th><th>Bet</th><th>Odds</th><th>Stake</th><th>Pays</th><th>Place</th>
      </tr></thead>
      <tbody>${legs}</tbody>
    </table>
    <div class="arb-outcomes">outcomes: ${outcomes}</div>
    ${notes}
    ${BETS_WRITABLE ? `<div class="bl-acts" style="justify-content:flex-start;margin-top:8px">
      <button type="button" class="bl-btn primary" data-bl="toggle" data-bl-index="${index}"
        >Log this bet</button>
      <span class="dim" style="font-size:12px">stakes come across pre-filled</span>
    </div>` : ''}
    ${arbLogDrawer(o, index)}
  </article>`;
}

/* ── placed bets ─────────────────────────────────────────────────────────────
   The only writing surface on the page. Everything else here renders what a
   scrape found; this renders what the operator says they did with it, out of a
   sibling database the collector never touches.

   Two rules shape the whole section. Writing needs the local server, so a
   ``file://`` copy shows the ledger and disables every control rather than
   offering buttons that silently do nothing. And every write answers with the
   *whole* ledger, which replaces ``BETS`` — the page never patches its own copy
   from what it hoped the server did, so what is on screen is what is in the
   file even when a request half-succeeds. */

const BET_FALLBACK_STATUSES = ['pending', 'won', 'lost', 'push', 'void', 'cashout'];
let BETS = DATA.bets || { slips: [], summary: null, statuses: BET_FALLBACK_STATUSES };
const BET_STATUSES = (BETS.statuses && BETS.statuses.length)
  ? BETS.statuses : BET_FALLBACK_STATUSES;
/** A served page can write; a file:// page is a read-only ledger. Same rule the
 *  Scrape buttons follow, for the same reason: only the server has the file. */
const BETS_WRITABLE = typeof location !== 'undefined' && location.protocol === 'http:';

const betSummary = () => (BETS && BETS.summary) || {};
const betSlips = () => (BETS && BETS.slips) || [];

/** Money, always with its sign visible when the sign is the point. */
function usd(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return (n < 0 ? '−$' : '$') + Math.abs(n).toFixed(2);
}
function usdSigned(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return (n < 0 ? '−$' : '+$') + Math.abs(n).toFixed(2);
}
const plClass = (v) => (v === null || v === undefined || Math.abs(v) < 0.005
  ? '' : (v > 0 ? 'up' : 'down'));

/** One line naming a logged position, from whatever the snapshot kept.
 *
 *  A slip logged off an arb card has teams; one typed in by hand may have
 *  nothing but a leg's description, and printing " @ " around two empty strings
 *  is worse than printing the description. */
function slipTitle(slip) {
  const away = slip.away_team ? nick(slip.away_team) : '';
  const home = slip.home_team ? nick(slip.home_team) : '';
  if (away && home) return `${away} @ ${home}`;
  if (home || away) return home || away;
  const first = (slip.legs || [])[0];
  if (first && first.selection) return first.selection;
  if (slip.kind === 'arb') return 'Arbitrage position';
  return slip.kind === 'promo' ? 'Promo play' : 'Bet';
}

function slipMeta(slip) {
  const bits = [];
  if (slip.sport) bits.push(sportLabel(slip.sport));
  if (slip.league) bits.push(leagueLabel(slip.league));
  if (slip.market) {
    bits.push(marketOf(slip.market, slip.sport).plain
      + (slip.period ? ` · ${periodOf(slip.period).plain}` : ''));
  }
  if (slip.line !== null && slip.line !== undefined) bits.push(fmtLine(slip.line, slip.market));
  if (slip.commence_time) bits.push(fmtClock(slip.commence_time));
  if (slip.kind === 'promo' && slip.promo_source) bits.push(`promo at ${promoBookLabel(slip.promo_source)}`);
  bits.push('placed ' + fmtClock(slip.placed_at));
  return bits.join(' · ');
}

/* ── talking to the ledger ───────────────────────────────────────────────── */

/** POST (or GET) one ledger call and adopt whatever ledger comes back.
 *
 *  Returns ``{ok, error}``. Callers report the error next to the control that
 *  caused it rather than in one banner at the top: a stake box that rejects
 *  what was typed has to say so where the typing happened. */
async function betApi(path, body) {
  if (!BETS_WRITABLE) {
    return { ok: false, error: 'Open the dashboard with --serve to change the ledger.' };
  }
  let res;
  try {
    const init = body === undefined
      ? { cache: 'no-store' }
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        };
    // Keep every target as a literal so the static-page network guard can
    // prove that ledger controls only talk to this same-origin server.
    if (path === '/api/bets') res = await fetch('/api/bets', init);
    else if (path === '/api/bets/log') res = await fetch('/api/bets/log', init);
    else if (path === '/api/bets/leg') res = await fetch('/api/bets/leg', init);
    else if (path === '/api/bets/slip') res = await fetch('/api/bets/slip', init);
    else if (path === '/api/bets/settle') res = await fetch('/api/bets/settle', init);
    else if (path === '/api/bets/delete') res = await fetch('/api/bets/delete', init);
    else return { ok: false, error: 'Unknown ledger action.' };
  } catch (err) {
    return { ok: false, error: (err && err.message) ? err.message : String(err) };
  }
  const payload = await res.json().catch(() => ({}));
  if (!res.ok || !payload.ok) {
    return { ok: false, error: payload.error || res.statusText || ('HTTP ' + res.status) };
  }
  if (payload.bets) adoptBets(payload.bets);
  return { ok: true, result: payload.result };
}

/** Replace the page's ledger and repaint whatever is showing it. */
function adoptBets(payload) {
  BETS = payload;
  markStale('bets');
  if (here.panel === 'bets') ensurePanel('bets');
  scheduleNavCounts();
}

function betMessage(id, text, kind) {
  const node = el(id);
  if (!node) return;
  node.textContent = text || '';
  node.classList.toggle('bad', kind === 'bad');
  node.classList.toggle('good', kind === 'good');
}

/* ── the panel ───────────────────────────────────────────────────────────── */

function renderBets() {
  const nav = el('nav-bets');
  const summary = betSummary();
  const slips = betSlips();
  // The sportsbook picker narrows which *slips* are listed — a slip with at
  // least one leg at the picked book, folded through `ledgerBrand`, exact arms
  // only. The nav badge, the bankroll strip and the per-book row stay whole:
  // `betSummary()` is settlement arithmetic computed in Python, the ledger is
  // a record of what was placed rather than a view of a scrape, and re-deriving
  // money figures client-side is the failure the movement table already taught.
  const shown = currentBrand
    ? slips.filter((slip) => (slip.legs || []).some((leg) => keepLedgerBook(leg.book)))
    : slips;
  if (nav) nav.textContent = slips.length ? String(slips.length) : '';

  const note = el('bets-summary');
  if (note) {
    note.textContent = BETS.error
      ? `ledger unreadable — ${BETS.error}`
      : (slips.length
        ? `${slips.length} position${slips.length === 1 ? '' : 's'} · ${
          summary.pending_legs || 0} leg${summary.pending_legs === 1 ? '' : 's'} still open`
        : 'nothing logged yet');
  }

  const stats = el('bets-stats');
  if (stats) {
    const roi = summary.roi_pct === null || summary.roi_pct === undefined
      ? '—' : Number(summary.roi_pct).toFixed(2) + '%';
    const record = summary.record || {};
    /* The one after-tax figure on the page that is not a projection: it is
       computed from the legs the operator actually settled, and its gross
       winnings are the number a return asks for. Python supplies the basis for
       the same reason it supplies the rest of this strip — see the comment
       above about not re-deriving ledger money here. */
    const netProfit = taxOn() ? afterTax(summary.profit, summary.tax_basis) : null;
    const netRoi = netProfit !== null && summary.settled_stake
      ? `${(netProfit / summary.settled_stake * 100).toFixed(2)}%`
      : null;
    stats.innerHTML = [
      ['profit', usdSigned(summary.profit),
        netProfit === null ? 'settled bets only' : `${usdSigned(netProfit)} after tax`],
      ['staked', usd(summary.staked), `${summary.leg_count || 0} legs logged`],
      ['still open', usd(summary.open_stake), `${summary.pending_legs || 0} unsettled`],
      ['ROI', roi, netRoi === null
        ? `on ${usd(summary.settled_stake)} settled`
        : `${netRoi} after tax · on ${usd(summary.settled_stake)} settled`],
      ['record', `${record.won || 0}-${record.lost || 0}-${
        (record.push || 0) + (record.void || 0)}`, 'won-lost-push'],
    ].map(([name, value, sub]) =>
      `<div class="stat"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b>`
      + `<small>${escapeHtml(sub)}</small></div>`).join('');
  }

  const listNote = el('bets-list-note');
  if (listNote) {
    const narrowed = currentBrand && slips.length
      ? ` · ${shown.length} of ${slips.length} position${slips.length === 1 ? '' : 's'} ${
          shown.length === 1 ? 'has' : 'have'} a ${brandLabel(currentBrand)} leg`
      : '';
    listNote.textContent = (BETS_WRITABLE
      ? 'newest first · change a stake, price or result and it saves'
      : 'newest first · view only — start with --serve to edit') + narrowed;
  }

  const list = el('bets-list');
  if (list) {
    list.innerHTML = shown.length
      ? shown.map((slip) => slipCard(slip)).join('')
      : (currentBrand && slips.length
        ? `<div class="arb-empty">No logged bets have a ${escapeHtml(brandLabel(currentBrand))} leg.
          The ledger records what you typed, so a bet logged as "DK app" sits under its own
          name — pick it in the sidebar, or clear the sportsbook picker to see every position.</div>`
        : `<div class="arb-empty">No bets logged yet. The quickest way in is the
          <a href="#arb">Arbitrage</a> panel — every card there has a
          <b>Log this bet</b> button that carries the books, prices and stakes across.
          ${BETS_WRITABLE ? '' : '<br/>This page was opened from a file, so the ledger is read-only. Start it with <code>python -m src.report --serve 8765 --open</code> to log bets.'}</div>`);
  }

  const books = el('bets-books');
  if (books) {
    const rows = summary.by_book || [];
    // Whole, deliberately, with the picked book's row marked: this strip is the
    // per-book ledger total, and hiding a row would make it disagree with the
    // bankroll figures above it.
    books.innerHTML = rows.length
      ? rows.map((row) =>
        `<span${currentBrand && keepLedgerBook(row.book) ? ' class="picked"' : ''}>${
          escapeHtml(book(row.book))} <b>${escapeHtml(usdSigned(row.profit))}</b>`
        + ` <span class="dim">${row.bets} bet${row.bets === 1 ? '' : 's'},`
        + ` ${escapeHtml(usd(row.staked))} staked`
        + `${row.pending ? `, ${escapeHtml(usd(row.open))} open` : ''}</span></span>`
      ).join('')
      : '<span class="dim">Nothing logged yet.</span>';
  }

  const status = el('bl-status');
  if (status && (!status.options || !status.options.length)) {
    status.innerHTML = BET_STATUSES.map((value) =>
      `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  }
  const books2 = el('bl-books');
  if (books2 && (!books2.options || !books2.options.length)) {
    books2.innerHTML = (DATA.sources || []).map((s) =>
      `<option value="${escapeHtml(s.key)}">${escapeHtml(s.label || s.key)}</option>`).join('');
  }
  const addBtn = el('bl-add');
  if (addBtn && !BETS_WRITABLE) {
    addBtn.disabled = true;
    addBtn.classList.add('is-file');
    addBtn.title = 'Start the dashboard with: python -m src.report --serve 8765 --open';
    betMessage('bl-add-msg', 'View only. Run with --serve 8765 --open to log bets.', '');
  }
}

function slipCard(slip) {
  const legs = slip.legs || [];
  const rw = BETS_WRITABLE;
  const rows = legs.map((leg) => {
    const bookCell = rw
      ? `<input class="bl-text" value="${escapeHtml(leg.book || '')}"
          data-bl-field="book" data-bl-leg="${leg.id}" list="bl-books"
          aria-label="Book"/>`
      : `<b>${escapeHtml(book(leg.book))}</b>`;
    const selectionCell = rw
      ? `<input class="bl-text" value="${escapeHtml(leg.selection || '')}"
          data-bl-field="selection" data-bl-leg="${leg.id}" aria-label="Bet"/>`
      : escapeHtml(leg.selection || '—');
    const priceCell = rw
      ? `<input class="odds" value="${escapeHtml(leg.american_odds === null
        || leg.american_odds === undefined ? '' : leg.american_odds)}"
        data-bl-field="american_odds" data-bl-leg="${leg.id}" inputmode="numeric"
        aria-label="US odds"/>`
      : escapeHtml(leg.american_odds === null || leg.american_odds === undefined
        ? '—' : fmtAmerican(leg.american_odds));
    const stakeCell = rw
      ? `<input class="money" value="${Number(leg.stake).toFixed(2)}"
        data-bl-field="stake" data-bl-leg="${leg.id}" inputmode="decimal" aria-label="Stake"/>`
      : escapeHtml(usd(leg.stake));
    const statusCell = rw
      ? `<select data-bl-field="status" data-bl-leg="${leg.id}" aria-label="Result">${
        BET_STATUSES.map((value) =>
          `<option value="${escapeHtml(value)}"${value === leg.status ? ' selected' : ''}>${
            escapeHtml(value)}</option>`).join('')}</select>`
      : `<span class="bl-status ${escapeHtml(leg.status)}">${escapeHtml(leg.status)}</span>`;
    // The returned box is only offered once there is an outcome. A pending leg
    // has not paid anything, and the ledger refuses the pair outright, so
    // showing the box would be offering an edit that can only fail.
    const backCell = leg.status === 'pending'
      ? '<span class="dim">—</span>'
      : (rw
        ? `<input class="money" value="${leg.returned === null || leg.returned === undefined
          ? '' : Number(leg.returned).toFixed(2)}" data-bl-field="returned"
          data-bl-leg="${leg.id}" inputmode="decimal" aria-label="Returned"
          placeholder="${Number(leg.to_return).toFixed(2)}"/>`
        : escapeHtml(usd(leg.returned)));
    const linked = leg.link_url
      ? ` <a href="${escapeHtml(leg.link_url)}" target="_blank" rel="noopener noreferrer"
          title="open the slip at the book">↗</a>`
      : '';
    return `<tr>
      <td>${bookCell}${linked}</td>
      <td>${selectionCell}${leg.line === null || leg.line === undefined
        ? '' : ' ' + escapeHtml(fmtLine(leg.line, slip.market))}</td>
      <td class="num">${priceCell}</td>
      <td class="num">${stakeCell}${leg.stake_kind && leg.stake_kind !== 'cash'
        ? ` <span class="dim">${escapeHtml(leg.stake_kind === 'bonus' ? 'credit' : leg.stake_kind)}</span>` : ''}</td>
      <td class="num">${escapeHtml(usd(leg.to_return))}</td>
      <td>${statusCell}</td>
      <td class="num">${backCell}</td>
      <td class="num bl-pl ${plClass(leg.profit)}">${escapeHtml(
        leg.profit === null || leg.profit === undefined ? '—' : usdSigned(leg.profit))}</td>
    </tr>`;
  }).join('');

  const kpis = [
    `staked <strong>${escapeHtml(usd(slip.stake))}</strong>`,
    slip.profit === null || slip.profit === undefined
      ? `open <strong>${escapeHtml(usd(slip.open_stake))}</strong>`
      : `P/L <strong class="bl-pl ${plClass(slip.profit)}">${escapeHtml(usdSigned(slip.profit))}</strong>`,
  ];
  if (slip.open_stake && slip.profit !== null && slip.profit !== undefined) {
    kpis.push(`still open <strong>${escapeHtml(usd(slip.open_stake))}</strong>`);
  }
  if (slip.margin_pct !== null && slip.margin_pct !== undefined) {
    kpis.push(`edge when placed <strong>${Number(slip.margin_pct).toFixed(2)}%</strong>`);
  }
  if (slip.expected_profit !== null && slip.expected_profit !== undefined) {
    kpis.push(`expected <strong>${escapeHtml(usdSigned(slip.expected_profit))}</strong>`);
  }
  // Said out loud rather than folded into the totals: a cashout with no amount
  // entered is money whose fate the ledger does not know, and averaging it in as
  // zero would print a loss nobody took.
  if (slip.unpriced_legs) {
    kpis.push(`<span class="dim">${slip.unpriced_legs} settled leg${
      slip.unpriced_legs === 1 ? '' : 's'} with no amount entered</span>`);
  }

  const acts = BETS_WRITABLE
    ? `<div class="bl-acts">
        <button type="button" class="bl-btn" data-bl="settle" data-bl-slip="${slip.id}"
          data-bl-status="won">All won</button>
        <button type="button" class="bl-btn" data-bl="settle" data-bl-slip="${slip.id}"
          data-bl-status="lost">All lost</button>
        <button type="button" class="bl-btn" data-bl="settle" data-bl-slip="${slip.id}"
          data-bl-status="push">All push</button>
        <button type="button" class="bl-btn" data-bl="settle" data-bl-slip="${slip.id}"
          data-bl-status="pending">Reopen</button>
        <button type="button" class="bl-btn danger" data-bl="delete" data-bl-slip="${slip.id}"
          >Delete</button>
      </div>`
    : '';

  const openGame = slip.event_key
    ? ` · <a href="${escapeHtml(href('fixture', slip.event_key))}">open game</a>` : '';

  return `<article class="bl-slip" data-slip="${slip.id}">
    <div class="bl-top">
      <div>
        <b>${escapeHtml(slipTitle(slip))}</b>
        <div class="bl-meta">${escapeHtml(slipMeta(slip))}${openGame}</div>
      </div>
      <span class="bl-status ${escapeHtml(slip.status)}">${escapeHtml(
        slip.kind === 'arb' || slip.kind === 'promo' ? `${slip.kind} · ${slip.status}` : slip.status)}</span>
    </div>
    <div class="bl-kpis">${kpis.join('')}</div>
    <table class="bl-legs">
      <thead><tr>
        <th>Book</th><th>Bet</th><th>Odds</th><th>Stake</th><th>Pays</th>
        <th>Result</th><th>Got back</th><th>P/L</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${acts}
    ${rw
      ? `<textarea class="bl-note" data-bl-slip-field="note" data-bl-slip="${slip.id}"
          placeholder="Add a note…" aria-label="Bet note">${escapeHtml(slip.note || '')}</textarea>`
      : (slip.note ? `<p class="bl-note">${escapeHtml(slip.note)}</p>` : '')}
    <p class="bl-msg" data-bl-msg="${slip.id}"></p>
  </article>`;
}

/* ── logging from what was already scraped ───────────────────────────────── */

/** The drawer under an arbitrage card: every leg pre-filled, stakes editable.
 *
 *  This is the point of the whole feature. The detector already knows the books,
 *  the prices, the selections and the stake split that makes the position
 *  risk-free; retyping any of that by hand is how a logged bet stops matching
 *  the bet that was placed. The reader changes one number — how much — and the
 *  rest travels with it. */
function arbLogDrawer(o, index) {
  if (!BETS_WRITABLE) return '';
  const legs = (o.legs || []).map((leg) => ({
    book: leg.source,
    selection: leg.selection,
    line: leg.line,
    american_odds: leg.american_odds,
    decimal_odds: leg.decimal_odds,
    stake: leg.stake,
    link_url: (leg.link && leg.link.url) || '',
  }));
  const slip = {
    kind: 'arb',
    sport: o.sport,
    league: o.league,
    event_key: o.event_key,
    home_team: o.home_team,
    away_team: o.away_team,
    commence_time: o.commence_time,
    market: o.market,
    period: o.period,
    side: o.side || '',
    line: o.line,
    margin_pct: o.margin_pct,
    expected_profit: o.guaranteed_profit,
    source_run_id: currentRunId,
    legs: legs,
  };
  return slipDrawerHtml(slip, index, { total: o.total_stake });
}

/** The drawer itself: one row per leg with an editable stake, and the slip
 *  the reader posts carried on the node so the handler never rebuilds it.
 *  Shared by the arbitrage card and the promo plan card; ``total`` adds the
 *  bankroll box that rescales every leg in the detector's ratio, which only
 *  an arbitrage position wants — a promo's credit leg is the offer's size. */
function slipDrawerHtml(slip, index, { total }) {
  const legs = slip.legs || [];
  const rows = legs.map((leg, i) =>
    `<tr>
      <td>${escapeHtml(book(leg.book))}</td>
      <td>${escapeHtml(leg.selection)}${leg.line === null || leg.line === undefined
        ? '' : ' ' + escapeHtml(fmtLine(leg.line, slip.market))}</td>
      <td class="num">${escapeHtml(fmtAmerican(leg.american_odds))}</td>
      <td class="num"><label class="bl-field"><span class="sr-only">Stake at ${
        escapeHtml(book(leg.book))}</span>
        <input class="money" data-bl-stake="${i}" inputmode="decimal"
          value="${Number(leg.stake).toFixed(2)}"/></label>${
        leg.stake_kind && leg.stake_kind !== 'cash'
          ? ` <span class="dim">${escapeHtml(leg.stake_kind === 'bonus' ? 'credit' : leg.stake_kind)}</span>` : ''}</td>
    </tr>`).join('');
  const totalBox = total === null || total === undefined ? '' : `<label class="bl-field"><span>Total $</span>
        <input class="money" data-bl-total="${index}" inputmode="decimal"
          value="${Number(total).toFixed(2)}"/></label>`;
  return `<div class="bl-drawer" hidden data-bl-drawer="${index}"
      data-bl-payload="${escapeHtml(JSON.stringify(slip))}">
    <table class="bl-legs"><tbody>${rows}</tbody></table>
    <div class="bl-form" style="margin-top:8px">
      ${totalBox}
      <button type="button" class="bl-btn primary" data-bl="log-slip" data-bl-index="${index}"
        >Log ${legs.length} leg${legs.length === 1 ? '' : 's'}</button>
      <button type="button" class="bl-btn" data-bl="cancel" data-bl-index="${index}">Cancel</button>
    </div>
    <p class="bl-msg" data-bl-msg="slip-${index}"></p>
  </div>`;
}

/** Rescale every leg to a new bankroll, keeping the ratio the detector chose.
 *
 *  Rounding each leg independently is what makes a $100 position add up to
 *  $99.99; the last leg absorbs the remainder so the stakes always sum to the
 *  number the reader typed. */
function rescaleArbStakes(drawer) {
  const totalBox = drawer.querySelector('[data-bl-total]');
  const boxes = [...drawer.querySelectorAll('[data-bl-stake]')];
  if (!totalBox || !boxes.length) return;
  const wanted = Number(totalBox.value);
  if (!isFinite(wanted) || wanted <= 0) return;
  let base;
  try {
    base = JSON.parse(drawer.getAttribute('data-bl-payload')).legs.map((leg) => Number(leg.stake));
  } catch (_) { return; }
  const sum = base.reduce((n, v) => n + v, 0);
  if (!(sum > 0)) return;
  let assigned = 0;
  boxes.forEach((boxNode, i) => {
    if (i === boxes.length - 1) {
      boxNode.value = Math.max(0, wanted - assigned).toFixed(2);
      return;
    }
    const share = Math.round((base[i] / sum) * wanted * 100) / 100;
    assigned += share;
    boxNode.value = share.toFixed(2);
  });
}

/** Read a drawer back out: the scraped payload, with the stakes as edited. */
function drawerPayload(drawer) {
  const slip = JSON.parse(drawer.getAttribute('data-bl-payload'));
  for (const boxNode of drawer.querySelectorAll('[data-bl-stake]')) {
    const i = Number(boxNode.getAttribute('data-bl-stake'));
    if (slip.legs[i]) slip.legs[i].stake = boxNode.value;
  }
  return slip;
}

/** One button's worth of markup for logging a single scraped price. */
function singleLogButton(payload, label) {
  if (!BETS_WRITABLE) return '';
  return `<button type="button" class="bl-btn" data-bl="log-single"
    data-bl-payload="${escapeHtml(JSON.stringify(payload))}"
    title="Add this to My bets">${escapeHtml(label || 'log')}</button>`;
}

/* ── one delegated handler for every bet control on the page ─────────────── */

/** Wired once, at the document, because the controls live in three different
 *  panels and every one of them is rebuilt by its renderer. Re-attaching per
 *  render is how a page ends up firing one click five times. */
function wireBetControls() {
  if (typeof document === 'undefined' || !document.body
      || document.body.dataset.betControls === '1') return;
  document.body.dataset.betControls = '1';

  document.body.addEventListener('click', (ev) => {
    const node = ev.target && ev.target.closest ? ev.target.closest('[data-bl]') : null;
    if (!node) return;
    const kind = node.getAttribute('data-bl');
    if (kind === 'toggle' || kind === 'cancel') {
      const index = node.getAttribute('data-bl-index');
      const drawer = document.querySelector(`[data-bl-drawer="${index}"]`);
      if (drawer) drawer.hidden = kind === 'cancel' ? true : !drawer.hidden;
      return;
    }
    if (kind === 'log-slip') return void logDrawerSlip(node);
    if (kind === 'log-single') return void logSingleBet(node);
    if (kind === 'settle') return void settleSlip(node);
    if (kind === 'delete') return void deleteSlip(node);
  });

  // ``change`` rather than ``input``: a stake is saved when the reader is done
  // typing it, not on every keystroke — otherwise "50" is written as 5 then 50,
  // and a backspaced field posts a blank the ledger has to refuse.
  document.body.addEventListener('change', (ev) => {
    const node = ev.target;
    if (!node || !node.getAttribute) return;
    if (node.hasAttribute('data-bl-total')) {
      const drawer = node.closest('[data-bl-drawer]');
      if (drawer) rescaleArbStakes(drawer);
      return;
    }
    const field = node.getAttribute('data-bl-field');
    if (field) { void editLeg(node, field); return; }
    const slipField = node.getAttribute('data-bl-slip-field');
    if (slipField) void editSlip(node, slipField);
  });
}

async function logDrawerSlip(node) {
  const index = node.getAttribute('data-bl-index');
  const drawer = document.querySelector(`[data-bl-drawer="${index}"]`);
  if (!drawer) return;
  node.disabled = true;
  const box = drawer.querySelector(`[data-bl-msg="slip-${index}"]`);
  if (box) { box.textContent = 'Saving…'; box.classList.remove('bad', 'good'); }
  const slip = drawerPayload(drawer);
  const res = await betApi('/api/bets/log', slip);
  node.disabled = false;
  if (!res.ok) {
    if (box) { box.textContent = res.error; box.classList.add('bad'); }
    return;
  }
  // The drawer belongs to the panel it was drawn in, which this write did not rebuild.
  if (box) { box.textContent = 'Logged — see My bets.'; box.classList.add('good'); }
  drawer.hidden = true;
  // Spending a promo is what "done" means.  ``betApi`` already adopted the
  // ledger's echo, so the offer reads as logged; repaint the Campaign table.
  if (slip.kind === 'promo') renderPromos();
}

async function logSingleBet(node) {
  let payload;
  try {
    payload = JSON.parse(node.getAttribute('data-bl-payload'));
  } catch (_) { return; }
  const stake = window.prompt(
    `How much did you put on this at ${book(payload.legs[0].book)}?`, '50');
  if (stake === null) return;
  payload.legs[0].stake = stake;
  node.disabled = true;
  const was = node.textContent;
  node.textContent = 'saving…';
  const res = await betApi('/api/bets/log', payload);
  node.disabled = false;
  node.textContent = res.ok ? 'logged ✓' : was;
  if (!res.ok) window.alert('Could not log that bet: ' + res.error);
}

async function editLeg(node, field) {
  const legId = Number(node.getAttribute('data-bl-leg'));
  if (!legId) return;
  const slipNode = node.closest('[data-slip]');
  const box = slipNode
    ? slipNode.querySelector(`[data-bl-msg="${slipNode.getAttribute('data-slip')}"]`) : null;
  const value = node.value;
  const body = { leg_id: legId };
  // A cleared "got back" box means "I do not know what this returned", which is
  // a real state (an unpriced cashout) and distinct from zero. Sending the empty
  // string preserves it; sending 0 would invent a total loss.
  body[field] = (field === 'returned' && String(value).trim() === '') ? null : value;
  if (box) { box.textContent = 'Saving…'; box.classList.remove('bad', 'good'); }
  const res = await betApi('/api/bets/leg', body);
  if (!res.ok) {
    // The panel was not replaced, so the box still shows the rejected text. Say
    // why, and leave it there to be corrected rather than silently reverting it.
    if (box) { box.textContent = res.error; box.classList.add('bad'); }
    else window.alert(res.error);
  }
}

async function editSlip(node, field) {
  const slipId = Number(node.getAttribute('data-bl-slip'));
  if (!slipId) return;
  const box = node.closest('[data-slip]')?.querySelector(`[data-bl-msg="${slipId}"]`);
  const body = { slip_id: slipId };
  body[field] = node.value;
  if (box) { box.textContent = 'Saving…'; box.classList.remove('bad', 'good'); }
  const res = await betApi('/api/bets/slip', body);
  if (!res.ok) {
    if (box) { box.textContent = res.error; box.classList.add('bad'); }
    else window.alert(res.error);
  }
}

async function settleSlip(node) {
  const slipId = Number(node.getAttribute('data-bl-slip'));
  const status = node.getAttribute('data-bl-status');
  const res = await betApi('/api/bets/settle', { slip_id: slipId, status: status });
  if (!res.ok) window.alert('Could not settle that bet: ' + res.error);
}

async function deleteSlip(node) {
  const slipId = Number(node.getAttribute('data-bl-slip'));
  if (!window.confirm('Delete this logged bet? This cannot be undone.')) return;
  const res = await betApi('/api/bets/delete', { slip_id: slipId });
  if (!res.ok) window.alert('Could not delete that bet: ' + res.error);
}

/** The hand-entry form: one leg, one book, whatever the operator types. */
async function addManualBet() {
  const btn = el('bl-add');
  const bookKey = (el('bl-book').value || '').trim();
  const what = (el('bl-what').value || '').trim();
  const odds = (el('bl-odds').value || '').trim();
  const stake = (el('bl-stake').value || '').trim();
  const status = el('bl-status').value || 'pending';
  if (!bookKey) { betMessage('bl-add-msg', 'Which book took the bet?', 'bad'); return; }
  if (!what) { betMessage('bl-add-msg', 'Say what the bet was.', 'bad'); return; }
  btn.disabled = true;
  betMessage('bl-add-msg', 'Saving…', '');
  const res = await betApi('/api/bets/log', {
    kind: 'single',
    note: '',
    legs: [{
      book: bookKey,
      selection: what,
      american_odds: odds,
      stake: stake,
      status: status,
    }],
  });
  btn.disabled = false;
  if (!res.ok) { betMessage('bl-add-msg', res.error, 'bad'); return; }
  el('bl-what').value = '';
  el('bl-odds').value = '';
  el('bl-stake').value = '';
  el('bl-status').value = 'pending';
  betMessage('bl-add-msg', 'Logged.', 'good');
}

function wireBetForm() {
  const btn = el('bl-add');
  if (!btn) return;
  btn.addEventListener('click', () => { void addManualBet(); });
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
  const run = runById.get(currentRunId) || { sports: [] };
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
    // "Feeds", because this league-level count includes view-only mirrors —
    // unlike the sport grid's counterparty-only Books column above it, which
    // used to sit as "Books 0" directly over an MLB row reading "Books 10",
    // the 10 styled as clearing the two-book bar.  A feed count is a true
    // statement; a book count that includes mirrors is not.
    { band: 'totals', label: 'Feeds', num: true,
      cell: (r) => cell(r.books.length, r.books.length ? '' : 'dim') },
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

/* How many distinct scopes a health row refused.

 * Modern rows always set ``scopes_failed`` whenever they set refusals.  The
 * additive migration that added the column backfills ``0`` onto older rows
 * whose ``scopes_refused`` messages are still populated, and treating that
 * zero as authoritative hid every pre-upgrade refusal behind a green
 * "responded normally" pill.  A stated zero with messages is therefore read
 * as "the distinct count was never written", not as "nothing was refused". */
function scopesFailedOf(h) {
  const messages = h.scopes_refused || [];
  const stated = h.scopes_failed;
  if (stated == null) return messages.length;
  if (stated === 0 && messages.length) {
    // ``"{scope}: {error}"`` — split on the first colon-space, not the first
    // colon.  Pinnacle's fallback scopes are ``MLB:246`` / ``MLB:247``.
    const names = new Set(messages.map((m) => String(m).split(': ', 1)[0]));
    return names.size || messages.length;
  }
  return stated;
}

function renderSources() {
  const run = runById.get(currentRunId);
  // Books is the one panel the sportsbook picker does not narrow, and this
  // says so where the reader is looking. A venue that failed produced no rows
  // at all, so a row-based filter would erase exactly the venues this panel
  // exists to report — the picked book's cards are marked instead of the rest
  // being hidden. Written in both states: the region must never render empty.
  const sourcesNote = el('sources-note');
  if (sourcesNote) {
    sourcesNote.textContent = currentBrand
      ? 'Every venue that answered this scrape. The sportsbook picker does not hide any of '
        + 'them: a venue you filtered out still answered, and hiding it here would make this '
        + `page's health count disagree with the scrape it describes. ${
          brandLabel(currentBrand)}'s own feeds are marked.`
      : 'Every venue that answered this scrape.';
  }
  if (!run) {
    el('nav-sources').textContent = '';
    el('source-cards').innerHTML = '<div class="empty">No scrapes yet.</div>';
    blankRegion('skips');
    return;
  }
  const byKey = new Map(run.sources.map((h) => [h.key, h]));
  el('nav-sources').textContent = run.sources.filter((h) => h.ok).length + '/' + run.sources.length;

  if (!run.sources.length) {
    // A silently blank section reads as "no venues", which is a different claim.
    el('source-cards').innerHTML =
      `<div class="empty">No per-venue detail was recorded for this collection. Its
       ${run.quote_count.toLocaleString()} prices are still stored — see Games and All prices.</div>`;
    blankRegion('skips');
    return;
  }

  el('source-cards').innerHTML = DATA.sources.map((src) => {
    const h = byKey.get(src.key);
    if (!h) return '';
    // A source that answered but was refused part of what it was asked for is
    // not "responded normally", and said so nowhere on this page: the columns
    // reached the stored payload and stopped there, so a book that had lost
    // most of its leagues rendered byte-identically to a clean one.
    const refused = scopesFailedOf(h);
    const truncated = (h.scopes_truncated || []).length;
    const pill = !h.ok
      ? `<span class="pill bad"><i></i>${escapeHtml(label(h.error_kind) || 'failed')}</span>`
      : refused
        ? `<span class="pill warn"><i></i>refused ${refused} of ${
            h.scopes_requested || refused} scopes</span>`
        : truncated
          ? `<span class="pill warn"><i></i>cut short on ${truncated} scope${
              truncated === 1 ? '' : 's'}</span>`
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
    if (truncated) {
      cells.push(['cut short', `${truncated} scope${truncated === 1 ? '' : 's'}`]);
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
    // Books is the one panel the switch does not filter: a venue you cannot bet
    // at is still a venue that answered, and hiding it here would make the
    // health count on this page disagree with the run it describes. It is
    // marked instead, and says which state the switch has it in.
    const offshore = isUsUnavailable(src.key);
    const offshoreMark = offshore ? '<span class="us-off">can\'t bet from US</span>' : '';
    const offshoreNote = offshore
      ? `<p class="dim" style="font-size:11.5px">Not bettable from the United States${
          showOffshore
            ? ' — its prices are being included anyway, so a position using it cannot be placed.'
            : " — its prices are excluded from the board and from arbitrage. Turn on “Include books you can't bet from the US” to see them."}</p>`
      : '';
    return `<a class="src${offshore ? ' is-us-off' : ''}${
      currentBrand && keepBrand(src.key) ? ' picked' : ''}" href="${escapeHtml(href('book', src.key))}">
      <div class="src-top"><div><b>${escapeHtml(src.label)}</b>${offshoreMark}<code>${escapeHtml(src.host)}</code></div>${kindPill}${pill}</div>
      <p>${escapeHtml(src.what)}</p>
      ${offshoreNote}
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
    { label: 'What it was', cell: (r) => cell(skipReasonLabel(r.reason)) },
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

/** This run's rows, narrowed to the chosen feed type.
 *
 *  Applied before the games are summarised rather than after, so a game whose
 *  only prices are republished disappears under "first-party only" instead of
 *  surviving with an empty book list — and so the book counts on each card
 *  describe the feeds actually being shown. */
function feedFilteredRows() {
  const node = el('events-feed');
  const want = (node && node.value) || '';
  // `sportRows()`: the Games tab compares the books, so the sportsbook picker
  // narrows its *games* (in `filteredGameEvents`) and never its rows.
  const rows = sportRows();
  if (!want) return rows;
  return rows.filter((r) => feedKind(str(r[COL.source])) === want);
}

/** Games on the Games tab after search / league / feed / book filters. */
function filteredGameEvents() {
  const all = brandGames(eventSummaries(feedFilteredRows()));
  const qNode = el('events-q');
  const bookNode = el('events-book');
  const q = ((qNode && qNode.value) || '').trim().toLowerCase();
  const book = (bookNode && bookNode.value) || '';
  return all.filter((e) => {
    if (currentLeague && e.league !== currentLeague) return false;
    if (book && !e.bySource.has(book)) return false;
    if (!q) return true;
    const hay = [e.key, nick(e.homeRaw), nick(e.awayRaw),
      fullName(e.homeRaw), fullName(e.awayRaw),
      e.league, leagueLabel(e.league), e.sport, sportLabel(e.sport)]
      .join(' ').toLowerCase();
    return hay.includes(q);
  });
}

function buildEventsFilters() {
  buildLeaguePicker();
  // Off the feed-filtered rows, so the two pickers compose: under "first-party
  // only" the book list holds first-party books, and a republished book left
  // selected from before cannot silently empty the panel with nothing saying
  // why. ``fillSelect`` drops a selection its new list does not contain.
  const rows = feedFilteredRows();
  const bookNode = el('events-book');
  if (bookNode) {
    fillSelect(bookNode,
      [...new Set(rows.map((r) => str(r[COL.source])))].sort(),
      'any book', book);
  }
}

function renderEvents() {
  // `sportRows()` + `brandGames`: the coverage grid's columns are the books, so
  // the sportsbook picker narrows which games are listed and keeps every
  // column — a one-column heat matrix of one book against itself answers
  // nothing.
  const rows = sportRows();
  buildEventsFilters();
  const all = brandGames(eventSummaries(rows));
  const events = filteredGameEvents();
  el('nav-events').textContent = events.length;
  const note = el('events-filter-note');
  if (note) {
    // The feed picker belongs in this test for the same reason the other three
    // do: it can hide games, and a count that says "41" over a list of four is
    // the one thing this note exists to prevent.
    const narrowing = currentLeague || (el('events-book') && el('events-book').value)
      || (el('events-feed') && el('events-feed').value)
      || ((el('events-q') && el('events-q').value.trim()));
    note.textContent = narrowing && all.length
      ? `showing ${events.length} of ${all.length}`
      : '';
  }
  renderBrowseGames(el('events-games'), el('events-games-note'), events);

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

  table(el('coverage'), [
    { band: 'the game', label: 'Starts', cell: (e) => cell(fmtClock(e.commence), 'dim') },
    { band: 'the game', label: 'Sport', cell: (e) => cell(sportLabel(e.sport), 'dim') },
    { band: 'the game', label: 'League',
      hint: 'Recorded for coverage only — never used to join two books together',
      cell: (e) => cell(leagueLabel(e.league), 'dim') },
    { band: 'the game', label: 'Game',
      cell: (e) => html(`${escapeHtml(nick(e.awayRaw))} <span class="dim">at</span> ${escapeHtml(nick(e.homeRaw))}`) },
    { band: 'the game', label: 'Game ID',
      hint: "This tool's own name for the game, so books can be compared",
      cell: (e) => cell(e.key, 'mono dim') },
    ...keys.map((k) => ({
      band: mode === 'source' ? 'prices, per sportsbook' : 'prices, per kind of bet',
      label: heading(k), num: true, cell: (e) => heat(pickMap(e).get(k) || 0),
    })),
    // "Feeds": bySource counts every feed with rows, view-only mirrors
    // included — the same misnomer relabelled at the league grid.
    { band: 'totals', label: 'Feeds', num: true, cell: (e) => cell(e.bySource.size) },
    { band: 'totals', label: 'Prices', num: true, cell: (e) => cell(e.rows.length) },
  ], events, { className: 'cov', noun: 'games', empty: all.length
      ? 'No games match these filters.'
      : 'No prices stored for this scrape.',
               go: (e) => href('fixture', e.key) });

  // Deliberately does NOT repoint the fixture panel any more. It used to, to keep
  // that panel populated while off screen; the router now builds it on arrival with
  // ``defaultFixture()``, so the only thing the repoint still did was overwrite the
  // game a reader had deliberately opened. That happened for real: type into the
  // Games search, click a game inside the 140ms debounce window, and the pending
  // renderEvents fired afterwards, replaced the fixture panel's heading and prices
  // with the search's top hit, and left the breadcrumb naming the game you clicked.
  // Nothing corrected it afterwards, because nothing re-rendered.
}

function selectEvent(key) {
  // One game across every book: the picker narrows which games exist, and the
  // fixture keeps every book's price for a game that does.
  const events = brandGames(eventSummaries(sportRows()));
  selectedEvent = key;
  // The `shownChild` tag for this panel is set by showCurrentPanel, the only caller.
  // A second caller would have to set it too, or showCurrentPanel's dedupe would
  // believe the panel still holds the game the router last asked for and skip
  // rebuilding it. An earlier version of renderEvents was such a caller, and the tag
  // was set here; it no longer repoints the panel, so the assignment was dead.
  const event = events.find((e) => e.key === key);
  if (!event) {
    // Two different situations, and saying "Pick a game" for both was a lie in the
    // second: nothing is open, versus a game is open that this scrape does not hold.
    // The latter is reached by switching scrapes or sports with a game open, and the
    // breadcrumb still names that game — so the panel has to explain the mismatch
    // rather than pretend the reader never clicked anything.
    const asked = key !== null && key !== undefined && key !== '';
    el('event-title').textContent = asked ? 'Not in this scrape' : 'Pick a game';
    el('event-sub').textContent = asked
      ? 'This collection has no prices for that game. Pick another scrape, or open a game from this one.'
      : "Open Today's games and click a card.";
    el('event-count').textContent = asked ? key : '';
    table(el('event-detail'), [{ label: '', cell: () => cell('') }], [],
      { empty: asked
        ? 'No prices for this game in this scrape.'
        : "No game selected. Open Today's games and click a card." });
    return;
  }

  el('event-title').textContent = `${fullName(event.awayRaw)} at ${fullName(event.homeRaw)}`;
  el('event-sub').textContent = `${sportLabel(event.sport)} · ${leagueLabel(event.league)} · ${
    fmtClock(event.commence)} · ${event.rows.length} prices from ${
    event.bySource.size} feed${event.bySource.size === 1 ? '' : 's'}`;
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
      band: 'American odds (best highlighted)', label: book(s), num: true,
      hint: `${book(s)}'s American odds after commission`,
      cell: (r) => {
        const q = r.prices.get(s);
        if (!q) return html('<span class="dim">—</span>');
        const net = netOdds(q);
        // "Best" means best *takeable*, as the detector means it: a suspended
        // price is showing, not offering.  37 cross-book selections on the live
        // slate had their green marker on a row not accepting bets.  View-only
        // feeds (AN Open) never set or receive the mark.
        const live = [...r.prices.entries()]
          .filter(([src, p]) => !isViewOnly(src) && str(p[COL.status]) === 'active')
          .map(([, p]) => p);
        const best = live.length ? Math.max(...live.map(netOdds)) : null;
        const suspended = str(q[COL.status]) !== 'active';
        const isBest = !isViewOnly(s) && !suspended && live.length > 1 && net === best;
        const american = americanFromDecimal(net);
        const note = `${fmtAmerican(american)} · $100 returns ${fmtReturn(net)}${
          charges(s) ? ` · quoted ${fmtOdds(q[COL.decimal_odds])} before commission` : ''}${
          isViewOnly(s) ? ' · context only, not a book' : ''}${
          suspended ? ' · not taking bets right now' : ''}`;
        return html(`<span class="odds-cell${isBest ? ' best' : ''}${suspended ? ' dim' : ''}">${
          fmtAmerican(american)}</span>`, '', note);
      },
    })),
    {
      band: 'American odds (best highlighted)',
      label: 'Best vs worst', num: true, hint: 'How much more the best price pays than the worst',
      cell: (r) => {
        const vals = [...r.prices.entries()]
          .filter(([src, p]) => !isViewOnly(src) && str(p[COL.status]) === 'active')
          .map(([, p]) => netOdds(p));
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
  if (!(window > 0) || base === undefined) return 0;
  // Inclusive of the window's far edge, matching ``betRows``
  // (``Math.abs(when - anchor) > window``).  ``Math.floor(delta / window)``
  // put a start *exactly* one window after the anchor in bucket 1, so the bet
  // panel joined the price history and the Movement view split it.
  const delta = when - base;
  if (delta <= window) return 0;
  return Math.floor(delta / window);
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
  for (const m of shown) keepBetter(byBook, str(m.row[COL.source]), m.row);
  // Best *takeable*, matching the fixture table and the detector: a suspended
  // price is showing, not offering.  View-only feeds stay visible but never
  // win (or set) the best mark.
  const live = [...byBook.entries()]
    .filter(([src, r]) => !isViewOnly(src) && str(r[COL.status]) === 'active')
    .map(([, r]) => r);
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
      const isBest = !isViewOnly(source) && open && prices.length > 1 && net === best;
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
      // "I am taking this one, at this book, at this price." Everything the
      // ledger needs is already on screen, so the button carries it rather than
      // making the reader retype a price they are looking at.
      const logIt = singleLogButton({
        kind: 'single',
        sport: sport,
        league: str(r[COL.league]),
        event_key: spec.event,
        home_team: home,
        away_team: away,
        commence_time: str(r[COL.commence_time]),
        market: spec.market,
        period: spec.period,
        side: spec.side || '',
        line: spec.line,
        source_run_id: sample.run.id,
        legs: [{
          book: source,
          selection: spec.selection,
          line: spec.line,
          american_odds: americanOf(r),
          decimal_odds: r[COL.decimal_odds],
          stake: 0,
          link_url: '',
        }],
      }, 'log');
      return `<div class="qcard ${isBest ? 'top' : ''}${open ? '' : ' off'}">
        <div class="who"><b>${escapeHtml(book(source))}</b>${
          isBest ? '<span class="pill ok"><i></i>best</span>' : ''}${
          open ? '' : '<span class="pill warn">paused</span>'}${logIt}</div>
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
  //
  // A spread's two sides carry OPPOSITE signed lines (home -1.5 / away +1.5)
  // and no side field, so its siblings match on the detector's rule — the
  // CANONICAL home-oriented signed line (src/arb.py canonical_line): home rows
  // at the canon, away rows at its negation.  Matching on the raw line meant
  // no spread drill-down ever showed its other side; matching on Math.abs
  // (round three's first attempt) over-merged instead, collapsing home -1.5 /
  // away +1.5 with the MIRRORED market home +1.5 / away -1.5 — Pinnacle's
  // ±0.25 Asian handicaps and Kambi's alternate run lines both offer the two
  // as separate contracts, and the merge printed one rung's label beside the
  // other rung's price with a false "mispaired" verdict under both.  Legacy
  // vocabulary rides the same alias the rest of the page uses (run_line is a
  // supported stored spelling of spread).
  const specMarket = mkt(spec.market);
  const specIsSpread = specMarket === 'spread';
  const canon = !specIsSpread || spec.line === null || spec.line === undefined
    ? null
    : (spec.selection === 'home' ? spec.line : -spec.line);
  const belongs = (r) => {
    if (mkt(str(r[COL.market])) !== specMarket || str(r[COL.period]) !== spec.period) {
      return false;
    }
    if (!specIsSpread) {
      return (str(r[COL.side]) || '') === (spec.side || '')
        && (r[COL.line] === null || r[COL.line] === undefined ? '' : r[COL.line])
           === (spec.line === null || spec.line === undefined ? '' : spec.line);
    }
    const line = r[COL.line];
    if (line === null || line === undefined || canon === null) return false;
    const sel = str(r[COL.selection]);
    if (sel === 'home') return line === canon;
    if (sel === 'away') return line === -canon;
    return false;
  };
  const runRowsNow = rowsByRun.get(sample.run.id) || [];
  const siblings = new Map();
  for (const r of runRowsNow) {
    if (str(r[COL.event_key]) !== spec.event || !belongs(r)) continue;
    const sel = str(r[COL.selection]);
    // Each side carries its OWN line (a spread's mirror is the negation), so
    // the sibling remembers it: the description, notation, and the go: link
    // below must speak the side's number, not the clicked side's.
    if (!siblings.has(sel)) {
      siblings.set(sel, {
        selection: sel,
        line: r[COL.line] === null || r[COL.line] === undefined ? null : r[COL.line],
        prices: new Map(),
      });
    }
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
    && belongs(r)).map((r) => str(r[COL.source])))].sort();

  table(el('bet-sides'), [
    { band: 'the side', label: 'The bet', cell: (s) => {
        const one = Object.assign({}, bet, { selection: s.selection, line: s.line });
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
    // The target key must be one betKeyOf actually generates for the target
    // row — the sibling's own line, not the clicked side's, or a spread's
    // mirrored side linked to a bet that does not exist.
    go: (s) => href('bet', [spec.event, spec.market, spec.period, spec.side || '',
      s.line === null ? '' : s.line, s.selection, spec.is_alternate ? '1' : '0'].join('~')),
  });

  const totals = sideSources.map((source) => {
    const sides = [...siblings.values()].map((s) => s.prices.get(source)).filter(Boolean);
    // Same rule as the quality strip: a venue's cut can only be read off a
    // market it priced completely.  On two legs of a three-way this printed
    // "keeps -23.3%" — a 23% edge to the bettor — for a missing draw offer.
    if (!sumsToAMargin(sides)) return null;
    const sum = sides.reduce((a, r) => a + 1 / netOdds(r), 0);
    // A complete market is not necessarily a correctly paired one, and a cut is only
    // a cut if the venue could have quoted it. A sportsbook prices both sides itself
    // and will not price itself to lose, so a sum below 1.0 there is evidence about
    // the parser — validation says so in those words and fails the run for it. On an
    // exchange the two sides are separate order books and may cross by a little, so a
    // small negative is a thin market rather than a mispairing.
    //
    // Printed as fact, this read "Hard Rock Bet (VegasInsider) keeps -48.3%" beside
    // eleven venues keeping 1.5-4.4%, on a market validation had already flagged as
    // mispaired. A 48% edge to the bettor is not a price anyone can take.
    if (sum < 1 && !orderDriven(source)) {
      return `${book(source)} — both sides stored, but they sum to ${
        (sum * 100).toFixed(1)}%, so they are mispaired rather than a cut`;
    }
    return `${book(source)} keeps ${((sum - 1) * 100).toFixed(1)}%`;
  }).filter(Boolean);

  // How the price has moved, one row per book and one column per collection. This is
  // the same evidence as the movement table, narrowed to the one bet being looked at.
  const embedded = runs.slice().reverse().filter((r) => rowsByRun.has(r.id));
  const history = [...new Set(all.map((m) => str(m.row[COL.source])))].sort().map((source) => {
    // Same rule as ``byBook`` above, not last-write-wins. The bet key drops
    // ``is_alternate``, so a book's main and alternate row at one number both
    // land here, and ``new Map(...)`` kept whichever the source order put last
    // — so this column could disagree with the price column beside it, on the
    // same book, in the same collection.
    const byRun = new Map();
    for (const m of all) {
      if (str(m.row[COL.source]) !== source) continue;
      keepBetter(byRun, m.run.id, m.row);
    }
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
  const note = sourceInfo(key);
  const health = (run.sources || []).find((h) => h.key === key);

  // `sourceInfo` ends `|| {}`, so `note` is never falsy and this arm was
  // unreachable: with no venue to show, the panel fell through and wrote
  // `book(null)` — which is `null` — into its own headline, so a reader arriving
  // at #book on a run with no per-venue health rows got a blank title where this
  // sentence belongs. Ask what the arm was always asking: is there a venue.
  if (!key || (!note.label && !health)) {
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

  el('book-title').textContent = note.label || book(key);
  el('book-what').textContent = [
    (note && note.what) || '',
    note && note.commission ? `Commission: ${note.commission}.` : '',
    (note && note.settles) || '',
  ].filter(Boolean).join(' ');
  el('book-host').textContent = (note && note.host) || '';
  const refusedNow = health ? scopesFailedOf(health) : 0;
  const truncatedNow = health ? (health.scopes_truncated || []).length : 0;
  const routeBadges = [
    note && note.route_scope === 'global'
      ? '<span class="pill flat">GLOBAL</span>'
      : '',
    note && note.diagnostic_only
      ? '<span class="pill warn">diagnostic only</span>'
      : '',
    note && note.route_status
      ? `<span class="pill ${note.route_status === 'validated' ? 'ok' : 'warn'}">${escapeHtml(note.route_status)}</span>`
      : '',
  ].filter(Boolean).join(' ');
  el('book-state').innerHTML = (health
    ? (!health.ok
        ? `<span class="pill bad"><i></i>${escapeHtml(label(health.error_kind) || 'failed')}</span>`
        : refusedNow
          ? `<span class="pill warn"><i></i>refused ${refusedNow} of ${
              health.scopes_requested || refusedNow} scopes</span>`
          : truncatedNow
            ? `<span class="pill warn"><i></i>cut short on ${truncatedNow} scope${
                truncatedNow === 1 ? '' : 's'}</span>`
            : '<span class="pill ok"><i></i>responded normally</span>')
    : '<span class="pill flat">nothing recorded for this collection</span>')
    + (routeBadges ? ' ' + routeBadges : '');

  // `sportRows()`, never the row-narrowed accessor: this page's whole subject
  // is the venue in its heading, so the sportsbook picker naming a *different*
  // venue must not empty it — every other book's page would otherwise claim
  // "stored no prices" about a book that stored plenty, and the diagnostic
  // below, which only knows the offshore and sport filters, would blame the
  // wrong one.
  const mine = sportRows().filter((r) => str(r[COL.source]) === key);
  // `mine` is empty; this works out which of the row filters emptied
  // it, by asking the unfiltered rows the same question. Counting rows rather
  // than reading `health.quote_count` is the point: the health figure is
  // all-sports and all-books, so a book whose baseball rows exist but whose
  // hockey rows do not would otherwise be reported as hidden by the offshore
  // switch — and flipping that switch would then change nothing, which is a
  // worse answer than saying nothing.
  const unfiltered = rawRunRows().filter((r) => str(r[COL.source]) === key);
  const inSport = currentSport
    ? unfiltered.filter((r) => str(r[COL.sport]) === currentSport)
    : unfiltered;
  const hiddenByOffshore = inSport.length > 0 && isUsUnavailable(key) && !showOffshore;
  const hiddenBySport = !hiddenByOffshore && unfiltered.length > 0 && inSport.length === 0;
  const published = health ? health.quote_count : 0;
  const mixEmpty = hiddenByOffshore
    ? `${inSport.length.toLocaleString()} price${inSport.length === 1 ? '' : 's'} from this book are hidden here because you can't bet at it from the US. Turn on "Include books you can't bet from the US" to see them.`
    : hiddenBySport
      ? `This book priced ${unfiltered.length.toLocaleString()} market${unfiltered.length === 1 ? '' : 's'} in this collection, none of them ${escapeHtml(sportLabel(currentSport))}. Clear the sport filter to see them.`
      : !detailLoaded(currentRunId)
        ? `This book's ${published.toLocaleString()} prices are not in this page — only the newest few collections carry them. Rebuild with more --quote-runs to include it.`
        : 'This book stored no prices in this collection.';
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
    ['refused', refusedNow + ' of ' +
      (health.scopes_requested || (health.scopes_refused || []).length || 0),
      'scopes it was asked for'],
    ['cut short', truncatedNow,
      'scopes that answered but stopped early'],
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
  if (health && (health.scopes_truncated || []).length) {
    // A different claim from a refusal, and it has to read like one: these
    // scopes answered. What is missing is the tail of each of them, which is why
    // this is never counted into "refused N of M" — the whole scope was not
    // lost, and saying it was failed a run that had collected ten venues' rows.
    el('book-stats').innerHTML += `<div class="stat"><span>where it stopped early</span><small>${
      health.scopes_truncated.map(escapeHtml).join('<br>')}</small></div>`;
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
     // Three reasons this table can be empty and only one of them is "no prices".
     // `currentRows` drops books you can't bet from the US when the switch is off,
     // so a venue that published thousands of rows renders an empty table two
     // inches under its own "prices published 894" tile. Saying "stored no prices"
     // there contradicts the number beside it, and the reader cannot tell which
     // one is lying.
     { empty: mixEmpty });

  const skips = DATA.skipped.filter((s) => s.run_id === currentRunId && s.source === key)
    .sort((a, b) => b.count - a.count);
  el('book-skip-count').textContent = skips.length
    ? `${skips.reduce((a, s) => a + s.count, 0).toLocaleString()} offers across ${skips.length} kinds`
    : detailLoaded(currentRunId) ? 'nothing skipped'
    : `${((health && health.skipped_count) || 0).toLocaleString()} recorded — breakdown not in this page`;
  table(el('book-skips'), [
    { label: 'What it was', cell: (r) => cell(skipReasonLabel(r.reason)) },
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
  const fFeed = el('f-feed').value;
  // The venue list follows the feed pick, so the two compose rather than
  // silently contradicting: choosing "first-party only" and then a republished
  // venue would otherwise match nothing and say only "0 of 10,074".
  const sourcePool = fFeed
    ? rows.filter((r) => feedKind(str(r[COL.source])) === fFeed)
    : rows;
  fillSelect(el('f-source'), [...new Set(sourcePool.map((r) => str(r[COL.source])))].sort(), 'every sportsbook', book);
  fillSelect(el('f-market'), [...new Set(rows.map((r) => str(r[COL.market])))].sort(), 'every kind of bet', (v) => marketOf(v, currentSport).plain);
  fillSelect(el('f-period'), [...new Set(rows.map((r) => str(r[COL.period])))].sort(), 'any part of the game', (v) => periodOf(v).plain);
  fillSelect(el('f-league'), [...new Set(rows.map((r) => str(r[COL.league])))].sort(), 'every league', leagueLabel);

  const query = el('q').value.trim().toLowerCase();
  const fSource = el('f-source').value, fMarket = el('f-market').value;
  const fPeriod = el('f-period').value, fAlt = el('f-alt').value;
  const fLeague = el('f-league').value;

  let filtered = rows.filter((r) => {
    if (fFeed && feedKind(str(r[COL.source])) !== fFeed) return false;
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
    // Brand first, then feed, so one book's three feeds sit together instead
    // of scattering to wherever `an_`/`vi_` lands alphabetically.
    { band: WHAT, key: 'source', label: 'Sportsbook', cell: (r) => cell(book(str(r[COL.source]))),
      sort: (r) => { const k = str(r[COL.source]); return brandOf(k) + '\x1f' + k; } },
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

  // Before the cap, not through table()'s own opt: the sort ranks the whole
  // filtered set and the cap then shows its top, so "sort by price" surfaces
  // the best prices rather than reordering whichever 500 happened to show.
  filtered = applySort('odds-table', columns, filtered);

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
  wireSort(node, columns, 'odds-table', renderOdds);
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
    return `<rect class="run-bar" data-run-id="${r.id}" style="cursor:pointer"
      x="${(centre(i) - barW / 2).toFixed(1)}" y="${(floor - h).toFixed(1)}"
      width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="2"
      fill="${r.ok ? 'var(--accent)' : 'var(--down)'}" opacity="${r.id === currentRunId ? '1' : '0.55'}"
      ><title>${fmtClock(r.started_at)}: ${r.quote_count.toLocaleString()} prices — click to view</title></rect>`;
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
      <span>oldest on the left, newest on the right · click a bar to open that scrape</span>
    </div>`;
  el('runs-chart').querySelectorAll('.run-bar').forEach((node) => {
    node.addEventListener('click', () => selectRun(+node.getAttribute('data-run-id')));
  });
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

/** Identity of one price series across collections.
 *
 * The same list as ``Quote.dedup_key`` in Python, plus the fixture bucket, and
 * it has to stay that way: anything ``dedup_key`` separates is two rows the
 * store holds at once, so a key that merges them turns two simultaneous prices
 * into a movement between them. ``is_alternate`` was the one left out — a book
 * really does offer the same number twice, on its main market and on an
 * alternate-line market — and the table sorts by swing, so the widest of those
 * fabrications sorted to the top of the page.
 */
function movementKey(r, anchors) {
  const bet = betOf(r);
  return [str(r[COL.source]), str(r[COL.event_key]), bet.market, bet.period,
    bet.side || '', bet.selection, bet.line, bet.is_alternate ? 'alt' : 'main',
    fixtureBucket(r, anchors)].join('\x1f');
}

/** Keep the row a reader should be shown where a book has more than one.
 *
 * The bet key deliberately drops ``is_alternate`` — the detector ignores it too
 * — so a book's main and alternate row at one number land under the same key,
 * and something has to choose. Last-write-wins chose by source order, which
 * meant the price column and the per-collection history column beside it could
 * name different rows of the same book in the same collection.
 */
function keepBetter(map, key, row) {
  const held = map.get(key);
  if (!held || _betterPrice(row, held)) map.set(key, row);
  return map;
}

/** Every embedded run's prices, joined into one series per bet.
 *
 *  Split out of renderMovement so the nav count can be had without building the
 *  table, and memoized because it is the most expensive scan on the page: it walks
 *  every embedded run, not just the current one — 88ms of the 95ms the nav counts
 *  cost.
 *
 *  Keyed on the sport alone, deliberately. Price movement is a fact about the
 *  whole embedded history, so the answer does not depend on which run is being
 *  viewed — nothing below reads ``currentRunId``. Clearing this on a run change
 *  (which ``invalidatePanels`` used to do) re-ran the scan to arrive at the same
 *  number, and cost 88ms on every run switch including switches to a run holding
 *  no prices at all. */
let movementCache = null;

function movementAnalysis() {
  if (movementCache && movementCache.sport === currentSport) return movementCache;
  const ordered = runs.slice().reverse().filter((r) => rowsByRun.has(r.id));
  const anchors = fixtureAnchorsFor(
    ordered.flatMap((run) => rowsByRun.get(run.id) || [])
  );
  const series = new Map();
  for (const run of ordered) {
    for (const r of rowsByRun.get(run.id)) {
      if (currentSport && str(r[COL.sport]) !== currentSport) continue;
      // Keyed on the scheduled start as well, for the reason ``betRows`` is: the
      // doubleheader ordinal is a within-run rank, so once game one has started
      // and been dropped, game two inherits the bare key.  Without this, 97
      // series on the live slate spliced game one's price onto game two's and
      // drew the join as a price movement — one of them a "+29.63% change" with
      // a sparkline, between two different games.
      const key = movementKey(r, anchors);
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

  movementCache = { sport: currentSport, ordered, series, moved };
  return movementCache;
}

/** How many bets changed price — the Movement nav count, without the table. */
function movedCount() {
  return movementAnalysis().moved.length;
}

function renderMovement() {
  svgRunsChart();

  const { ordered, series, moved } = movementAnalysis();

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
  // The sportsbook picker applies here — a finding row carries a `source` — but
  // the sport filter cannot: a finding carries no sport. A run-level or
  // cross-source finding carries no source either, and those are *kept* under a
  // brand pick, because a check about the whole run belongs to no book and
  // dropping it would let a filtered view read as a cleaner scrape.
  const findings = DATA.findings.filter((f) => f.run_id === currentRunId)
    .filter((f) => !f.source || keepBrand(f.source));
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
    // dropChunkControl before blanking, because the control is a *sibling*: emptying
    // the table cannot take it with it. Without this, switching from the newest
    // scrape to one whose prices are not embedded left "Showing 120 of 500 rows"
    // under the not-embedded notice, and clicking it appended 120 of the *previous*
    // scrape's findings into the blanked table — headerless rows that still carried
    // `data-go` and still navigated to the other scrape's fixtures.
    ['findings', 'overround', 'rejections', 'skips'].forEach(blankRegion);
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
  // Under a brand pick the truncation test is forced off: the run summary has
  // no per-book breakdown, so a filtered count below the whole-run total says
  // nothing about embedding, and claiming truncation would be a guess.
  const recordedFindings = run ? (run.error_count || 0) + (run.warning_count || 0) : 0;
  const findingsShort = !currentBrand && findings.length < recordedFindings;
  const items = [
    ['problems found', findingsShort ? recordedFindings : findings.length,
      findingsShort ? 'recorded — not embedded in this page'
        : findings.length
          ? (currentBrand ? `about ${brandLabel(currentBrand)} or the whole run` : 'listed below')
          : 'nothing flagged',
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
    { key: 'severity', label: 'How serious', sort: (f) => f.severity,
      cell: (f) => html(`<span class="pill ${f.severity === 'error' ? 'bad' : 'warn'}"><i></i>${
        f.severity === 'error' ? 'problem' : 'worth a look'}</span>`) },
    { key: 'code', label: 'Check', sort: (f) => f.code, cell: (f) => cell(label(f.code)) },
    // Brand first, then feed, so one book's mirrors sort beside it.
    { key: 'source', label: 'Sportsbook',
      sort: (f) => (f.source ? brandOf(f.source) + '\x1f' + f.source : ''),
      cell: (f) => cell(f.source ? book(f.source) : '—', 'dim') },
    { key: 'fixture', label: 'Fixture', sort: (f) => f.event_key || '',
      cell: (f) => cell(f.event_key || '—', 'mono dim') },
    { key: 'message', label: 'What it says', sort: (f) => f.message,
      cell: (f) => cell(f.message, 'wrap') },
  ], findings, { sort: 'findings', rerender: renderQuality, empty: findingsShort
                   ? `This collection's ${recordedFindings.toLocaleString()} notes are not in this page — the 500 embedded here were used up by other collections. Rebuild with fewer --quote-runs to include them.`
                   : currentBrand
                     ? `Nothing at ${brandLabel(currentBrand)} was flagged in this collection. Checks that aren't about one book are still listed.`
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

  const rejections = DATA.rejections.filter((r) => r.run_id === currentRunId)
    .filter((r) => !r.source || keepBrand(r.source));
  // The 500-row cap is shared across every embedded run and the health rows
  // carry each source's true rejection count, so the page can always tell a
  // clean run from one whose rejections were cut — the findings table above
  // makes exactly this check, and this table said "nothing had to be thrown
  // away" about 60 rejections the cap had squeezed out.  (``run`` is the
  // function-level binding from the top of renderQuality.)  The recorded total
  // follows the sportsbook picker — health rows are per-source, so unlike the
  // findings tile this figure stays exact rather than guessed under a brand.
  const recordedRejections = run
    ? run.sources.filter((h) => keepBrand(h.key))
        .reduce((total, h) => total + (h.rejection_count || 0), 0) : 0;
  const rejectionsShort = rejections.length < recordedRejections;
  el('rejections-note').textContent = (rejectionsShort && rejections.length
    ? `${rejections.length.toLocaleString()} of ${recordedRejections.toLocaleString()} rejected rows shown — the rest did not fit this page's 500-row cap. Rebuild with fewer --quote-runs to include them. `
    : '') + 'Click any column heading to sort.';
  table(el('rejections'), [
    { key: 'source', label: 'Venue',
      sort: (r) => (r.source ? brandOf(r.source) + '\x1f' + r.source : ''),
      cell: (r) => cell(book(r.source)) },
    { key: 'reason', label: 'Why', sort: (r) => r.reason, cell: (r) => cell(label(r.reason)) },
    { key: 'detail', label: 'Detail', sort: (r) => r.detail || '', cell: (r) => cell(r.detail, 'wrap') },
  ], rejections, { sort: 'rejections', rerender: renderQuality, empty: rejectionsShort
      ? `This collection's ${recordedRejections.toLocaleString()} rejected rows are not in this page — the 500 embedded here were used up by other collections. Rebuild with fewer --quote-runs to include them.`
      : currentBrand
        ? `Nothing from ${brandLabel(currentBrand)} had to be thrown away in this collection.`
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

/* ── render scheduling ──────────────────────────────────────────────────────
   Every panel used to be built up front, on load and again on every run or
   sport change.  Measured on a 16-scrape page that is 1,702 games across 22
   books: 577,098 DOM nodes for thirteen panels when exactly one is on screen,
   461ms of computation plus roughly as much again building markup, and 1,143ms
   of layout the moment the odds board was shown.  Chrome does not survive that
   for long, and an interaction that blocks the main thread for a second is
   indistinguishable from a crash.

   A panel is now built when it is first shown, and marked stale when the run or
   sport moves under it.  The guarantee the old comment cared about — that a link
   straight into a panel opens on something rather than on a blank page — is kept
   by rendering on arrival, which ``applyRoute`` does before the panel is
   revealed. ``tests/dashboard_smoke.mjs`` walks every panel so each render path
   is still exercised on every build.

   The nav counts are the exception: they have to be right *before* you click,
   so they are computed without building any markup, and after the first paint,
   because two of them have to scan the quote rows to know the answer. */
const PANEL_RENDERERS = {
  overview: renderOverview,
  run:      renderOverview,      // the dense strip and the flow live on This scrape
  screen:   renderOddsScreen,
  arb:      renderArb,
  promos:   renderPromos,
  bets:     renderBets,
  sports:   renderSports,
  sources:  renderSources,
  events:   renderEvents,
  odds:     renderOdds,
  movement: renderMovement,      // also draws the every-scrape chart
  quality:  renderQuality,
  raw:      renderRaw,
};

/** Panels whose markup no longer matches the run or sport being viewed. */
const stalePanels = new Set(Object.keys(PANEL_RENDERERS));

/** The regions worth throwing away when their panel leaves the screen.
 *
 *  Building one panel at a time bounds what *arriving* costs, and on its own it
 *  does not bound the document: every panel visited stayed in the DOM, so browsing
 *  five of them and scrolling their lists reached 407,670 elements — the number the
 *  eager render used to hit on load. These are the regions big enough to be worth
 *  rebuilding rather than keeping; the small ones (stat strips, summaries, chrome)
 *  are cheaper to leave alone. */
const PANEL_HEAVY = {
  screen:   ['odds-screen'],
  events:   ['coverage', 'events-games'],
  // overview and run are both rendered by renderOverview, so they own the same
  // regions. Splitting them meant arriving at one built the other's region and threw
  // it away in the same task, then left it resident for good on the way out. Sharing
  // them only works because `applyRoute` evicts before it builds — see the note
  // there; evicting afterwards blanks whichever of the two was just revealed.
  overview: ['browse-games', 'matrix'],
  run:      ['browse-games', 'matrix'],
  odds:     ['odds-table'],
  movement: ['move-table', 'runs-chart'],
  quality:  ['findings', 'overround', 'rejections'],
  raw:      ['raws'],
  sources:  ['source-cards', 'skips'],
  // sports-gaps is bigger than the two grids together and was the one region on
  // Coverage that was never reclaimed.
  sports:   ['sports-grid', 'leagues-grid', 'sports-gaps'],
  promos:   ['promo-list'],
  arb:      ['arb-list'],
  bets:     ['bets-list'],
  // No entries for fixture/bet/book. A drill-down is never unloaded: each one owns a
  // single set of regions that the next game or bet overwrites, so they cannot
  // accumulate, and keeping them means going Back to the list you came from is free.
};

/** Mark a panel for rebuilding, and every other name that shares its renderer. */
function markStale(name) {
  const render = PANEL_RENDERERS[name];
  if (!render) return;
  for (const [key, fn] of Object.entries(PANEL_RENDERERS)) {
    if (fn === render) stalePanels.add(key);
  }
}

/** Empty a panel's heavy regions and arrange for it to be rebuilt on return. */
function evictPanel(name) {
  const ids = PANEL_HEAVY[name];
  if (!ids) return;
  for (const id of ids) {
    const node = el(id);
    if (!node) continue;
    dropChunkControl(node);
    node.innerHTML = '';
  }
  markStale(name);
}

function invalidatePanels() {
  for (const name of Object.keys(PANEL_RENDERERS)) stalePanels.add(name);
  shownChild = null;
  // movementCache is deliberately NOT cleared here: it is keyed on the sport and
  // answers a question about every embedded run, so a run change cannot change it.
}

/** A panel's own filter changed: rebuild it if it is on screen, else mark it stale.
 *
 *  The filter handlers used to call their renderers directly, which meant a control
 *  shared between two panels rebuilt both. One league pick on the Games panel wrote
 *  792,544 bytes of markup, 647,882 of it into the off-screen odds board — which was
 *  stale anyway and would be rebuilt on arrival, so the work was pure waste and the
 *  markup pure accumulation. The debounced boxes could do it up to 140ms after the
 *  reader had already left. */
function refreshPanel(name) {
  markStale(name);
  if (here.panel === name) { ensurePanel(name); return; }
  // Not rendering means the panel's own renderer is not going to write its nav count,
  // and that count describes state the filter just changed. Centralised here rather
  // than at each call site, because every caller has the same obligation and one of
  // them had already forgotten it: picking a league on Odds narrowed the board to 31
  // games while "Today's games" in the rail still read 1,702. It also covers the
  // debounce race — typing in the Games search and leaving inside the 140ms window,
  // where the pending refresh lands after the reader is somewhere else.
  scheduleNavCounts();
}

/** Build a panel unless it is already current. Returns whether it ran. */
function ensurePanel(name) {
  const render = PANEL_RENDERERS[name];
  if (!render || !stalePanels.has(name)) return false;
  // Cleared first: renderOverview backs two panel names, and a renderer that
  // throws must not be retried on every route change.
  for (const [key, fn] of Object.entries(PANEL_RENDERERS)) {
    if (fn === render) stalePanels.delete(key);
  }
  render();
  return true;
}

/** The drill-down panel already built, as panel + subject, so one route pass
 *  that changes the run does not build the same detail panel twice. */
let shownChild = null;

/** Build whatever is on screen right now, including the drill-down panels.
 *
 *  The child panels take their subject from the route rather than from a stale
 *  render, so changing the run while sitting on one repaints it for the new run. */
function showCurrentPanel() {
  const { panel, arg } = here;
  if (panel === 'book' || panel === 'fixture' || panel === 'bet') {
    // A bare #book / #fixture / #bet still opens on something, which is what the
    // eager render used to guarantee. With no data to default to these fall back
    // to their own empty state, which is the honest answer.
    const subject = arg !== null && arg !== undefined ? arg
      : (panel === 'book' ? defaultBook() : panel === 'fixture' ? defaultFixture() : defaultBet());
    const tag = panel + '\x1f' + (subject === null || subject === undefined ? '' : subject);
    if (shownChild === tag) return;
    shownChild = tag;
    if (panel === 'book') renderBook(subject);
    else if (panel === 'fixture') selectEvent(subject);
    else renderBet(subject);
    return;
  }
  ensurePanel(panel);
}

function renderRunScoped() {
  renderChrome();
  // Reconcile the filter state before anything reads it. A league or book chosen
  // under one sport may not exist under the next, and the code that clears an
  // impossible choice used to run on every render because every panel rendered.
  // Now only one panel builds, so a stale choice survived and the nav counts read
  // it: picking Tennis with the book filter left on a baseball-only book showed
  // "Games 0" beside a scrape holding 381 tennis games. It also fixes an older
  // version of the same fault, where the board itself rendered empty because
  // renderOddsScreen selected its rows before clearing the impossible league.
  buildEventsFilters();
  invalidatePanels();
  showCurrentPanel();
  scheduleNavCounts();
}

/* The counts beside each nav entry. Everything here reads a summary or scans
   rows; nothing builds markup, which is what made the eager render expensive. */
let navCountTimer = null;

/** Fill the nav counts once the current panel has painted.
 *
 *  Deferred rather than inline because the board and games counts have to walk
 *  the quote rows, and a reader waiting to click should not pay for a number. */
function scheduleNavCounts() {
  if (typeof setTimeout !== 'function') { renderNavCounts(); return; }
  if (navCountTimer !== null) clearTimeout(navCountTimer);
  navCountTimer = setTimeout(() => { navCountTimer = null; renderNavCounts(); }, 0);
}

function renderNavCounts() {
  const run = runById.get(currentRunId);
  const setCount = (id, value) => { const node = el(id); if (node) node.textContent = String(value); };

  // Before the no-run guard, and outside every filter: the ledger is a record of
  // what was placed, not a view of a scrape. Blanking it when the run picker has
  // nothing selected would hide a real open position behind an unrelated state.
  const slipCount = betSlips().length;
  setCount('nav-bets', slipCount ? String(slipCount) : '');

  if (!run) {
    for (const id of ['nav-screen', 'nav-arb', 'nav-promos', 'nav-events', 'nav-odds',
                      'nav-sports', 'nav-sources', 'nav-move', 'nav-quality', 'nav-raw']) {
      setCount(id, '');
    }
    return;
  }

  // From the run summary and the embedded bags — no row scan needed.
  const entries = run.sports || [];
  setCount('nav-sports', `${entries.filter((e) => e.comparable).length}/${entries.length}`);
  setCount('nav-sources', `${run.sources.filter((h) => h.ok).length}/${run.sources.length}`);
  // Mirrors renderArb's three cases: prices not embedded, embedded but not
  // computed, and computed. They are three different sentences in the panel and
  // must not collapse to one number here — and the computed one counts only
  // takeable positions, exactly as renderArb's badge does. This refresher runs
  // on a timer after renderArb's synchronous write, so counting the flagged
  // positions here silently overwrote the honest badge with the bigger number.
  const bag = arbBundle();
  setCount('nav-arb', !detailLoaded(currentRunId) ? ''
    : (!bag ? '—'
      : String(arbPositions(bag)
        .filter((o) => !currentSport || o.sport === currentSport)
        .filter((o) => !currentBrand || (o.legs || []).some((leg) => keepBrand(leg.source)))
        .filter(arbTakeable).length)));
  // Unconditional, as renderPromos has it: with no promo scrape the honest count
  // is 0, and a blank would read as "not counted yet". `promoOffers()` is the
  // same list the panel counts, so the badge and the headline cannot drift.
  setCount('nav-promos', String(promoOffers().length));
  // The same brand clause as renderQuality's, sourceless findings kept, so the
  // badge counts exactly what the panel lists.
  const findings = DATA.findings.filter((f) => f.run_id === currentRunId)
    .filter((f) => !f.source || keepBrand(f.source));
  setCount('nav-quality', !runRows().length && !detailLoaded(currentRunId)
    ? '—' : (findings.length ? String(findings.length) : 'clear'));
  setCount('nav-raw', detailLoaded(currentRunId)
    ? DATA.raws.filter((r) => r.run_id === currentRunId).length
    : ((run && run.raw_count) || 0));

  // Two scans, because the two counts answer different questions under the
  // sportsbook picker: All prices counts the picked book's own rows, while the
  // board counts games — narrowed through `brandGames` exactly as the board is,
  // so the badge and the panel cannot disagree.
  setCount('nav-odds', currentRows().length.toLocaleString());
  setCount('nav-screen', brandGames(eventSummaries(
    sportRows().filter((r) => !currentLeague || str(r[COL.league]) === currentLeague))).length);
  setCount('nav-events', filteredGameEvents().length);

  // Movement is counted separately, and last. It has to join every embedded run's
  // rows — 88-123ms of the ~100ms this whole pass used to cost — so it waits for
  // idle rather than riding along with the cheap counts.
  //
  // An earlier version guarded this with `if (stalePanels.has('movement'))`,
  // which selected the expensive case rather than skipping it: stale is exactly
  // when the cache is cold and the full scan has to run.
  scheduleMovementCount();
}

/** Fill the Movement count when the main thread is free.
 *
 *  Deferred harder than the other counts because it is the one number that costs a
 *  cross-run scan. If the panel is opened first it fills the count itself and the
 *  memo makes this a no-op. */
let movementCountTimer = null;

function scheduleMovementCount() {
  const fill = () => {
    movementCountTimer = null;
    const node = el('nav-move');
    if (node) node.textContent = movedCount().toLocaleString();
  };
  // A timer, deliberately, not requestIdleCallback. Idle callbacks do not run in a
  // hidden tab — measured: the count never arrived, even with a 2000ms timeout set —
  // so the number would be missing or stale for anyone who changed sport, switched
  // away and came back. A timer fires either way; all this has to buy is "not in the
  // same task as the click".
  if (typeof setTimeout !== 'function') { fill(); return; }
  if (movementCountTimer !== null) clearTimeout(movementCountTimer);
  movementCountTimer = setTimeout(fill, 200);
}

/** The book, game and bet the detail panels sit on until something is clicked. */
function defaultBook() {
  const run = runById.get(currentRunId) || { sources: [] };
  const first = (run.sources || [])[0];
  return first ? first.key : null;
}
function defaultFixture() {
  if (selectedEvent) return selectedEvent;
  const first = filteredGameEvents()[0] || brandGames(eventSummaries(sportRows()))[0];
  return first ? first.key : null;
}
function defaultBet() {
  const row = currentRows()[0];
  return row ? betKeyOf(row) : null;
}

buildRunPicker();
buildSportPicker();
renderChrome();
renderReference();

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('hashchange', applyRoute);
}
// Builds exactly the panel the URL asks for. Everything else is built on arrival.
applyRoute();
scheduleNavCounts();

/** Run `fn` once the typing stops.
 *
 *  The search boxes rebuilt their whole panel on every keystroke: 50ms a
 *  character on All prices and 95ms on Games, where each character also rebuilt
 *  the 1,704-row coverage table. Typing a team name paid that eight times over
 *  and felt like a hang. A trailing edge is right for a filter — the answer for
 *  the prefix you are still typing is not one anybody reads. */
function afterTyping(fn, ms) {
  let timer = null;
  return () => {
    if (typeof setTimeout !== 'function') { fn(); return; }
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(); }, ms === undefined ? 140 : ms);
  };
}

// Only the typed boxes are debounced. A select fires once on a deliberate pick, so
// delaying it just makes the page feel late — these repaint immediately.
const refreshOdds = () => refreshPanel('odds');
const refreshEvents = () => refreshPanel('events');
const refilterOdds = afterTyping(refreshOdds);
el('q').addEventListener('input', refilterOdds);
['f-feed', 'f-source', 'f-league', 'f-market', 'f-period', 'f-alt'].forEach((id) => {
  el(id).addEventListener('input', refreshOdds);
});
const refilterEvents = afterTyping(refreshEvents);
el('events-q').addEventListener('input', refilterEvents);
el('events-feed').addEventListener('input', refreshEvents);
el('events-book').addEventListener('input', refreshEvents);
el('cov-mode').addEventListener('change', refreshEvents);
el('move-source').addEventListener('change', () => refreshPanel('movement'));
// The sport filter is bound once, on the element rather than on its options, so
// rebuilding the list for a different run cannot silently drop the handler.
el('sport-pick').addEventListener('change', () => {
  currentSport = el('sport-pick').value;
  selectedEvent = null;
  buildSportPicker();
  renderRunScoped();
});

/* The offshore switch. Goes through the same path as changing the sport, and for
   the same reason: it changes which rows exist, so the sport list, the filter
   reconciliation, every panel and every nav count have to be redone rather than
   just the panel on screen. The selected game is dropped because a fixture only
   an offshore book priced stops existing when the switch goes off. */
const offshoreToggle = el('offshore-toggle');
if (offshoreToggle) {
  offshoreToggle.checked = showOffshore;
  offshoreToggle.addEventListener('change', () => {
    showOffshore = offshoreToggle.checked;
    storeValue(OFFSHORE_KEY, showOffshore ? '1' : '0');
    selectedEvent = null;
    buildSportPicker();
    renderRunScoped();
  });
}

/* The sportsbook picker. Global for the same reason as the two above: it
   changes which rows and games exist on nearly every panel, so the whole
   reconcile-then-rebuild path runs rather than repainting the panel on screen.
   The selected game is dropped because a game only this book priced stops
   existing when the pick changes. The sport list deliberately does not narrow
   to the picked book's sports — it reads `runRows()`, like this picker's own
   pool, so the two global filters cannot strangle each other.
   Guarded: the empty scrape shell does not carry the control. */
const bookPick = el('book-pick');
if (bookPick) {
  bookPick.addEventListener('change', () => {
    currentBrand = bookPick.value;
    storeValue(BOOK_KEY, currentBrand || '');
    selectedEvent = null;
    renderRunScoped();
  });
}

/* The after-tax pickers. Unlike the three global controls above, these change
   no row's membership in anything — they add a second figure beside numbers
   that are already on screen. So the reconcile-then-rebuild path is not run:
   `buildEventsFilters` has nothing to reconcile and no nav count reads a rate.
   Every panel is marked stale and the one on screen rebuilds, which is what
   makes the arbitrage, promo and ledger figures move together.
   Guarded: the empty scrape shell does not carry the controls. */
function fillTaxPicker(node, presets, selected) {
  if (!node) return selected;
  const rates = (presets || []).map((entry) => Number(entry.rate));
  // A rate remembered from a payload that no longer offers it would leave the
  // control showing one thing while the arithmetic used another. Decided from
  // the rate list rather than from what the control reports back: a <select>
  // only answers with its `selected` option once the markup is really in the
  // document, so trusting `node.value` here reads as "nothing was remembered"
  // wherever it is not.
  const chosen = rates.includes(selected) ? selected : (rates.length ? rates[0] : 0);
  node.innerHTML = presets.map((entry) =>
    `<option value="${Number(entry.rate)}"${
      Number(entry.rate) === chosen ? ' selected' : ''
    }>${escapeHtml(String(entry.label))}</option>`).join('');
  node.value = String(chosen);
  return chosen;
}

const taxFedPick = el('tax-fed');
const taxStatePick = el('tax-state');
const taxCapToggle = el('tax-cap');

taxFed = fillTaxPicker(taxFedPick, TAX.federal, taxFed);
taxState = fillTaxPicker(taxStatePick, TAX.state, taxState);

function applyTaxChange() {
  storeValue(TAX_FED_KEY, String(taxFed));
  storeValue(TAX_STATE_KEY, String(taxState));
  storeValue(TAX_CAP_KEY, taxCap ? '1' : '0');
  paintTaxMeta();
  invalidatePanels();
  showCurrentPanel();
}

if (taxFedPick) {
  taxFedPick.addEventListener('change', () => {
    taxFed = Number(taxFedPick.value) || 0;
    applyTaxChange();
  });
}
if (taxStatePick) {
  taxStatePick.addEventListener('change', () => {
    taxState = Number(taxStatePick.value) || 0;
    applyTaxChange();
  });
}
if (taxCapToggle) {
  taxCapToggle.checked = taxCap;
  taxCapToggle.addEventListener('change', () => {
    taxCap = taxCapToggle.checked;
    applyTaxChange();
  });
}

/** The rail's line under the pickers: what the rates currently do, in words. */
function paintTaxMeta() {
  const node = el('tax-meta');
  if (!node) return;
  if (!taxOn()) {
    node.textContent = 'every figure on the page is pre-tax';
    return;
  }
  const parts = [];
  if (taxFed > 0) {
    const deducted = taxCap
      ? `${(DEDUCTIBLE_SHARE * 100).toFixed(0)}% of losses`
      : 'losses';
    parts.push(`${(taxFed * 100).toFixed(0)}% federal on winnings less ${deducted}`);
  }
  if (taxState > 0) {
    parts.push(`${(taxState * 100).toFixed(2)}% state on gross winnings, no offset`);
  }
  node.textContent = `${parts.join(' · ')} — informational, not tax advice`;
}

/** The rail's line under the switch: what it is currently hiding or admitting. */
function paintOffshoreMeta() {
  const node = el('offshore-meta');
  if (!node) return;
  const hidden = new Set();
  for (const row of rawRunRows()) {
    const key = str(row[COL.source]);
    if (isUsUnavailable(key)) hidden.add(key);
  }
  if (!hidden.size) {
    node.textContent = 'this scrape has no offshore prices';
    return;
  }
  const names = [...hidden].map(book).sort();
  const list = names.slice(0, 3).join(', ') + (names.length > 3 ? `, +${names.length - 3} more` : '');
  node.textContent = showOffshore
    ? `showing ${names.length} unbettable book(s): ${list}`
    : `hiding ${names.length} book(s) you can't bet: ${list}`;
}

/* ── scrape from the UI (only when served on localhost) ──────────────────── */

function scrapeScopePayload() {
  const value = el('scrape-scope').value || 'league:MLB';
  const states = selectedScrapeStates();
  if (value === 'all') return { tier: 'core', states };
  if (value.startsWith('sport:')) return { tier: 'core', sport: value.slice(6), states };
  if (value.startsWith('league:')) return { tier: 'core', league: value.slice(7), states };
  return { tier: 'core', league: 'MLB', states };
}

function selectedScrapeStates() {
  return Array.from(document.querySelectorAll('#scrape-states input:checked'))
    .map((node) => node.value);
}

/* Which states to scrape is the operator's choice, and these boxes are where it
   is made.  Detection only pre-checks one — it used to check it AND disable it,
   which made a third-party IP lookup the authority on where you are.  It was
   wrong about this machine (said CA for an Illinois egress), and a wrong reading
   could not be corrected by hand.  Now: a remembered choice wins over detection,
   detection fills in when there is no remembered choice, and IL is the last
   resort so the button is never dead. */
const SCRAPE_STATES_KEY = 'lineshop.scrape.states';

function rememberScrapeStates() {
  storeValue(SCRAPE_STATES_KEY, JSON.stringify(selectedScrapeStates()));
}

function rememberedScrapeStates() {
  const raw = storedValue(SCRAPE_STATES_KEY, '');
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    /* An empty remembered array is NOT a choice to scrape nothing — it is what a
       stray click leaves behind, and honouring it would disable the button with
       no way back except clearing storage. */
    return Array.isArray(parsed) && parsed.length ? parsed : null;
  } catch (err) {
    return null;
  }
}

function initScrapeStates(detected) {
  const boxes = Array.from(document.querySelectorAll('#scrape-states input'));
  if (!boxes.length) return;
  boxes.forEach((node) => node.addEventListener('change', rememberScrapeStates));
  const remembered = rememberedScrapeStates();
  const wanted = remembered
    || (detected && boxes.some((node) => node.value === detected) ? [detected] : ['IL']);
  boxes.forEach((node) => {
    node.checked = wanted.indexOf(node.value) >= 0;
    node.closest('label').title = node.value === detected
      ? 'Detected from your connection — uncheck it if that is wrong'
      : `Also scrape ${node.value}`;
  });
}
initScrapeStates(DATA.meta.detected_state);

/* ``state_note`` is set only when live detection could not answer and a stored
   reading stood in for it.  The page rebuilt after the scrape cannot work that
   out for itself — its ``detected_state`` is that stored reading, and looks
   ordinary — so the fact is carried over the reload rather than inferred. */
const SCRAPE_NOTE_KEY = 'lineshop.scrape.stateNote';

function carryStateNote(note) {
  if (!note) return;
  try {
    window.sessionStorage.setItem(SCRAPE_NOTE_KEY, String(note));
  } catch (err) { /* nothing to carry it in; the CLI still prints it */ }
}

function takeCarriedStateNote() {
  try {
    const note = window.sessionStorage.getItem(SCRAPE_NOTE_KEY);
    if (note) window.sessionStorage.removeItem(SCRAPE_NOTE_KEY);
    return note || '';
  } catch (err) {
    return '';
  }
}

/* Both scrape buttons drive one state machine: poll a status endpoint, paint a
   bar, POST a collect endpoint, then either reload or repaint in place. It was
   written out twice — 292 lines for one machine — and the copies had already
   drifted apart in ways nothing on the page explains: only the promo half
   guarded its own nodes before touching them, and only the odds half told the
   reader when the *other* kind of scrape was holding the lock. Neither
   difference was a decision.

   What genuinely differs per kind lives in SCRAPE_KINDS below, and nothing
   else does. The two hooks exist because the tails really are different work:
   an odds scrape always reloads onto its new snapshot, while a promo scrape
   may repaint the panel it is already on. */
const SCRAPE_KINDS = {
  odds: {
    kind: 'odds',
    ids: {
      btn: 'scrape-btn', status: 'scrape-status', box: 'scrape-progress',
      fill: 'scrape-bar-fill', msg: 'scrape-msg', meta: 'scrape-meta',
    },
    // The requests live here, with literal targets, because
    // ``test_page_reaches_no_network`` audits every call on the page for a
    // same-origin ``/api/`` string literal — a target read out of a table is
    // exactly what it refuses, and rightly: a variable target cannot be
    // audited at all. Two short thunks per kind is what the fold costs to keep
    // that guard intact, against the 292 lines it replaces.
    askStatus: () => fetch('/api/status', { cache: 'no-store' }),
    postCollect: (payload) => fetch('/api/collect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
    countKey: 'quote_count',
    countNoun: 'prices',
    busyText: 'Scraping…',
    startText: 'Starting scrape…',
    runningText: 'Scraping in progress…',
    readyText: 'Ready — scrapes the venues, then reloads this page on the new snapshot.',
    fileText: 'View only. Run with --serve 8765 --open to scrape from here.',
    failPrefix: 'Scrape failed: ',
    // Said only by the odds half, because the odds scrape is the one that
    // waits: a promo scrape holds the lock and this is where the reader is.
    // Named, not "anything that is not me" — the sentence claims *which* scrape
    // is running, so it may only be said about the kind it names. A third kind
    // added later gets its own entry here or stays unannounced; it does not
    // inherit this one's wording by default.
    heldBy: 'promos',
    heldText: 'Promo scrape running — odds scrape waits until it finishes.',
    payload: () => scrapeScopePayload(),
    /* A scrape that ran on a stood-in state says so on the page built FROM it.
       The success path reloads immediately, so a note written into this line
       before the reload would flash and vanish — it is carried across instead. */
    onReady: (status) => {
      const carried = takeCarriedStateNote();
      if (carried) status.textContent = carried + ' Ready to scrape again.';
    },
    onDone: (body, count, status) => {
      carryStateNote(body.collect && body.collect.state_note);
      status.textContent = `Got ${count.toLocaleString()} prices — reloading…`;
      return { message: `Got ${count.toLocaleString()} prices — reloading…`, reload: true };
    },
  },
  promos: {
    kind: 'promos',
    ids: {
      btn: 'promo-scrape-btn', status: 'promo-scrape-status', box: 'promo-scrape-progress',
      fill: 'promo-scrape-bar-fill', msg: 'promo-scrape-msg', meta: 'promo-scrape-meta',
    },
    askStatus: () => fetch('/api/promos/status', { cache: 'no-store' }),
    postCollect: (payload) => fetch('/api/promos/collect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
    countKey: 'offer_count',
    countNoun: 'offers',
    busyText: 'Scraping promos…',
    startText: 'Starting promo scrape…',
    runningText: 'Promo scrape in progress…',
    readyText: 'Ready — scrapes public bonuses/promos, then reloads this page.',
    fileText: 'View only. Run with --serve 8765 --open to scrape bonuses here.',
    failPrefix: 'Promo scrape failed: ',
    heldBy: '',
    heldText: '',
    payload: () => ({ states: selectedScrapeStates() }),
    onReady: () => {},
    onDone: (body, count, status) => {
      if (body.promos) PROMOS = body.promos;
      status.textContent = `Got ${count.toLocaleString()} promo offer(s)`
        + (body.reload ? ' — reloading…' : '.');
      if (body.reload) {
        return { message: `Got ${count.toLocaleString()} promo offer(s)`, reload: true, hash: '#promos' };
      }
      return {
        message: `Got ${count.toLocaleString()} promo offer(s)`,
        reload: false,
        after: () => { renderPromos(); go('#promos'); },
      };
    },
  },
};

function paintScrape(spec, progress, busy) {
  const box = el(spec.ids.box);
  const fill = el(spec.ids.fill);
  const msg = el(spec.ids.msg);
  const meta = el(spec.ids.meta);
  if (!box || !fill || !msg || !meta) return;
  if (!busy && !(progress && progress.phase === 'done')) {
    box.classList.remove('on', 'is-indeterminate');
    return;
  }
  box.classList.add('on');
  const done = Number(progress && progress.done) || 0;
  const total = Number(progress && progress.total) || 0;
  const phase = (progress && progress.phase) || '';
  const known = total > 0 && (phase === 'fetching' || phase === 'fetched' || phase === 'starting');
  box.classList.toggle('is-indeterminate', busy && !known);
  if (known) {
    fill.style.width = Math.max(4, Math.min(100, Math.round((done / total) * 100))) + '%';
  } else if (phase === 'rebuilding' || phase === 'done') {
    fill.style.width = '100%';
    box.classList.remove('is-indeterminate');
  } else {
    fill.style.width = '35%';
  }
  msg.textContent = (progress && progress.message) || (busy ? spec.busyText : '');
  const bits = [];
  if (total > 0) bits.push(`${done}/${total} feeds`);
  const count = progress && progress[spec.countKey];
  if (count != null) bits.push(`${Number(count).toLocaleString()} ${spec.countNoun}`);
  if (phase && phase !== 'fetching' && phase !== 'fetched') bits.push(phase);
  meta.textContent = bits.join(' · ');
}

function wireScrape(spec) {
  const btn = el(spec.ids.btn);
  const status = el(spec.ids.status);
  if (!btn || !status) return;
  const paint = (progress, busy) => paintScrape(spec, progress, busy);
  const served = typeof location !== 'undefined' && location.protocol === 'http:';
  if (!served) {
    btn.disabled = true;
    btn.classList.add('is-file');
    btn.title = 'Start the dashboard with: python -m src.report --serve 8765 --open';
    status.textContent = spec.fileText;
    return;
  }
  status.textContent = spec.readyText;
  spec.onReady(status);

  let pollTimer = null;
  const stopPoll = () => {
    if (pollTimer != null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  };
  const startPoll = () => {
    stopPoll();
    const tick = async () => {
      try {
        const res = await spec.askStatus();
        const body = await res.json().catch(() => ({}));
        const mine = !body.busy_kind || body.busy_kind === spec.kind;
        if (mine) paint(body.progress, !!body.busy && mine);
        if (body.busy && body.busy_kind === spec.kind) {
          btn.disabled = true;
          status.textContent = spec.runningText;
        } else if (body.busy && body.busy_kind === spec.heldBy && spec.heldText) {
          // The lock-holder must be the kind `heldText` actually names. The fold
          // first wrote this as a bare `body.busy`, which let an unnamed busy
          // state ("", null) claim a promo scrape was running — overwriting
          // whatever the line said, including a failure the reader needs — and
          // then as `body.busy_kind &&`, which did the same for any third kind
          // under a sentence that names the second.
          status.textContent = spec.heldText;
        }
      } catch (_) { /* keep last paint */ }
    };
    tick();
    pollTimer = setInterval(tick, 500);
  };

  // Resume the progress UI if this tab opened while a scrape was already running.
  spec.askStatus()
    .then((r) => r.json())
    .then((body) => {
      if (body && body.busy && body.busy_kind === spec.kind) {
        btn.disabled = true;
        status.textContent = spec.runningText;
        startPoll();
      }
    })
    .catch(() => {});

  const fail = (err) => {
    stopPoll();
    status.textContent = spec.failPrefix + err;
    paint({ phase: 'error', message: String(err) }, false);
    el(spec.ids.box).classList.add('on');
    btn.disabled = false;
  };

  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    status.textContent = spec.busyText;
    paint({
      phase: 'starting',
      message: spec.startText,
      done: 0,
      total: 0,
      [spec.countKey]: 0,
    }, true);
    startPoll();
    try {
      const res = await spec.postCollect(spec.payload());
      const body = await res.json().catch(() => ({}));
      stopPoll();
      if (!res.ok || !body.ok) {
        fail(body.error || res.statusText || res.status);
        return;
      }
      const count = (body.collect && body.collect[spec.countKey]) || 0;
      const done = spec.onDone(body, count, status);
      paint({
        phase: 'done',
        message: done.message,
        [spec.countKey]: count,
        done: 1,
        total: 1,
      }, false);
      if (done.reload) {
        if (done.hash) location.hash = done.hash;
        location.reload();
        return;
      }
      if (done.after) done.after();
      btn.disabled = false;
    } catch (err) {
      fail(err && err.message ? err.message : err);
    }
  });
}

wireScrape(SCRAPE_KINDS.odds);
wireScrape(SCRAPE_KINDS.promos);

/* The ledger's controls span three panels, so they are wired once at the body
   rather than by any one renderer. */
wireBetControls();
wireBetForm();

const refreshPromos = () => refreshPanel('promos');
const refilterPromos = afterTyping(refreshPromos);
['promo-kind', 'promo-source', 'promo-region', 'promo-q'].forEach((id) => {
  const node = el(id);
  if (!node) return;
  node.addEventListener('input', id === 'promo-q' ? refilterPromos : refreshPromos);
});
"""


#: The whole page shown when there is nothing to show yet: a wiped database still
#: has to be able to scrape from the UI.  Handwritten here beside ``CSS``/``BODY``/
#: ``JS`` rather than inside :mod:`src.report`, which builds payloads and serves
#: them.  Its palette is still its own and it still declares `color-scheme: dark`
#: where `CSS` carries both schemes — moving it changed which file it lives in, not
#: that, so a light-mode operator still gets a dark shell on an empty database.
EMPTY_SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Line shop — scrape to begin</title>
<style>
  :root { color-scheme: dark; --ground:#141414; --surface:#1a1a1a; --ink:#ecece8;
    --muted:#8a8a82; --line:#333; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
    background:var(--ground); color:var(--ink);
    font:400 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .card { width:min(400px, 92vw); padding:22px; border:1px solid var(--line);
    border-radius:4px; background:var(--surface); }
  h1 { margin:0 0 6px; font:600 18px/1.25 system-ui; }
  p { margin:0 0 14px; color:var(--muted); font-size:13px; }
  select, button { width:100%; padding:8px 10px; border-radius:3px; border:1px solid var(--line);
    font:500 13px/1.2 system-ui; margin-top:6px; background:var(--surface); color:var(--ink); }
  button { background:#222; cursor:pointer; }
  button:hover { border-color:var(--muted); }
  button:disabled { opacity:0.55; cursor:wait; }
  #status { margin-top:10px; font:400 12px/1.4 ui-monospace, monospace; color:var(--muted); }
  .bar { margin-top:10px; height:4px; border-radius:2px; background:#2a2a2a; overflow:hidden; display:none; }
  .bar.on { display:block; }
  .bar > i { display:block; height:100%; width:0%; background:#7aa3c9; transition:width .25s ease; }
</style></head><body>
<div class="card">
  <h1>Line shop</h1>
  <p>No scrapes yet. Pull live prices, then this page reloads as the odds board.</p>
  <label for="scope" style="font-size:12px;color:var(--muted)">Scope</label>
  <select id="scope">
    <option value="league:MLB" selected>MLB baseball (fast)</option>
    <option value="sport:baseball">All baseball</option>
    <option value="all">Everything (slower)</option>
  </select>
  <button type="button" id="go">Scrape now</button>
  <div class="bar" id="bar"><i id="fill"></i></div>
  <div id="status">Ready.</div>
</div>
<script>
const btn = document.getElementById('go');
const status = document.getElementById('status');
const scope = document.getElementById('scope');
const bar = document.getElementById('bar');
const fill = document.getElementById('fill');
let poll = null;
function payload() {
  const v = scope.value || 'league:MLB';
  if (v === 'all') return { tier: 'core' };
  if (v.startsWith('sport:')) return { tier: 'core', sport: v.slice(6) };
  if (v.startsWith('league:')) return { tier: 'core', league: v.slice(7) };
  return { tier: 'core', league: 'MLB' };
}
function paint(p) {
  if (!p) return;
  bar.classList.add('on');
  status.textContent = p.message || 'Scraping…';
  const done = Number(p.done) || 0, total = Number(p.total) || 0;
  if (total > 0) fill.style.width = Math.max(4, Math.round(done / total * 100)) + '%';
}
function startPoll() {
  if (poll) clearInterval(poll);
  poll = setInterval(async () => {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      const b = await r.json();
      if (b.progress) paint(b.progress);
    } catch (_) {}
  }, 500);
}
btn.addEventListener('click', async () => {
  btn.disabled = true;
  status.textContent = 'Scraping…';
  bar.classList.add('on');
  startPoll();
  try {
    const res = await fetch('/api/collect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload()),
    });
    const body = await res.json().catch(() => ({}));
    if (poll) clearInterval(poll);
    if (!res.ok || !body.ok) {
      status.textContent = 'Failed: ' + (body.error || res.statusText || res.status);
      btn.disabled = false;
      return;
    }
    status.textContent = 'Got ' + ((body.collect && body.collect.quote_count) || 0) + ' prices — reloading…';
    fill.style.width = '100%';
    location.reload();
  } catch (err) {
    if (poll) clearInterval(poll);
    status.textContent = 'Failed: ' + (err && err.message ? err.message : err);
    btn.disabled = false;
  }
});
</script></body></html>
"""
