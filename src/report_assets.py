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

section { margin-bottom: 34px; scroll-margin-top: 18px; }
section > header { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
section > header h2 { margin: 0; font: 700 16px/1.2 var(--sans); letter-spacing: -0.015em; }
section > header p { margin: 0; color: var(--muted); font-size: 12.5px; max-width: 78ch; }

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

/* ── the explainer ─────────────────────────────────────────────────────── */

.explain { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
.explain > div { display: flex; flex-direction: column; gap: 5px; }
.explain h4 {
  margin: 0; font: 600 10px/1.2 var(--sans); letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--accent);
}
.explain p { margin: 0; font-size: 13px; color: var(--ink-2); max-width: 46ch; }
.explain b.big { font: 700 17px/1.2 var(--mono); font-variant-numeric: tabular-nums; color: var(--ink); }

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
.src { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 14px; display: flex; flex-direction: column; gap: 10px; }
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
"""


BODY = """
<div class="shell">
  <aside class="rail">
    <div class="brand">
      <b>Baseball Odds Collector</b>
      <span id="brand-sub">local pipeline</span>
    </div>

    <nav class="nav" id="nav" aria-label="Sections">
      <a href="#overview">Start here</a>
      <a href="#sources">Sportsbooks <i id="nav-sources"></i></a>
      <a href="#events">Games <i id="nav-events"></i></a>
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

    <div class="notice" id="run-notice"></div>

    <section id="overview">
      <header>
        <h2>Start here</h2>
        <p>If you have never read a betting page, this is the part to read.</p>
      </header>

      <div class="card">
        <div class="card-head"><h3>How to read a price</h3><span class="eyebrow">the only three things you need</span></div>
        <div class="card-body">
          <div class="explain">
            <div>
              <h4>A price is a payout</h4>
              <b class="big">2.30 &rarr; $230 back</b>
              <p>Bet $100 at 2.30 and a win returns $230 — your $100 back plus $130 profit.
              A US sportsbook writes the same price as <b>+130</b>. A minus sign, like
              &minus;150, marks the favourite: you would stake $150 to profit $100.</p>
            </div>
            <div>
              <h4>Lower price, likelier outcome</h4>
              <b class="big">1 &divide; 2.30 = 43%</b>
              <p>Flip a price over and you get roughly how likely the book thinks that
              outcome is. Short prices are favourites, long prices are underdogs.</p>
            </div>
            <div>
              <h4>Every bet has sides</h4>
              <b class="big">43% + 61% = 104%</b>
              <p>The book prices both sides, and the two add up to more than 100%. That
              extra 4% is its cut. It is also a useful check: under 100% would be
              impossible, and would mean this tool had mixed up which prices belong
              together.</p>
            </div>
          </div>
          <p class="note">Four kinds of bet are collected: <b>who wins</b>, <b>winner with a
          handicap</b>, <b>total runs</b> scored by both teams, and <b>one team's runs</b>. The
          <a href="#glossary">glossary</a> explains each one in a sentence.</p>
        </div>
      </div>

      <div class="card"><div class="card-body flush"><div class="stats" id="stat-strip"></div></div></div>

      <div class="card">
        <div class="card-head">
          <h3>What happened on this collection</h3>
          <span class="eyebrow">each step feeds the next</span>
        </div>
        <div class="card-body"><div class="flow" id="flow"></div>
          <p class="note">Pages are saved to disk <em>before</em> anything reads them. That is
          what makes it possible to re-check the whole thing later without going back
          online — and to prove no number was quietly changed after the fact.</p>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><h3>What was collected, by kind of bet</h3><span class="eyebrow">prices per sportsbook</span></div>
        <div class="card-body flush scroll"><table id="matrix"></table></div>
      </div>
    </section>

    <section id="sources">
      <header>
        <h2>Sportsbooks</h2>
        <p>Three of them, each read straight from its own public website. No paid data
        provider, no account, no login.</p>
      </header>
      <div class="sources" id="source-cards"></div>
      <div class="card">
        <div class="card-head">
          <h3>What was left alone on purpose</h3>
          <span class="eyebrow">seen, counted, not collected</span>
        </div>
        <div class="card-body flush scroll tall"><table id="skips"></table></div>
      </div>
      <p class="note">These bets exist on the sportsbooks' pages but are not stored, because
      recording one as something it isn't would be worse than not having it. Everything
      skipped is counted, so nothing disappears quietly.</p>
    </section>

    <section id="events">
      <header>
        <h2>Games</h2>
        <p>Today's games, and how many prices each sportsbook published for each one.
        Click any game to see its prices side by side.</p>
      </header>
      <div class="card">
        <div class="card-head">
          <h3>Which books cover which games</h3>
          <div class="controls">
            <label class="eyebrow" for="cov-mode">Break down</label>
            <select id="cov-mode">
              <option value="source">by sportsbook</option>
              <option value="market">by kind of bet</option>
            </select>
          </div>
        </div>
        <div class="card-body flush scroll"><table class="cov" id="coverage"></table></div>
      </div>
      <div class="card" id="event-detail-card">
        <div class="card-head"><h3 id="event-title">Pick a game above</h3><span class="eyebrow" id="event-sub"></span></div>
        <div class="card-body flush scroll tall"><table id="event-detail"></table></div>
      </div>
      <p class="note">Green marks the best available price for a bet — the same wager pays
      more at one book than another. The last column shows how far apart the books are.</p>
    </section>

    <section id="odds">
      <header>
        <h2>All prices</h2>
        <p>Every price collected, in one table. Three sportsbooks, three different
        formats, one set of columns.</p>
      </header>
      <div class="card">
        <div class="card-head">
          <div class="controls">
            <input type="search" id="q" placeholder="team, game, kind of bet&hellip;" aria-label="Filter prices" />
            <select id="f-source"><option value="">every sportsbook</option></select>
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
        <div class="card-body" id="runs-chart"></div>
      </div>
      <div class="card">
        <div class="card-head">
          <h3>Bets whose price moved</h3>
          <div class="controls">
            <select id="move-source"><option value="">every sportsbook</option></select>
            <span class="eyebrow" id="move-count"></span>
          </div>
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
      <div class="card"><div class="card-body flush"><div class="stats" id="quality-strip"></div></div></div>
      <div class="card">
        <div class="card-head"><h3>Anything that looked wrong</h3><span class="eyebrow">problems worth a human's attention</span></div>
        <div class="card-body flush scroll tall"><table id="findings"></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>The sportsbook's cut</h3><span class="eyebrow">how much margin is built into each bet</span></div>
        <div class="card-body" id="overround"></div>
      </div>
      <div class="card">
        <div class="card-head"><h3>Prices thrown away</h3><span class="eyebrow">offered, but not understood well enough to keep</span></div>
        <div class="card-body flush scroll"><table id="rejections"></table></div>
      </div>
    </section>

    <section id="raw">
      <header>
        <h2>Saved pages</h2>
        <p>Every page fetched is kept exactly as it arrived. These are the originals every
        number above was read from.</p>
      </header>
      <div class="card"><div class="card-body flush scroll tall"><table id="raws"></table></div></div>
      <p class="note">The fingerprint is a checksum of the file's contents. Two fetches with
      the same fingerprint are byte-for-byte identical, which is how a genuinely quiet
      market is told apart from a feed that has got stuck.</p>
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
      <div class="card"><div class="card-body flush scroll"><table id="schema-table"></table></div></div>
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

const el = (id) => document.getElementById(id);
const txt = (v) => (v === null || v === undefined ? '' : String(v));
const str = (i) => (i === null || i === undefined || i < 0 ? null : S[i]);

/* ── plain language ──────────────────────────────────────────────────────── */

// Every enum the pipeline stores, paired with the words a person would use and the
// phrase a sportsbook prints.  The technical value stays reachable as a tooltip.
const BOOKS = { fanduel: 'FanDuel', pinnacle: 'Pinnacle', betrivers_kambi: 'BetRivers' };
const MARKETS = {
  moneyline:       { plain: 'Who wins',                 term: 'moneyline' },
  run_line:        { plain: 'Winner with a handicap',   term: 'run line / spread' },
  total_runs:      { plain: 'Total runs',               term: 'total, over/under' },
  team_total_runs: { plain: "One team's runs",          term: 'team total' },
};
const PERIODS = {
  full_game:       { plain: 'Whole game',      term: 'full game' },
  first_5_innings: { plain: 'First 5 innings', term: 'F5' },
  first_1_inning:  { plain: 'First inning',    term: '1st inning' },
};
const book = (key) => BOOKS[key] || key;
const marketOf = (key) => MARKETS[key] || { plain: txt(key).replace(/_/g, ' '), term: key };
const periodOf = (key) => PERIODS[key] || { plain: txt(key).replace(/_/g, ' '), term: key };
const label = (v) => txt(v).replace(/_/g, ' ');

function nick(rawName) {
  const known = DATA.team_names[rawName];
  return known ? known.nickname : txt(rawName);
}
function abbr(rawName) {
  const known = DATA.team_names[rawName];
  return known ? known.abbr : txt(rawName);
}

const isHalf = (n) => Math.abs(Math.abs(n % 1) - 0.5) < 1e-9;

/** One bet, as a sentence: "Reds win by 2 or more". */
function describeBet(bet, homeRaw, awayRaw) {
  const home = nick(homeRaw), away = nick(awayRaw);
  const picked = bet.selection === 'home' ? home : bet.selection === 'away' ? away : null;
  const after = bet.period === 'first_5_innings' ? ' after 5 innings'
    : bet.period === 'first_1_inning' ? ' after 1 inning' : '';
  const during = bet.period === 'first_5_innings' ? ' in the first 5 innings'
    : bet.period === 'first_1_inning' ? ' in the first inning' : '';
  let text;

  if (bet.market === 'moneyline') {
    text = bet.selection === 'draw'
      ? (after ? `Scores level${after}` : 'Tie')
      : (after ? `${picked} ahead${after}` : `${picked} win`);

  } else if (bet.market === 'run_line') {
    // Half numbers cannot be tied with, so they always settle one way or the other.
    // Whole numbers can land exactly on the handicap, which refunds the stake — say so,
    // because "lose by fewer than 1" is not a thing that can happen in baseball.
    const size = Math.abs(bet.line);
    const margin = (n) => (n === 1 ? 'lose by 1' : `lose by ${n} or fewer`);
    if (bet.line < 0) {
      text = isHalf(size)
        ? `${picked} win by ${Math.ceil(size)} or more`
        : `${picked} win by ${size + 1} or more (a ${size}-run win refunds)`;
    } else {
      const room = isHalf(size) ? Math.floor(size) : size - 1;
      const core = room < 1 ? `${picked} win` : `${picked} win, or ${margin(room)}`;
      text = isHalf(size) ? core : `${core} (a ${size}-run loss refunds)`;
    }
    text += during;

  } else {
    const scorer = bet.market === 'team_total_runs'
      ? (bet.side === 'home' ? home : away)
      : 'Both teams together';
    const n = bet.line;
    if (bet.selection === 'over') {
      text = isHalf(n) ? `${scorer} score ${Math.ceil(n)} or more`
        : `${scorer} score ${n + 1} or more (exactly ${n} refunds)`;
    } else {
      text = isHalf(n) ? `${scorer} score ${Math.floor(n)} or fewer`
        : `${scorer} score ${n - 1} or fewer (exactly ${n} refunds)`;
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
    : ' ' + (bet.market === 'run_line' && bet.line > 0 ? '+' : '') + (+bet.line).toFixed(1);
  const which = bet.side ? ` ${abbr(bet.side === 'home' ? homeRaw : awayRaw)}` : '';
  return `${marketOf(bet.market).term} · ${periodOf(bet.period).term}${which} · ${side}${number}`;
}

/** A row from the quotes table, in the shape describeBet() wants. */
const betOf = (r) => ({
  market: str(r[COL.market]), period: str(r[COL.period]), selection: str(r[COL.selection]),
  side: str(r[COL.side]), line: r[COL.line], is_alternate: !!r[COL.is_alternate],
});

/* ── formatting ──────────────────────────────────────────────────────────── */

function fmtOdds(d) { return d.toFixed(2); }
function fmtAmerican(a) { return (a > 0 ? '+' : '−') + Math.abs(a); }
function fmtReturn(d) { return '$' + (d * 100).toFixed(0); }
function fmtNumber(v, market) {
  if (v === null || v === undefined) return '';
  return (market === 'run_line' && v > 0 ? '+' : '') + (+v).toFixed(1);
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
  const head = columns.map((c) =>
    `<th class="${c.num ? 'num' : ''}"${c.hint ? ` title="${escapeHtml(c.hint)}"` : ''}>${c.label}</th>`
  ).join('');
  const body = rows.map((r) => '<tr' + (r._attrs || '') + '>' + columns.map((c) => {
    const cell = c.cell(r);
    const cls = [c.num ? 'num' : '', cell.cls || ''].filter(Boolean).join(' ');
    return `<td class="${cls}"${cell.title ? ` title="${escapeHtml(cell.title)}"` : ''}>${
      cell.html !== undefined ? cell.html : escapeHtml(cell.text)}</td>`;
  }).join('') + '</tr>').join('');
  node.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
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
const currentRows = () => rowsByRun.get(currentRunId) || [];

function buildRunPicker() {
  const pick = el('run-pick');
  pick.innerHTML = runs.map((r, i) => {
    const when = fmtClock(r.started_at);
    const flag = r.ok ? '' : '  — problems';
    const kept = rowsByRun.has(r.id) ? '' : '  — prices not in this page';
    return `<option value="${r.id}">${i === 0 ? 'Latest: ' : ''}${when}${flag}${kept}</option>`;
  }).join('');
  pick.value = String(currentRunId);
  pick.addEventListener('change', () => { currentRunId = +pick.value; renderRunScoped(); });
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

  el('masthead-pills').innerHTML = [
    `<span class="pill ${run.ok ? 'ok' : 'bad'}"><i></i>${run.ok ? 'all checks passed' : 'checks found problems'}</span>`,
    `<span class="pill ${producing.length >= 2 ? 'flat' : 'bad'}">${producing.length} of ${health.length} sportsbooks responded</span>`,
    `<span class="pill flat">${runs.length} collection${runs.length === 1 ? '' : 's'} so far</span>`,
  ].join('');

  const notice = el('run-notice');
  notice.classList.toggle('on', !hasRows);
  notice.innerHTML = hasRows ? '' :
    `<b>&#9432;</b><span>This collection's ${run.quote_count.toLocaleString()} prices are not
     included in this page — only the ${DATA.meta.runs_with_rows} most recent collection(s) carry
     prices, to keep the file small. The totals below come from its own summary, so the tables
     further down are empty on purpose. Rebuild with
     <code>--quote-runs ${DATA.meta.runs_recorded}</code> to include it.</span>`;

  const stats = [
    ['prices collected', (hasRows ? rows.length : run.quote_count).toLocaleString(), 'one per bet you could place'],
    ['games', hasRows ? events.size : run.event_count, `playing ${DATA.meta.slate_dates}`],
    ['separate bets', hasRows ? groups.size.toLocaleString() : '—', 'each with every side priced'],
    ['sportsbooks', producing.length, producing.map((h) => book(h.key)).join(', ')],
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
  const steps = [
    ['1. asked', requests, `${health.length} sportsbooks, ${fmtBytes(bytes)} downloaded`],
    ['2. saved', run.raw_count, 'originals kept on disk'],
    ['3. read', parsed + rejected, `${skipped.toLocaleString()} other bets seen and skipped`],
    ['4. checked', parsed, `${run.error_count} problem${run.error_count === 1 ? '' : 's'}, ${run.warning_count} worth a look`],
    ['5. stored', run.quote_count, 'no bet stored twice'],
  ];
  el('flow').innerHTML = steps.map((s, i) =>
    (i ? '<div class="flow-arrow">&rarr;</div>' : '') +
    `<div class="flow-step"><span>${escapeHtml(s[0])}</span><b>${Number(s[1]).toLocaleString()}</b><small>${escapeHtml(s[2])}</small></div>`
  ).join('');

  const sources = [...new Set(rows.map((r) => str(r[COL.source])))].sort();
  const combos = new Map();
  for (const r of rows) {
    const key = str(r[COL.market]) + '\x1f' + str(r[COL.period]);
    if (!combos.has(key)) combos.set(key, new Map());
    const per = combos.get(key);
    const src = str(r[COL.source]);
    per.set(src, (per.get(src) || 0) + 1);
  }
  const matrixRows = [...combos.entries()].map(([key, per]) => {
    const [market, period] = key.split('\x1f');
    return { market, period, per, total: [...per.values()].reduce((a, b) => a + b, 0) };
  }).sort((a, b) => b.total - a.total);
  table(el('matrix'), [
    { label: 'Kind of bet', cell: (r) => cell(marketOf(r.market).plain, '', marketOf(r.market).term) },
    { label: 'Part of the game', cell: (r) => cell(periodOf(r.period).plain, 'dim') },
    ...sources.map((s) => ({
      label: book(s), num: true,
      cell: (r) => { const v = r.per.get(s) || 0; return cell(v || '—', v ? '' : 'dim'); },
    })),
    { label: 'All books', num: true, cell: (r) => cell(r.total) },
  ], matrixRows, { empty: 'No prices stored for this collection.' });
}

/* ── sportsbooks ─────────────────────────────────────────────────────────── */

function renderSources() {
  const run = runById.get(currentRunId);
  const byKey = new Map(run.sources.map((h) => [h.key, h]));
  el('nav-sources').textContent = run.sources.filter((h) => h.ok).length + '/' + run.sources.length;

  if (!run.sources.length) {
    // A silently blank section reads as "no sportsbooks", which is a different claim.
    el('source-cards').innerHTML =
      `<div class="empty">No per-sportsbook detail was recorded for this collection. Its
       ${run.quote_count.toLocaleString()} prices are still stored — see Games and All prices.</div>`;
    el('skips').innerHTML = '';
    return;
  }

  el('source-cards').innerHTML = DATA.sources.map((src) => {
    const h = byKey.get(src.key);
    if (!h) return '';
    const pill = h.ok
      ? '<span class="pill ok"><i></i>responded normally</span>'
      : `<span class="pill bad"><i></i>${escapeHtml(label(h.error_kind) || 'failed')}</span>`;
    const cells = [
      ['prices', h.quote_count.toLocaleString()],
      ['games', h.event_count],
      ['pages read', h.request_count],
      ['downloaded', fmtBytes(h.raw_bytes)],
      ['took', h.latency_ms === null ? '—' : (h.latency_ms / 1000).toFixed(2) + 's'],
      ['same as last time', `${h.unchanged_payloads} of ${h.request_count}`],
    ];
    return `<div class="src">
      <div class="src-top"><div><b>${escapeHtml(src.label)}</b><code>${escapeHtml(src.host)}</code></div>${pill}</div>
      <p>${escapeHtml(src.what)}</p>
      <div class="src-grid">${cells.map(([k, v]) =>
        `<div><span>${escapeHtml(k)}</span><b>${escapeHtml(String(v))}</b></div>`).join('')}</div>
      ${h.error_message ? `<p class="dim" style="font-size:11.5px">${escapeHtml(h.error_message)}</p>` : ''}
    </div>`;
  }).join('');

  const skips = DATA.skipped.filter((s) => s.run_id === currentRunId)
    .sort((a, b) => b.count - a.count);
  table(el('skips'), [
    { label: 'Sportsbook', cell: (r) => cell(book(r.source)) },
    { label: 'What it was', cell: (r) => cell(r.reason.replace(/^criterion:/, '').replace(/^matchup_type:/, '').replace(/_/g, ' ')) },
    { label: 'How many', num: true, cell: (r) => cell(r.count.toLocaleString()) },
    { label: 'Why it was left alone', cell: (r) => cell(skipNote(r.reason), 'dim wrap') },
  ], skips, { empty: 'Everything this collection saw was in scope.' });
}

/* ── games ───────────────────────────────────────────────────────────────── */

let selectedEvent = null;

function eventSummaries(rows) {
  const events = new Map();
  for (const r of rows) {
    const key = str(r[COL.event_key]);
    if (!events.has(key)) {
      events.set(key, {
        key,
        homeRaw: str(r[COL.home_team]), awayRaw: str(r[COL.away_team]),
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
    // Prefer a spelling the team list recognises, so the page can say "Reds".
    if (!DATA.team_names[e.homeRaw] && DATA.team_names[str(r[COL.home_team])]) e.homeRaw = str(r[COL.home_team]);
    if (!DATA.team_names[e.awayRaw] && DATA.team_names[str(r[COL.away_team])]) e.awayRaw = str(r[COL.away_team]);
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
  const heading = (k) => (mode === 'source' ? book(k) : marketOf(k).plain);
  const pickMap = (e) => (mode === 'source' ? e.bySource : e.byMarket);
  const peak = Math.max(1, ...events.flatMap((e) => [...pickMap(e).values()]));

  const heat = (v) => {
    if (!v) return html('<span class="dim">—</span>', 'cell zero');
    const alpha = 0.10 + 0.42 * (v / peak);
    return html(`<span class="heat" style="background:color-mix(in srgb, var(--accent) ${(alpha * 100).toFixed(0)}%, transparent)">${v}</span>`, 'cell');
  };

  const node = table(el('coverage'), [
    { label: 'First pitch', cell: (e) => cell(fmtClock(e.commence), 'dim') },
    { label: 'Game', cell: (e) => html(`${escapeHtml(nick(e.awayRaw))} <span class="dim">at</span> ${escapeHtml(nick(e.homeRaw))}`) },
    { label: 'Game ID', hint: "This tool's own name for the game, so books can be compared",
      cell: (e) => cell(e.key, 'mono dim') },
    ...keys.map((k) => ({ label: heading(k), num: true, cell: (e) => heat(pickMap(e).get(k) || 0) })),
    { label: 'Books', num: true, cell: (e) => cell(e.bySource.size) },
    { label: 'Prices', num: true, cell: (e) => cell(e.rows.length) },
  ], events.map((e) => Object.assign(e, { _attrs: ` data-event="${escapeHtml(e.key)}"` })),
     { className: 'cov', empty: 'No prices stored for this collection.' });

  if (node) {
    node.querySelectorAll('tbody tr').forEach((tr) => {
      tr.tabIndex = 0;
      tr.addEventListener('click', () => selectEvent(tr.dataset.event, events));
      tr.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectEvent(tr.dataset.event, events); }
      });
    });
  }

  const keep = events.find((e) => e.key === selectedEvent);
  selectEvent(keep ? keep.key : (events[0] && events[0].key), events);
}

function selectEvent(key, events) {
  selectedEvent = key;
  const cov = el('coverage');
  if (cov.querySelectorAll) {
    cov.querySelectorAll('tbody tr').forEach((tr) => tr.classList.toggle('sel', tr.dataset.event === key));
  }
  const event = events.find((e) => e.key === key);
  if (!event) {
    el('event-title').textContent = 'Pick a game above';
    el('event-sub').textContent = '';
    table(el('event-detail'), [{ label: '', cell: () => cell('') }], [], { empty: 'No game selected.' });
    return;
  }

  el('event-title').textContent = `${nick(event.awayRaw)} at ${nick(event.homeRaw)}`;
  el('event-sub').textContent = `${fmtClock(event.commence)} · ${event.rows.length} prices from ${
    event.bySource.size} sportsbook${event.bySource.size === 1 ? '' : 's'} · ${event.key}`;

  const sources = [...event.bySource.keys()].sort();
  const lines = new Map();
  for (const r of event.rows) {
    const bet = betOf(r);
    const k = [bet.market, bet.period, bet.side || '', bet.line, bet.selection, bet.is_alternate].join('\x1f');
    if (!lines.has(k)) lines.set(k, Object.assign({}, bet, { prices: new Map() }));
    lines.get(k).prices.set(str(r[COL.source]), r);
  }

  const ordering = { moneyline: 0, run_line: 1, total_runs: 2, team_total_runs: 3 };
  const periodOrder = { full_game: 0, first_5_innings: 1, first_1_inning: 2 };
  // Totals read best as over/under pairs at each number, so they sort by number first.
  // Handicaps read best as one ladder per team — a book offers a team at both +1 and −1
  // as separate bets, so pairing by the number alone would interleave four rows.
  const within = (a, b) => (a.market === 'run_line'
    ? a.selection.localeCompare(b.selection) || ((a.line ?? 0) - (b.line ?? 0))
    : ((a.line ?? 0) - (b.line ?? 0)) || a.selection.localeCompare(b.selection));
  const detail = [...lines.values()].sort((a, b) =>
    (ordering[a.market] - ordering[b.market]) || (periodOrder[a.period] - periodOrder[b.period]) ||
    (a.is_alternate - b.is_alternate) || ((a.side || '').localeCompare(b.side || '')) || within(a, b));

  table(el('event-detail'), [
    { label: 'The bet', cell: (r) => cell(describeBet(r, event.homeRaw, event.awayRaw), 'plain',
        notation(r, event.homeRaw, event.awayRaw)) },
    { label: 'Kind of bet', cell: (r) => cell(marketOf(r.market).plain, 'dim') },
    { label: 'Part of game', cell: (r) => cell(periodOf(r.period).plain, 'dim') },
    ...sources.map((s) => ({
      label: book(s), num: true, hint: `${book(s)}'s price, as a decimal payout per $1`,
      cell: (r) => {
        const q = r.prices.get(s);
        if (!q) return html('<span class="dim">—</span>');
        const best = Math.max(...[...r.prices.values()].map((p) => p[COL.decimal_odds]));
        const isBest = r.prices.size > 1 && q[COL.decimal_odds] === best;
        const suspended = str(q[COL.status]) !== 'active';
        const note = `${fmtAmerican(q[COL.american_odds])} · $100 returns ${fmtReturn(q[COL.decimal_odds])}${
          suspended ? ' · not taking bets right now' : ''}`;
        return html(`<span class="${isBest ? 'best' : ''}${suspended ? ' dim' : ''}">${
          fmtOdds(q[COL.decimal_odds])}</span>`, '', note);
      },
    })),
    {
      label: 'Best vs worst', num: true, hint: 'How much more the best price pays than the worst',
      cell: (r) => {
        const vals = [...r.prices.values()].map((p) => p[COL.decimal_odds]);
        if (vals.length < 2) return html('<span class="dim">—</span>');
        const pct = (Math.max(...vals) / Math.min(...vals) - 1) * 100;
        return cell(pct.toFixed(1) + '%', pct >= 2 ? 'up' : 'dim');
      },
    },
  ], detail, { empty: 'No prices for this game.' });
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

function renderOdds() {
  const rows = currentRows();
  fillSelect(el('f-source'), [...new Set(rows.map((r) => str(r[COL.source])))].sort(), 'every sportsbook', book);
  fillSelect(el('f-market'), [...new Set(rows.map((r) => str(r[COL.market])))].sort(), 'every kind of bet', (v) => marketOf(v).plain);
  fillSelect(el('f-period'), [...new Set(rows.map((r) => str(r[COL.period])))].sort(), 'any part of the game', (v) => periodOf(v).plain);

  const query = el('q').value.trim().toLowerCase();
  const fSource = el('f-source').value, fMarket = el('f-market').value;
  const fPeriod = el('f-period').value, fAlt = el('f-alt').value;

  let filtered = rows.filter((r) => {
    if (fSource && str(r[COL.source]) !== fSource) return false;
    if (fMarket && str(r[COL.market]) !== fMarket) return false;
    if (fPeriod && str(r[COL.period]) !== fPeriod) return false;
    if (fAlt !== '' && String(r[COL.is_alternate]) !== fAlt) return false;
    if (query) {
      const home = str(r[COL.home_team]), away = str(r[COL.away_team]);
      const hay = [str(r[COL.event_key]), home, away, nick(home), nick(away),
        marketOf(str(r[COL.market])).plain, marketOf(str(r[COL.market])).term,
        periodOf(str(r[COL.period])).plain, str(r[COL.selection]), book(str(r[COL.source]))]
        .join(' ').toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });

  const columns = [
    { key: 'source', label: 'Sportsbook', cell: (r) => cell(book(str(r[COL.source]))),
      sort: (r) => str(r[COL.source]) },
    { key: 'event', label: 'Game', hint: 'Away team at home team',
      cell: (r) => cell(`${nick(str(r[COL.away_team]))} at ${nick(str(r[COL.home_team]))}`, '', str(r[COL.event_key])),
      sort: (r) => str(r[COL.event_key]) },
    { key: 'bet', label: 'The bet', cell: (r) => cell(
        describeBet(betOf(r), str(r[COL.home_team]), str(r[COL.away_team])), 'plain',
        notation(betOf(r), str(r[COL.home_team]), str(r[COL.away_team]))),
      sort: (r) => describeBet(betOf(r), str(r[COL.home_team]), str(r[COL.away_team])) },
    { key: 'market', label: 'Kind of bet', cell: (r) => cell(marketOf(str(r[COL.market])).plain, 'dim'),
      sort: (r) => str(r[COL.market]) },
    { key: 'period', label: 'Part of game', cell: (r) => cell(periodOf(str(r[COL.period])).plain, 'dim'),
      sort: (r) => str(r[COL.period]) },
    { key: 'dec', label: 'Price', hint: 'Decimal odds: total returned per $1 staked', num: true,
      cell: (r) => cell(fmtOdds(r[COL.decimal_odds])), sort: (r) => r[COL.decimal_odds] },
    { key: 'us', label: 'US odds', hint: 'The same price in American format', num: true,
      cell: (r) => cell(fmtAmerican(r[COL.american_odds]), 'dim'), sort: (r) => r[COL.american_odds] },
    { key: 'ret', label: '$100 returns', hint: 'What a winning $100 bet pays back in total', num: true,
      cell: (r) => cell(fmtReturn(r[COL.decimal_odds])), sort: (r) => r[COL.decimal_odds] },
    { key: 'prob', label: "Book's chance", hint: 'How likely the sportsbook is treating this outcome', num: true,
      cell: (r) => cell((r[COL.implied_probability] * 100).toFixed(1) + '%', 'dim'),
      sort: (r) => r[COL.implied_probability] },
    { key: 'limit', label: 'Max bet', hint: 'Largest stake the book will accept, where it says', num: true,
      cell: (r) => cell(r[COL.limit_amount] === null ? '—' : '$' + Math.round(r[COL.limit_amount]).toLocaleString(), 'dim'),
      sort: (r) => r[COL.limit_amount] ?? -1 },
    { key: 'status', label: 'Taking bets', cell: (r) => {
        const active = str(r[COL.status]) === 'active';
        return html(`<span class="pill ${active ? 'flat' : 'warn'}">${active ? 'yes' : 'paused'}</span>`);
      }, sort: (r) => str(r[COL.status]) },
    { key: 'changed', label: 'Book last moved it', num: false,
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
  el('odds-note').innerHTML = filtered.length > cap
    ? `Showing the first ${cap} of ${filtered.length.toLocaleString()} matching prices — narrow the
       filters or search to see the rest. All of them are in the database; the table is capped only
       so your browser stays quick. Click any column heading to sort.`
    : 'Click any column heading to sort. Hover a row to see the same bet in sportsbook shorthand.';

  const node = table(el('odds-table'), columns, shown, { empty: 'No prices match those filters.' });
  if (node) {
    node.querySelectorAll('thead th').forEach((th, i) => {
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

function renderMovement() {
  svgRunsChart();

  const ordered = runs.slice().reverse().filter((r) => rowsByRun.has(r.id));
  const series = new Map();
  for (const run of ordered) {
    for (const r of rowsByRun.get(run.id)) {
      const bet = betOf(r);
      const key = [str(r[COL.source]), str(r[COL.event_key]), bet.market, bet.period,
        bet.side || '', bet.selection, bet.line, bet.is_alternate].join('\x1f');
      if (!series.has(key)) series.set(key, { row: r, values: [] });
      const s = series.get(key);
      s.values.push(r[COL.decimal_odds]);
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

  const stable = series.size - moved.length;
  el('move-count').textContent = `${moved.length.toLocaleString()} of ${series.size.toLocaleString()} bets changed price`;
  el('nav-move').textContent = moved.length.toLocaleString();

  table(el('move-table'), [
    { label: 'Sportsbook', cell: (s) => cell(book(str(s.row[COL.source]))) },
    { label: 'Game', cell: (s) => cell(
        `${nick(str(s.row[COL.away_team]))} at ${nick(str(s.row[COL.home_team]))}`, '', str(s.row[COL.event_key])) },
    { label: 'The bet', cell: (s) => cell(
        describeBet(betOf(s.row), str(s.row[COL.home_team]), str(s.row[COL.away_team])), 'plain',
        notation(betOf(s.row), str(s.row[COL.home_team]), str(s.row[COL.away_team]))) },
    { label: 'Over time', hint: 'Oldest on the left, newest on the right', cell: (s) => html(sparkline(s.values)) },
    { label: 'Started at', num: true, cell: (s) => cell(fmtOdds(s.first), 'dim') },
    { label: 'Now', num: true, cell: (s) => cell(fmtOdds(s.last)) },
    { label: 'Change', num: true, hint: 'How much the payout has moved since first seen',
      cell: (s) => cell((s.drift > 0 ? '+' : '−') + Math.abs(s.drift).toFixed(2) + '%',
        s.drift > 0 ? 'up' : 'down') },
    { label: 'Seen', num: true, hint: 'How many collections this bet appeared in',
      cell: (s) => cell(s.values.length) },
  ], rows.slice(0, 300), {
    empty: ordered.length < 2
      ? 'Only one collection has prices in this page — collect again to watch them move.'
      : 'No price changed between these collections.',
  });

  el('move-note').textContent = ordered.length < 2
    ? 'Comparing prices needs at least two collections in this page. Collect again, then rebuild it.'
    : `${stable.toLocaleString()} bets held exactly the same price across all ${ordered.length} collections` +
      (rows.length > 300 ? `; the 300 biggest movers of ${rows.length.toLocaleString()} are shown` : '') +
      '. A book quietly serving a stale copy would show no movement at all here, and its saved pages ' +
      'below would be identical every time — which is why both are on this page.';
}

/* ── checks ──────────────────────────────────────────────────────────────── */

function renderQuality() {
  const run = runById.get(currentRunId);
  const rows = currentRows();
  const findings = DATA.findings.filter((f) => f.run_id === currentRunId);
  el('nav-quality').textContent = findings.length ? String(findings.length) : 'clear';

  const groups = marketGroups(rows.filter((r) => str(r[COL.status]) === 'active'));
  const overrounds = [];
  for (const group of groups.values()) {
    // Only a bet with every side priced says anything about the book's margin.
    const bySelection = new Map(group.map((r) => [str(r[COL.selection]) + (str(r[COL.side]) || ''), r]));
    if (bySelection.size < 2) continue;
    const sum = [...bySelection.values()].reduce((a, r) => a + 1 / r[COL.decimal_odds], 0);
    overrounds.push({ sum, source: str(group[0][COL.source]) });
  }
  const sums = overrounds.map((o) => o.sum).sort((a, b) => a - b);
  const median = sums.length ? sums[Math.floor(sums.length / 2)] : null;
  const impossible = overrounds.filter((o) => o.sum < 1).length;

  const items = [
    ['problems found', findings.length, findings.length ? 'listed below' : 'nothing flagged',
      findings.length ? 'is-warn' : 'is-good'],
    ['prices tested', rows.length.toLocaleString(), 'teams, times, numbers, duplicates'],
    ['bets fully priced', overrounds.length.toLocaleString(), 'every side present'],
    ["book's usual cut", median === null ? '—' : ((median - 1) * 100).toFixed(1) + '%', 'built into the price'],
    ['impossible prices', impossible, impossible ? 'prices are mispaired' : 'none — the pricing adds up',
      impossible ? 'is-bad' : 'is-good'],
    ['re-read from disk', DATA.meta.replay_note, 'same answer as when stored'],
  ];
  el('quality-strip').innerHTML = items.map(([name, value, sub, cls]) =>
    `<div class="stat ${cls || ''}"><span>${escapeHtml(name)}</span><b>${escapeHtml(String(value))}</b><small>${escapeHtml(sub)}</small></div>`
  ).join('');

  table(el('findings'), [
    { label: 'How serious', cell: (f) => html(`<span class="pill ${f.severity === 'error' ? 'bad' : 'warn'}"><i></i>${
        f.severity === 'error' ? 'problem' : 'worth a look'}</span>`) },
    { label: 'Check', cell: (f) => cell(label(f.code)) },
    { label: 'Sportsbook', cell: (f) => cell(f.source ? book(f.source) : '—', 'dim') },
    { label: 'Game', cell: (f) => cell(f.event_key || '—', 'mono dim') },
    { label: 'What it says', cell: (f) => cell(f.message, 'wrap') },
  ], findings, { empty: 'Nothing was flagged in this collection — no problems, nothing worth a look.' });

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
  <p class="note">Add up the chances a book implies for every side of one bet and the total comes to
  more than 100%. That excess is its cut, and it is how sportsbooks make money. It is also a check on
  this tool: a bet totalling <em>under</em> 100% would be free money, which never happens — so it
  would mean the wrong prices had been paired together.</p>`;

  const rejections = DATA.rejections.filter((r) => r.run_id === currentRunId);
  table(el('rejections'), [
    { label: 'Sportsbook', cell: (r) => cell(book(r.source)) },
    { label: 'Why', cell: (r) => cell(label(r.reason)) },
    { label: 'Detail', cell: (r) => cell(r.detail, 'wrap') },
  ], rejections, { empty: 'Nothing had to be thrown away in this collection.' });
}

/* ── saved pages ─────────────────────────────────────────────────────────── */

function renderRaw() {
  const raws = DATA.raws.filter((r) => r.run_id === currentRunId);
  el('nav-raw').textContent = raws.length;
  table(el('raws'), [
    { label: 'Sportsbook', cell: (r) => cell(book(r.source)) },
    { label: 'Which page', cell: (r) => cell(label(r.endpoint)) },
    { label: 'Reply', hint: 'The HTTP status the server sent back; 200 means OK',
      cell: (r) => html(`<span class="pill ${r.status_code === 200 ? 'ok' : 'bad'}">${
        r.status_code === 200 ? 'OK' : r.status_code}</span>`) },
    { label: 'Fetched at', cell: (r) => cell(fmtTime(r.fetched_at), 'mono dim') },
    { label: 'Size', num: true, cell: (r) => cell(fmtBytes(r.byte_size)) },
    { label: 'Fingerprint', hint: 'A checksum of the file, shortened', cell: (r) => cell(r.sha256.slice(0, 12), 'mono dim') },
    { label: 'Since last time', cell: (r) => html(r.unchanged
        ? '<span class="pill flat">identical</span>' : '<span class="pill accent">new content</span>') },
    { label: 'Address', cell: (r) => cell(r.url, 'dim') },
  ], raws, { empty: 'This collection saved nothing.' });
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
  renderSources();
  renderEvents();
  renderOdds();
  renderMovement();
  renderQuality();
  renderRaw();
}

function scrollSpy() {
  const links = [...document.querySelectorAll('.nav a')];
  const sections = links.map((a) => document.querySelector(a.getAttribute('href'))).filter(Boolean);
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      links.forEach((a) => a.setAttribute('aria-current', String(a.getAttribute('href') === '#' + entry.target.id)));
    });
  }, { rootMargin: '-10% 0px -80% 0px' });
  sections.forEach((s) => observer.observe(s));
}

buildRunPicker();
renderRunScoped();
renderReference();
scrollSpy();

['q', 'f-source', 'f-market', 'f-period', 'f-alt'].forEach((id) => {
  el(id).addEventListener('input', renderOdds);
});
el('cov-mode').addEventListener('change', renderEvents);
el('move-source').addEventListener('change', renderMovement);
"""
