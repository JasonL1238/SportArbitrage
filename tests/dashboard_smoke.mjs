// Runs the dashboard's own script against a real generated page, under a DOM stub
// small enough to have no dependencies.  It cannot check layout or styling; what it
// does check is that every render path executes, fills the region it owns, and never
// leaves NaN or undefined on screen — the two failures that look like missing data
// rather than like a crash.  Driven by tests/test_report.py, which skips it when
// node is not installed.
//
//   node tests/dashboard_smoke.mjs data/dashboard.html
import { readFileSync } from 'node:fs';

const page = readFileSync(process.argv[2], 'utf8');

const payload = page.match(/<script type="application\/json" id="report-data">([\s\S]*?)<\/script>/)[1];
const script = page.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];

const nodes = new Map();
let counted = 0;

function make(id) {
  const node = {
    id,
    tagName: 'DIV',
    tabIndex: 0,
    value: '',
    textContent: '',
    _html: '',
    dataset: {},
    style: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); } else if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    set innerHTML(v) { this._html = v; counted += 1; },
    get innerHTML() { return this._html; },
    setAttribute() {},
    getAttribute(name) { return name === 'href' ? '#overview' : null; },
    // Kept rather than dropped, so the harness can do what a reader does: the
    // page's whole run-scoped half is reachable only through this handler, and
    // a stub that swallowed it meant every render path was only ever executed
    // against the run the page happens to open on.
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    dispatch(type) { for (const fn of this._listeners[type] || []) fn(); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    replaceWith(other) { nodes.set(other.id || id, other); },
    insertAdjacentHTML() {},
    appendChild() {},
  };
  return node;
}

globalThis.document = {
  documentElement: make('root'),
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, make(id));
    return nodes.get(id);
  },
  querySelector(sel) { return this.getElementById(sel.replace(/[^a-z0-9_-]/gi, '')); },
  querySelectorAll() { return []; },
  createElement(tag) { const n = make(''); n.tagName = tag.toUpperCase(); return n; },
};
document.getElementById('report-data').textContent = payload;

globalThis.IntersectionObserver = class { observe() {} };
globalThis.window = globalThis;

const errors = [];
try {
  // The script is one function scope, so appending exports reaches its internals.
  new Function(script + `
    globalThis.__describeBet = describeBet;
    globalThis.__nick = nick;
    globalThis.__participants = DATA.participants;
    globalThis.__movementCounts = movementCounts;
    globalThis.__moneylineSides = moneylineSides;
    globalThis.__detailLoaded = detailLoaded;
    globalThis.__DATA = DATA;
    globalThis.__movementKey = movementKey;
    globalThis.__keepBetter = keepBetter;
    globalThis.__COL = COL;
    globalThis.__sumsToAMargin = sumsToAMargin;
    globalThis.__fixtureAnchorsFor = fixtureAnchorsFor;
    globalThis.__fixtureBucket = fixtureBucket;
    globalThis.__fixtureWindowMs = fixtureWindowMs;
    globalThis.__marketGroups = marketGroups;
    globalThis.__renderBook = renderBook;
    globalThis.__renderSources = renderSources;
    globalThis.__scopesFailedOf = scopesFailedOf;
    globalThis.__boardQuotes = boardQuotes;
    globalThis.__boardSideLabels = boardSideLabels;
    globalThis.__fmtLine = fmtLine;
    globalThis.__priceCell = priceCell;
    globalThis.__americanOf = americanOf;
    globalThis.__consensusLine = consensusLine;
    globalThis.__fmtAmerican = fmtAmerican;
  `)();
} catch (err) {
  errors.push(err);
}

if (errors.length) {
  for (const e of errors) console.error('RUNTIME ERROR:', e.stack);
  process.exit(1);
}

// Prove the render actually produced markup rather than silently no-oping.
const required = ['stat-strip', 'flow', 'matrix', 'sports-grid', 'leagues-grid', 'sports-gaps',
  'source-cards', 'skips', 'coverage', 'event-detail', 'odds-table', 'runs-chart',
  'move-table', 'quality-strip', 'findings', 'overround', 'rejections', 'raws',
  'schema-table', 'vocab', 'sport-pick', 'sport-meta',
  'run-list', 'run-pick', 'scrape-status', 'scrape-progress', 'scrape-msg', 'nav-history',
  'home-stats', 'browse-games', 'events-games',
  'odds-screen', 'screen-note', 'league-pick', 'market-tabs', 'nav-screen',
  'arb-list', 'arb-stats', 'arb-summary', 'arb-rejected', 'nav-arb',
  // The drill-down panels are rendered while off screen, so a link straight into one
  // opens on something. An empty one here means a reader would arrive at a blank page.
  'crumbs', 'event-title', 'event-sub',
  'book-title', 'book-state', 'book-stats', 'book-mix', 'book-skips', 'book-raws',
  'bet-title', 'bet-sub', 'bet-spread', 'bet-books', 'bet-sides', 'bet-history'];
// An empty result set legitimately renders as a text-only empty state, not markup.
const filled = (id) => {
  const n = nodes.get(id);
  return ((n?.innerHTML || '') + (n?.textContent || '')).trim();
};
const blank = required.filter((id) => !filled(id));
console.log(`script ran clean; ${counted} innerHTML writes across ${nodes.size} nodes`);
if (blank.length) {
  console.error('EMPTY REGIONS:', blank.join(', '));
  process.exit(1);
}

// Odds screen pins — the primary board must not silently regress.
{
  const boardProblems = [];
  const screen = filled('odds-screen');
  if (!screen.includes('oj-board') && !screen.includes('No games') && !screen.includes('not embedded')) {
    boardProblems.push('odds-screen missing board table or empty-state copy');
  }
  if (!filled('market-tabs').includes('Moneyline')) {
    boardProblems.push('market-tabs did not render Moneyline/Spread/Total');
  }
  if (globalThis.__fmtLine(0.25, 'spread') !== '+0.25') {
    boardProblems.push(`fmtLine spread quarter wrong: ${globalThis.__fmtLine(0.25, 'spread')}`);
  }
  if (globalThis.__fmtLine(8.5, 'total') !== '8.5') {
    boardProblems.push(`fmtLine total should not force +: ${globalThis.__fmtLine(8.5, 'total')}`);
  }
  // Call site must decode COL.market through str(), not pass the intern index.
  {
    const COL = globalThis.__COL;
    const DATA = globalThis.__DATA;
    let ti = DATA.strings.indexOf('total');
    if (ti < 0) { DATA.strings.push('total'); ti = DATA.strings.length - 1; }
    const q = new Array(Math.max(...Object.values(COL)) + 1).fill(null);
    q[COL.market] = ti;
    q[COL.line] = 8.5;
    q[COL.status] = (() => { let i = DATA.strings.indexOf('active'); if (i < 0) { DATA.strings.push('active'); i = DATA.strings.length - 1; } return i; })();
    q[COL.decimal_odds] = 1.91;
    if (COL.net_decimal_odds !== undefined) q[COL.net_decimal_odds] = 1.91;
    const cell = globalThis.__priceCell(q, null, false);
    if (cell.includes('+8.5')) boardProblems.push('priceCell must not prefix totals with +');
    if (!cell.includes('8.5')) boardProblems.push('priceCell dropped the total line');
  }
  // Same-line best only: two books on -1.5, one juicier loner on -3.5.
  const COL = globalThis.__COL;
  // boardQuotes reads through COL indexes and str() → DATA.strings.
  const DATA = globalThis.__DATA;
  const enc = (s) => {
    let i = DATA.strings.indexOf(s);
    if (i < 0) { DATA.strings.push(s); i = DATA.strings.length - 1; }
    return i;
  };
  const row = (src, sel, line, decimal) => {
    const r = new Array(Math.max(...Object.values(COL)) + 1).fill(null);
    r[COL.source] = enc(src);
    r[COL.market] = enc('spread');
    r[COL.period] = enc('full_game');
    r[COL.selection] = enc(sel);
    r[COL.status] = enc('active');
    r[COL.line] = line;
    r[COL.is_alternate] = 0;
    r[COL.decimal_odds] = decimal;
    r[COL.american_odds] = decimal >= 2
      ? Math.round((decimal - 1) * 100) : -Math.round(100 / (decimal - 1));
    if (COL.net_decimal_odds !== undefined) r[COL.net_decimal_odds] = decimal;
    return r;
  };
  const event = {
    rows: [
      row('bookA', 'home', -1.5, 1.91),
      row('bookB', 'home', -1.5, 1.95),
      row('bookC', 'home', -3.5, 2.80), // juicier, different line — must not win consensus
    ],
    homeRaw: 'HOME', awayRaw: 'AWAY', key: 't', sport: 'baseball', league: 'MLB', commence: '',
  };
  const byBook = globalThis.__boardQuotes(event, 'spread');
  const a = byBook.get('bookA')?.get('home');
  const c = byBook.get('bookC')?.get('home');
  if (!a || +a[COL.line] !== -1.5) boardProblems.push('consensus main line did not keep -1.5 for bookA');
  if (c) boardProblems.push('bookC -3.5 must not appear when consensus is -1.5');
  const b = byBook.get('bookB')?.get('home');
  const tip = globalThis.__priceCell(b, 1.95, true);
  if (!tip.includes('$100 returns')) boardProblems.push('priceCell tip must use "$100 returns"');
  if (!tip.includes('best')) boardProblems.push('priceCell should yellow-mark the best when comparable');
  const worse = globalThis.__priceCell(a, 1.95, true);
  if (worse.includes('best')) boardProblems.push('priceCell must not yellow a worse price on the same line');
  const alone = globalThis.__priceCell(a, 1.91, false);
  if (alone.includes('best')) boardProblems.push('priceCell must not yellow a lone/uncomparable price');
  const labels = globalThis.__boardSideLabels({
    rows: [
      row('bookA', 'home', null, 1.9),
      // F5 draw must not invent a Draw row on the full-game board
      (() => { const r = row('bookA', 'draw', null, 3.2); r[COL.period] = enc('first_5_innings'); r[COL.market] = enc('moneyline'); return r; })(),
    ],
    homeRaw: 'HOME', awayRaw: 'AWAY',
  }, 'moneyline').map(([k]) => k);
  if (labels.includes('draw')) boardProblems.push('Draw row leaked from non-full_game moneyline');
  if (boardProblems.length) {
    console.error('ODDS SCREEN PINS:', boardProblems.join(' | '));
    process.exit(1);
  }
  console.log('  odds-screen pins: ok');
}
// A missing payload field renders as NaN or undefined rather than throwing.
const rendered = [...nodes.entries()].filter(([id]) => id !== 'report-data')
  .map(([, n]) => (n.innerHTML || '') + (n.textContent || '')).join(' ');
const leaks = ['NaN', 'undefined', '[object Object]'].filter((s) => rendered.includes(s));
if (leaks.length) {
  console.error('LEAKED PLACEHOLDER VALUES:', leaks.join(', '));
  for (const [id, n] of nodes) {
    const h = (n.innerHTML || '') + (n.textContent || '');
    for (const s of leaks) if (h.includes(s)) console.error(`  ${id}: …${h.slice(Math.max(0, h.indexOf(s) - 70), h.indexOf(s) + 30)}…`);
  }
  process.exit(1);
}

for (const id of ['lede', 'odds-count', 'move-count', 'move-note', 'event-sub', 'run-meta']) {
  const n = nodes.get(id);
  console.log(`  ${id}: ${JSON.stringify(((n?.textContent || n?.innerHTML || '')).slice(0, 150))}`);
}

// The plain-English sentence is a claim about what a bet settles on. A wrong one is
// worse than notation, because the reader has no way to tell it is wrong. HOME/AWAY are
// used as participant keys so these cases test the grammar, not the participant table.
//
// The units are part of the claim now: the same handicap is runs in baseball, goals in
// hockey and points in football, and a sentence that says the wrong one is describing a
// bet that does not exist.
{
  const bet = (o) => Object.assign(
    { sport: 'baseball', market: 'moneyline', period: 'full_game', selection: 'home',
      side: null, line: null, is_alternate: false }, o);
  const say = (o) => globalThis.__describeBet(bet(o), 'HOME', 'AWAY');
  const cases = [
    [{}, 'HOME win'],
    [{ selection: 'away' }, 'AWAY win'],
    [{ period: 'first_5_innings', selection: 'away' }, 'AWAY ahead after 5 innings'],
    [{ period: 'first_1_inning', selection: 'draw' }, 'Scores level after 1 inning'],
    // Half numbers settle outright; whole numbers can land on the handicap and refund.
    [{ market: 'spread', line: -1.5 }, 'HOME win by 2 or more'],
    [{ market: 'spread', line: -2.5 }, 'HOME win by 3 or more'],
    [{ market: 'spread', line: -1 }, 'HOME win by 2 or more (a 1-run win refunds)'],
    [{ market: 'spread', selection: 'away', line: 1.5 }, 'AWAY win, or lose by 1'],
    [{ market: 'spread', selection: 'away', line: 2.5 }, 'AWAY win, or lose by 2 or fewer'],
    [{ market: 'spread', selection: 'away', line: 1 }, 'AWAY win (a 1-run loss refunds)'],
    [{ market: 'spread', selection: 'away', line: 2 }, 'AWAY win, or lose by 1 (a 2-run loss refunds)'],
    [{ market: 'total', selection: 'over', line: 8.5 }, 'Both sides together score 9 runs or more'],
    [{ market: 'total', selection: 'under', line: 8.5 }, 'Both sides together score 8 runs or fewer'],
    [{ market: 'total', selection: 'over', line: 8 }, 'Both sides together score 9 runs or more (exactly 8 refunds)'],
    [{ market: 'total', selection: 'under', line: 8 }, 'Both sides together score 7 runs or fewer (exactly 8 refunds)'],
    [{ market: 'total', selection: 'over', line: 4.5, period: 'first_5_innings' },
      'Both sides together score 5 runs or more in the first 5 innings'],
    [{ market: 'team_total', selection: 'over', side: 'home', line: 4.5 }, 'HOME score 5 runs or more'],
    [{ market: 'team_total', selection: 'under', side: 'away', line: 3.5 }, 'AWAY score 3 runs or fewer'],
    [{ market: 'total', selection: 'over', line: 8.5, is_alternate: true },
      'Both sides together score 9 runs or more · extra line'],
    // Quarter lines split the stake across the two neighbouring half-lines, so
    // the middle outcome pays half.  There is no third case in the sentence
    // builder unless one is written: they used to fall into the whole-number
    // branch and assert an impossible refund ("a 0.25-goal win refunds"), and
    // worse, hide the half-win a draw pays.  254 rows on the captured slate.
    [{ sport: 'soccer', market: 'spread', line: -0.25 },
      'HOME win by 1 or more; a draw loses half the stake'],
    [{ sport: 'soccer', market: 'spread', selection: 'away', line: 0.25 },
      'AWAY win; a draw pays half'],
    [{ sport: 'soccer', market: 'spread', line: -0.75 },
      'HOME win by 2 or more; a 1-goal win pays half'],
    [{ sport: 'soccer', market: 'spread', line: -1.25 },
      'HOME win by 2 or more; a 1-goal win loses half the stake'],
    [{ sport: 'soccer', market: 'spread', selection: 'away', line: 0.75 },
      'AWAY win, or draw; a 1-goal loss loses half the stake'],
    [{ sport: 'soccer', market: 'spread', selection: 'away', line: 1.75 },
      'AWAY win, or lose by 1; a 2-goal loss loses half the stake'],
    [{ sport: 'soccer', market: 'total', selection: 'over', line: 3.25 },
      'Both sides together score 4 goals or more; exactly 3 loses half the stake'],
    [{ sport: 'soccer', market: 'total', selection: 'over', line: 2.75 },
      'Both sides together score 4 goals or more; exactly 3 pays half'],
    [{ sport: 'soccer', market: 'total', selection: 'under', line: 2.75 },
      'Both sides together score 2 goals or fewer; exactly 3 loses half the stake'],
    [{ sport: 'soccer', market: 'total', selection: 'under', line: 3.25 },
      'Both sides together score 2 goals or fewer; exactly 3 pays half'],
    // Every sport counts something different, and the sentence has to say which.
    [{ sport: 'hockey', market: 'total', selection: 'over', line: 5.5 },
      'Both sides together score 6 goals or more'],
    [{ sport: 'hockey', market: 'spread', line: -1 },
      'HOME win by 2 or more (a 1-goal win refunds)'],
    // Regulation-only hockey is three-way, because 60 minutes really can end level.
    [{ sport: 'hockey', period: 'regulation', selection: 'draw' }, 'Scores level in regulation'],
    [{ sport: 'soccer', selection: 'draw' }, 'Scores level — a draw'],
    [{ sport: 'soccer', market: 'total', selection: 'under', line: 2.5 },
      'Both sides together score 2 goals or fewer'],
    [{ sport: 'basketball', market: 'spread', line: -7.5 }, 'HOME win by 8 or more'],
    [{ sport: 'basketball', market: 'total', selection: 'over', line: 214.5 },
      'Both sides together score 215 points or more'],
    [{ sport: 'football', market: 'total', selection: 'over', line: 44.5 },
      'Both sides together score 45 points or more'],
    [{ sport: 'tennis', selection: 'away' }, 'AWAY win'],
    // A database written before the vocabulary was made sport-neutral must still
    // describe its bets correctly, not fall through to the totals branch.
    [{ market: 'run_line', line: -1.5 }, 'HOME win by 2 or more'],
    [{ market: 'total_runs', selection: 'under', line: 8.5 }, 'Both sides together score 8 runs or fewer'],
    [{ market: 'team_total_runs', selection: 'over', side: 'away', line: 4.5 }, 'AWAY score 5 runs or more'],
  ];
  const wrong = cases.filter(([input, expected]) => say(input) !== expected)
    .map(([input, expected]) => `  got "${say(input)}"\n  want "${expected}"`);
  if (wrong.length) {
    console.error('WRONG PLAIN-ENGLISH DESCRIPTIONS:\n' + wrong.join('\n'));
    process.exit(1);
  }

  // And the participant table must actually shorten a resolved identity to a name a
  // person would say, rather than leaving the key on screen.
  const keys = Object.keys(globalThis.__participants || {});
  if (keys.length) {
    const sample = keys[0];
    const short = globalThis.__participants[sample].short;
    if (globalThis.__nick(sample) !== short) {
      console.error(`PARTICIPANT NOT SHORTENED: ${sample} -> ${globalThis.__nick(sample)}, want ${short}`);
      process.exit(1);
    }
    console.log(`plain English ok (${cases.length} cases); ${keys.length} participants mapped, e.g. "${sample}" -> "${short}"`);
  } else {
    console.error('NO PARTICIPANTS IN PAYLOAD: every bet would be described with a raw key');
    process.exit(1);
  }
}

// Eyeball the actual markup for a few regions, as plain text.
const strip = (h) => h.replace(/<[^>]+>/g, ' ').replace(/&rarr;/g, '->').replace(/&mdash;/g, '-')
  .replace(/&[a-z#0-9]+;/g, ' ').replace(/\s+/g, ' ').trim();
for (const id of (process.env.DUMP || '').split(',').filter(Boolean)) {
  const h = nodes.get(id)?.innerHTML || nodes.get(id)?.textContent || '';
  console.log(`\n== ${id} ==\n${strip(h).slice(0, +(process.env.DUMPLEN || 900))}`);
}

// The wide tables carry a band row above their headings.  If its colspans do not add
// up to the number of columns, every cell below shifts under the wrong band label —
// which reads as a mislabelled column rather than as a broken table.
{
  const problems = [];
  const skipped = [];
  for (const id of ['odds-table', 'event-detail', 'coverage', 'move-table', 'raws', 'matrix',
                    'sports-grid', 'leagues-grid']) {
    const node = nodes.get(id);
    // An empty result set is a text-only state with no header at all to check.
    if (node?.dataset?.wasTable) { skipped.push(id); continue; }
    const markup = node?.innerHTML || '';
    const rows = markup.match(/<tr[^>]*>[\s\S]*?<\/tr>/g) || [];
    const bandRow = rows.find((r) => r.includes('class="grouped"'));
    if (!bandRow) { problems.push(`${id}: no band row`); continue; }
    const spans = [...bandRow.matchAll(/colspan="(\d+)"/g)].reduce((a, m) => a + +m[1], 0);
    const headings = (rows[1]?.match(/<th/g) || []).length;
    if (spans !== headings) problems.push(`${id}: bands cover ${spans} of ${headings} columns`);
  }
  console.log(problems.length ? 'MISALIGNED COLUMN BANDS: ' + problems.join('; ')
    : 'column bands align with their headings'
      + (skipped.length ? ` (empty, not checked: ${skipped.join(', ')})` : ''));
  if (problems.length) process.exit(1);
}

// Generated SVG must be well-formed; an unclosed tag swallows the rest of the chart.
{
  const svg = (nodes.get('runs-chart')?.innerHTML || '') + (nodes.get('move-table')?.innerHTML || '');
  const problems = [];
  for (const tag of ['rect', 'circle', 'polyline', 'polygon', 'line', 'text', 'svg', 'title']) {
    const opens = (svg.match(new RegExp(`<${tag}[\\s>]`, 'g')) || []).length;
    const closes = (svg.match(new RegExp(`</${tag}>`, 'g')) || []).length;
    const selfClosed = (svg.match(new RegExp(`<${tag}\\b[^>]*/>`, 'g')) || []).length;
    if (opens !== closes + selfClosed) problems.push(`${tag}: ${opens} open vs ${closes} closed + ${selfClosed} self-closed`);
  }
  console.log(problems.length ? 'MALFORMED SVG: ' + problems.join('; ') : `svg well-formed (${svg.length} chars of chart markup)`);
  if (problems.length) process.exit(1);
}

// A book's margin can only be read off a market it priced completely. Summing two
// legs of a three-way — a suspended draw, or an exchange with no resting draw offer
// — makes a healthy market look like a book pricing itself to lose (0.757), which
// the page rendered as "impossible prices: 1" beside "problems found: 0", and as
// "smarkets keeps -23.3%" on the bet panel. Validation refuses to sum such a market
// for exactly this reason; the page now reads the same settlement table.
{
  const sides = globalThis.__moneylineSides;
  const cases = [
    ['soccer', 'full_game', 3],
    ['hockey', 'regulation', 3],
    ['hockey', 'full_game', 2],
    ['baseball', 'full_game', 2],
    ['baseball', 'first_1_inning', 3],
    ['football', 'full_game', 2],   // can tie, but the draw is not priced
    ['tennis', 'full_game', 2],
  ];
  const problems = [];
  for (const [sport, period, want] of cases) {
    const got = sides(sport, period);
    if (got !== want) problems.push(`${sport}/${period}: ${got} sides, want ${want}`);
  }
  console.log(problems.length ? 'MONEYLINE SHAPE WRONG: ' + problems.join('; ')
    : `a complete moneyline is sized from the settlement table (${cases.length} windows)`);
  if (problems.length) process.exit(1);
}

// ...and no venue on the real page is reported keeping an *implausible* cut. A
// genuinely crossed two-way book shows a small negative cut and that is honest;
// summing two legs of a three-way produced "keeps -23.3%", which is not a market
// state any venue survives.
{
  const text = [...nodes.values()].map((n) => (n.innerHTML || '') + (n.textContent || '')).join(' ');
  const cuts = [...text.matchAll(/keeps (-?\d+(?:\.\d+)?)%/g)].map((m) => Number(m[1]));
  const absurd = cuts.filter((c) => c < -5);
  console.log(absurd.length ? 'IMPLAUSIBLE VENUE CUT RENDERED: ' + absurd.join(', ') + '%'
    : `no venue is reported keeping an implausible cut (${cuts.length} cuts shown)`);
  if (absurd.length) process.exit(1);
}

// "N bets held exactly the same price" is the page's staleness signal, so it must
// count only bets that were *seen more than once*. Counting `series.size - moved`
// folded every bet observed in a single collection into the held-steady claim, and
// that number grows by exactly the rows of a venue that went missing.
{
  const series = (...lengths) => new Map(lengths.map((n, i) => [i, { values: Array(n).fill(1.5) }]));
  const cases = [
    // [series lengths, moved, expected {comparable, once, stable}]
    [[2, 2, 2], 0, { comparable: 3, once: 0, stable: 3 }],
    [[2, 2, 1], 0, { comparable: 2, once: 1, stable: 2 }],
    [[2, 2, 1], 1, { comparable: 2, once: 1, stable: 1 }],
    [[1, 1, 1], 0, { comparable: 0, once: 3, stable: 0 }],
    // the reviewer's measured case: 4,452 seen twice, 2,922 seen once, none moved
    [[...Array(4452).fill(2), ...Array(2922).fill(1)], 0,
      { comparable: 4452, once: 2922, stable: 4452 }],
    [[], 0, { comparable: 0, once: 0, stable: 0 }],
  ];
  const problems = [];
  for (const [lengths, moved, want] of cases) {
    const got = globalThis.__movementCounts(series(...lengths), moved);
    for (const key of ['comparable', 'once', 'stable']) {
      if (got[key] !== want[key]) {
        problems.push(`${lengths.length} series/${moved} moved: ${key} ${got[key]}, want ${want[key]}`);
      }
    }
  }
  console.log(problems.length ? 'MOVEMENT COUNTS WRONG: ' + problems.join('; ')
    : `movement counts hold across ${cases.length} shapes`);
  if (problems.length) process.exit(1);
}

// A run in the picker whose prices are *not* embedded is not a run that stored
// nothing, and the page has a different sentence for each. ``detailLoaded`` is
// what chooses between them, and it could be replaced outright with `return
// true` while 801 tests and sixteen source greps stayed green — because the one
// harness that executes this script rendered a payload with every run embedded,
// so the false branch was never taken and the function never had to answer
// anything.
//
// Only meaningful on a page built with a --quote-runs smaller than the number of
// stored runs; on a full page it says so and checks nothing.
{
  const data = globalThis.__DATA;
  const detail = new Set(data.detail_runs || []);
  const listed = (data.runs || []).map((r) => r.id);
  const missing = listed.filter((id) => !detail.has(id));
  if (!missing.length) {
    console.log(`every listed run carries detail (${listed.length}); truncation not exercised`);
  } else {
    const problems = [];
    for (const id of listed) {
      const want = detail.has(id);
      if (globalThis.__detailLoaded(id) !== want) {
        problems.push(`run ${id}: detailLoaded ${!want}, want ${want}`);
      }
    }
    // ...and the page has to *say* it, not just know it. The sentences are all
    // scoped to the run being viewed, so this is only reachable by doing what a
    // reader does: pick the older collection out of the picker.
    const pick = nodes.get('run-pick');
    pick.value = String(missing[0]);
    pick.dispatch('change');
    const text = [...nodes.entries()].filter(([id]) => id !== 'report-data')
      .map(([, n]) => (n.innerHTML || '') + (n.textContent || '')).join(' ');
    const said = text.includes('Rebuild with more --quote-runs');
    // A run-switch is a re-render, and a re-render is where a payload field that
    // exists only for embedded runs turns into NaN on screen.
    const stray = ['NaN', 'undefined', '[object Object]'].filter((s) => text.includes(s));
    if (stray.length) {
      console.error('LEAKED PLACEHOLDER VALUES AFTER SWITCHING RUN:', stray.join(', '));
      process.exit(1);
    }
    console.log(problems.length ? 'DETAIL-LOADED WRONG: ' + problems.join('; ')
      : `truncation is distinguished (${missing.length} of ${listed.length} runs carry no detail)`
        + (said ? ' and named on screen' : ''));
    if (problems.length) process.exit(1);
    if (!said) {
      console.error('TRUNCATED RUNS NOT NAMED: the page claims data it never loaded');
      process.exit(1);
    }
  }
}

// Two rows the store holds at once must not share a price series. ``dedup_key``
// is what decides that in Python, and the movement key has to separate whatever
// it separates — otherwise two prices that exist *simultaneously* are drawn as a
// change from one to the other, complete with a sparkline, and the table sorts
// by swing so the widest of them sorts to the top.
//
// ``is_alternate`` was the field left out. A book offers the same number on its
// main market and on an alternate-line market at different prices; both rows are
// real, both are stored, and they were being differenced against each other.
{
  const COL = globalThis.__COL;
  const rows = (globalThis.__DATA.quotes && globalThis.__DATA.quotes.rows) || [];
  const problems = [];
  if (!rows.length) {
    problems.push('no rows in the payload to build a key from');
  } else {
    const anchors = new Map();
    const main = rows[0].slice();
    main[COL.is_alternate] = 0;
    const alt = main.slice();
    alt[COL.is_alternate] = 1;
    const twin = main.slice();
    if (globalThis.__movementKey(main, anchors) === globalThis.__movementKey(alt, anchors)) {
      problems.push('a main and an alternate row share one series');
    }
    if (globalThis.__movementKey(main, anchors) !== globalThis.__movementKey(twin, anchors)) {
      problems.push('two identical rows do not share a series');
    }
    // Every other field of the identity, one at a time, so the key cannot be
    // "everything" or "one thing that happens to include is_alternate".
    //
    // The interned columns hold an *index* into the payload's string table, not
    // a string, so they are perturbed by index — writing a string there reads
    // back as undefined, which is what a null side already reads as, and the
    // check would pass itself.
    const interned = ['source', 'event_key', 'market', 'period', 'side', 'selection'];
    for (const field of interned) {
      const other = main.slice();
      other[COL[field]] = main[COL[field]] === 0 ? 1 : 0;
      if (globalThis.__movementKey(main, anchors) === globalThis.__movementKey(other, anchors)) {
        problems.push(`${field} is not part of the series identity`);
      }
    }
    const lined = main.slice();
    lined[COL.line] = main[COL.line] === 1.5 ? 2.5 : 1.5;
    if (globalThis.__movementKey(main, anchors) === globalThis.__movementKey(lined, anchors)) {
      problems.push('line is not part of the series identity');
    }
  }
  console.log(problems.length ? 'MOVEMENT SERIES KEY WRONG: ' + problems.join('; ')
    : 'a price series is identified the way a stored row is');
  if (problems.length) process.exit(1);
}

// Where a book has two rows under one bet key, both places that show "this
// book's price" have to pick the same one. They did not: the card used the
// better price and the per-collection history column used whichever the source
// order put last, so the two could name different rows of the same book in the
// same collection. One rule now, and it cannot depend on arrival order.
{
  const COL = globalThis.__COL;
  const rows = (globalThis.__DATA.quotes && globalThis.__DATA.quotes.rows) || [];
  const problems = [];
  if (rows.length) {
    // Both rows in the same status, so the comparison is decided on price
    // alone; that a suspended row loses to a live one whatever the price is
    // ``_betterPrice``'s own rule and is not what is being asked here.
    const priced = (value) => {
      const row = rows[0].slice();
      row[COL.decimal_odds] = value;
      if (COL.net_decimal_odds !== undefined) row[COL.net_decimal_odds] = value;
      return row;
    };
    const worse = priced(1.50);
    const better = priced(2.50);
    for (const order of [[worse, better], [better, worse]]) {
      const map = new Map();
      for (const r of order) globalThis.__keepBetter(map, 'k', r);
      if (map.get('k')[COL.decimal_odds] !== 2.50) {
        problems.push(`arrival order decided the winner (${order.map((r) => r[COL.decimal_odds])})`);
      }
    }
  } else {
    problems.push('no rows in the payload');
  }
  console.log(problems.length ? 'BEST-ROW RULE WRONG: ' + problems.join('; ')
    : 'the row shown for a book does not depend on arrival order');
  if (problems.length) process.exit(1);
}

// Every quarter line the page can describe, checked against what ``src/arb.py``
// says the middle outcome actually pays.
//
// The sentence and the detector were describing the same stored row
// differently: both halves of every quarter line were told the landing score
// "pays half", while ``arb.py`` gives one side HALF_WIN and the other
// HALF_LOSE — never both — so on 127 of the 254 quarter-line rows in the
// committed captures the page promised a payout to the reader who was losing
// half the stake there. Two such sentences sat next to each other on one panel.
//
// The expectations are generated from ``settlement_outcomes`` in Python and
// handed in as JSON, so this cannot be kept green by editing a literal here:
// the oracle is the code that sizes the stakes. Skipped when no file is passed,
// which is how the page-only invocations still work.
if (process.argv[3]) {
  const cases = JSON.parse(readFileSync(process.argv[3], 'utf8'));
  const problems = [];
  for (const c of cases) {
    const said = globalThis.__describeBet({
      sport: c.sport, market: c.market, period: 'full_game', selection: c.selection,
      side: null, line: c.line, is_alternate: false,
    }, 'HOME', 'AWAY');
    const verb = c.half_wins ? 'pays half' : 'loses half the stake';
    const other = c.half_wins ? 'loses half the stake' : 'pays half';
    if (!said.includes(verb) || said.includes(other)) {
      problems.push(`${c.sport} ${c.market} ${c.selection} ${c.line}: "${said}" should say "${verb}"`);
      continue;
    }
    // ...and it has to name the score it lands on, which is always a whole
    // number. "a 0.5-goal loss" was being printed for +0.75 lines. A landing of
    // zero is a level game, and "a draw" is the word for it.
    const names = c.landing === 0 ? /\bdraw\b/ : new RegExp(`\\b${c.landing}\\b`);
    if (!names.test(said)) {
      problems.push(`${c.sport} ${c.market} ${c.selection} ${c.line}: "${said}" never names ${c.landing}`);
    }
    if (/\d+\.\d/.test(said)) {
      problems.push(`${c.sport} ${c.market} ${c.selection} ${c.line}: "${said}" names a fractional score`);
    }
  }
  console.log(problems.length ? 'QUARTER LINES DESCRIBED WRONG:\n  ' + problems.join('\n  ')
    : `quarter lines agree with the detector (${cases.length} cases)`);
  if (problems.length) process.exit(1);
}

// A market with a suspended leg is exactly as unsummable as one with a missing
// leg — a price that is showing is not being offered — and ``validation.py``
// says so in one line: ``len(active) == len(rows)``. The page had that rule in
// one of the two call sites rather than in the function, so the quality strip
// correctly excluded a three-way with a suspended draw while the bet panel one
// click away read "keeps −17.9%" off the same market.
{
  const COL = globalThis.__COL;
  const rows = (globalThis.__DATA.quotes && globalThis.__DATA.quotes.rows) || [];
  const problems = [];
  if (!rows.length) {
    problems.push('no rows in the payload');
  } else {
    const build = (status) => {
      const row = rows[0].slice();
      row[COL.market] = globalThis.__DATA.strings.indexOf('spread');
      row[COL.status] = globalThis.__DATA.strings.indexOf(status);
      return row;
    };
    if (!globalThis.__sumsToAMargin([build('active'), build('active')])) {
      problems.push('two active rows are not summable');
    }
    if (globalThis.__sumsToAMargin([build('active'), build('suspended')])) {
      problems.push('a suspended leg was summed');
    }
  }
  console.log(problems.length ? 'MARGIN COMPLETENESS WRONG: ' + problems.join('; ')
    : 'a market with a suspended leg is not summed');
  if (problems.length) process.exit(1);
}

// The American odds shown beside a net return must be the net ones. Every row
// from a commission venue disagreed with itself: matchbook 2.34 rendered
// "+134 · $100 returns $231", and +134 pays $234.
{
  const text = [...nodes.entries()].filter(([id]) => id !== 'report-data')
    .map(([, n]) => (n.innerHTML || '') + (n.textContent || '')).join(' ');
  const problems = [];
  for (const m of text.matchAll(/([+\u2212]\d+) \u00b7 \$100 returns \$(\d+)/g)) {
    const american = Number(m[1].replace('\u2212', '-'));
    const implied = american > 0 ? 100 + american : 100 + 10000 / -american;
    if (Math.abs(implied - Number(m[2])) > 1) {
      problems.push(`${m[1]} implies $${Math.round(implied)}, shown beside $${m[2]}`);
    }
  }
  console.log(problems.length ? 'AMERICAN ODDS DISAGREE WITH THE RETURN: ' + problems.join('; ')
    : 'the American odds shown match the return shown');
  if (problems.length) process.exit(1);
}


// Two halves of a doubleheader must not share a price series. The event key's
// ordinal is a within-run rank, so once game one has started and dropped, game
// two inherits the bare key — and without bucketing on commence_time the page
// drew that join as a "+92%" price movement with a sparkline. ``fixtureBucket``
// could be replaced with ``return 0`` and every prior harness check stayed green.
{
  const COL = globalThis.__COL;
  const data = globalThis.__DATA;
  const S = data.strings;
  const rows = (data.quotes && data.quotes.rows) || [];
  const problems = [];
  if (!rows.length) {
    problems.push('no rows in the payload');
  } else {
    const league = 'MLB';
    // Push two starts more than one MLB window (90 minutes) apart, and one
    // within it, so the check cannot be satisfied by "always different" or
    // "always the same".
    const early = S.length; S.push('2026-07-28T18:00:00+00:00');
    const later = S.length; S.push('2026-07-28T22:00:00+00:00');
    const near = S.length; S.push('2026-07-28T18:30:00+00:00');
    const base = rows[0].slice();
    base[COL.league] = S.indexOf(league) >= 0 ? S.indexOf(league) : (S.push(league) - 1);
    base[COL.event_key] = S.indexOf('DH-A@DH-B:2026-07-28') >= 0
      ? S.indexOf('DH-A@DH-B:2026-07-28')
      : (S.push('DH-A@DH-B:2026-07-28') - 1);
    const a = base.slice(); a[COL.commence_time] = early;
    const b = base.slice(); b[COL.commence_time] = later;
    const c = base.slice(); c[COL.commence_time] = near;
    const anchors = globalThis.__fixtureAnchorsFor([a, b, c]);
    const window = globalThis.__fixtureWindowMs(league);
    if (!(window > 0)) problems.push(`MLB window is ${window}`);
    if (globalThis.__fixtureBucket(a, anchors) === globalThis.__fixtureBucket(b, anchors)) {
      problems.push('starts four hours apart share a bucket');
    }
    if (globalThis.__fixtureBucket(a, anchors) !== globalThis.__fixtureBucket(c, anchors)) {
      problems.push('starts thirty minutes apart do not share a bucket');
    }
    // Exactly one window later must still share the series — betRows keeps it
    // (``> window`` is exclusive) and floor(delta/window) used to split it.
    const atWindow = S.length;
    const baseWhen = Date.parse(S[early]);
    S.push(new Date(baseWhen + window).toISOString());
    const d = base.slice(); d[COL.commence_time] = atWindow;
    const anchors2 = globalThis.__fixtureAnchorsFor([a, d]);
    if (globalThis.__fixtureBucket(a, anchors2) !== globalThis.__fixtureBucket(d, anchors2)) {
      problems.push('a start exactly one window later was split from its anchor');
    }
    if (globalThis.__movementKey(a, anchors) === globalThis.__movementKey(b, anchors)) {
      problems.push('doubleheader halves share a movement series');
    }
  }
  console.log(problems.length ? 'DOUBLEHEADER BUCKET WRONG: ' + problems.join('; ')
    : 'doubleheader halves do not share a price series');
  if (problems.length) process.exit(1);
}

// A two-leg moneyline in a draw-priced window is not a complete market, and the
// page must not read a margin off it. ``return rows.length >= 2`` — the body
// this function had before the settlement table was consulted — still produces
// a byte-identical page against a fixture whose only moneylines are two-way,
// which is why the four Python greps that "pinned" it never caught a revert.
{
  const COL = globalThis.__COL;
  const data = globalThis.__DATA;
  const S = data.strings;
  const rows = (data.quotes && data.quotes.rows) || [];
  const problems = [];
  const idx = (value) => {
    let at = S.indexOf(value);
    // A thin payload (one moneyline, no F5) used to throw here and abort every
    // later pin — including the overview-card checks this harness also owns.
    if (at < 0) { at = S.length; S.push(value); }
    return at;
  };
  if (!rows.length) {
    problems.push('no rows in the payload');
  } else {
    const row = (selection) => {
      const r = rows[0].slice();
      r[COL.market] = idx('moneyline');
      r[COL.period] = idx('first_5_innings');
      r[COL.sport] = idx('baseball');
      r[COL.selection] = idx(selection);
      r[COL.status] = idx('active');
      r[COL.side] = null;
      return r;
    };
    const two = [row('home'), row('away')];
    const three = [row('home'), row('away'), row('draw')];
    if (globalThis.__sumsToAMargin(two)) {
      problems.push('a two-leg moneyline in a three-way window was summed');
    }
    if (!globalThis.__sumsToAMargin(three)) {
      problems.push('a complete three-way moneyline was refused');
    }
    if (globalThis.__moneylineSides('baseball', 'first_5_innings') !== 3) {
      problems.push('first_5_innings is not sized as three-way');
    }
  }
  console.log(problems.length ? 'MONEYLINE SHAPE NOT ENFORCED: ' + problems.join('; ')
    : 'a two-leg moneyline in a three-way window is not summed');
  if (problems.length) process.exit(1);
}

// "Separate bets — each with every side priced" must count only the groups that
// actually have every side priced. Counting ``marketGroups.size`` made the
// overview claim a larger number than the Checks panel's "bets fully priced"
// on the same page, for the same collection.
{
  const COL = globalThis.__COL;
  const data = globalThis.__DATA;
  const S = data.strings;
  const pick = nodes.get('run-pick');
  const currentId = Number(pick && pick.value);
  // Only meaningful on a run whose prices are embedded; after the truncation
  // check switches the picker to an unloaded run the strip reads "—" and a
  // loose digit match would pick up the next stat instead.
  if (!globalThis.__detailLoaded(currentId)) {
    console.log('separate-bets check skipped; current run carries no detail');
  } else {
    const rows = ((data.quotes && data.quotes.rows) || [])
      .filter((r) => r[COL.run_id] === currentId)
      .filter((r) => {
        const i = r[COL.status];
        return i !== null && i !== undefined && i >= 0 && data.strings[i] === 'active';
      });
    const groups = globalThis.__marketGroups(rows);
    let complete = 0;
    for (const group of groups.values()) if (globalThis.__sumsToAMargin(group)) complete += 1;
    const strip = nodes.get('stat-strip')?.innerHTML || '';
    const match = strip.match(/<span>separate bets<\/span><b>([\d,]+)<\/b>/i);
    const problems = [];
    if (!match) {
      problems.push('overview does not state a separate-bets count');
    } else {
      const shown = Number(match[1].replace(/,/g, ''));
      if (shown !== complete) {
        problems.push(`overview says ${shown}, complete groups are ${complete} (of ${groups.size})`);
      }
    }
    // A live MLB slate can have every group complete, which would make
    // ``shown === groups.size`` pass even if the overview counted every
    // group.  Prove the filter here with a synthetic pair that does not
    // depend on what the page happened to collect.
    const idx = (value) => {
      let at = S.indexOf(value);
      if (at < 0) { at = S.length; S.push(value); }
      return at;
    };
    const synth = (market, selection, side) => {
      const r = rows[0].slice();
      r[COL.market] = idx(market);
      r[COL.period] = idx('full_game');
      r[COL.sport] = idx('baseball');
      r[COL.selection] = idx(selection);
      r[COL.side] = side === null ? null : idx(side);
      r[COL.status] = idx('active');
      r[COL.line] = market === 'total' ? 8.5 : null;
      return r;
    };
    const bothWays = [synth('moneyline', 'home', null), synth('moneyline', 'away', null)];
    const oneSided = [synth('total', 'over', 'over')];
    if (!globalThis.__sumsToAMargin(bothWays)) {
      problems.push('a complete two-way moneyline was refused by the filter');
    }
    if (globalThis.__sumsToAMargin(oneSided)) {
      problems.push('a one-sided total was accepted as fully priced');
    }
    console.log(problems.length ? 'SEPARATE-BETS COUNT WRONG: ' + problems.join('; ')
      : `separate bets counts only complete markets (${complete} of ${groups.size})`);
    if (problems.length) process.exit(1);
  }
}

// "refused N of M" must use the distinct-scope count, not the message list.
// Two messages under one scope (SX Bet's halves) used to print "2 of 1".
{
  const data = globalThis.__DATA;
  const run = (data.runs || []).find((r) => (r.sources || []).some((h) => h.key))
    || (data.runs || [])[0];
  const health = run && (run.sources || []).find((h) => h.key);
  const problems = [];
  if (!health || typeof globalThis.__renderBook !== 'function') {
    problems.push('no health card or renderBook export to pin against');
  } else {
    // The truncation check above may have left the picker on a run with no
    // embedded detail.  Point it at the run whose health we are about to
    // rewrite, or renderBook reads a different card and the pin is inert.
    const pick = nodes.get('run-pick');
    if (pick) {
      pick.value = String(run.id);
      pick.dispatch('change');
    }
    const original = {
      scopes_failed: health.scopes_failed,
      scopes_refused: health.scopes_refused,
      scopes_requested: health.scopes_requested,
    };
    health.scopes_failed = 0;
    health.scopes_refused = ['mlb: metadata half', 'mlb: order-book half'];
    health.scopes_requested = 1;
    globalThis.__renderBook(health.key);
    const html = nodes.get('book-stats')?.innerHTML || '';
    const match = html.match(/<span>refused<\/span><b>([^<]*)<\/b>/i);
    if (!match) {
      problems.push('book card does not state a refused count');
    } else if (match[1] !== '1 of 1') {
      // Additive migration backfills scopes_failed=0 onto rows that still
      // name refusals.  Hiding them (``??`` alone → 0) or counting messages
      // (``||`` → 2) are both wrong; the distinct scope name is one.
      problems.push(`book card says refused ${match[1]}, want 1 of 1`);
    }
    const state = nodes.get('book-state')?.innerHTML || '';
    if (/responded normally/i.test(state)) {
      problems.push('book drill-down still shows responded normally for a refusal');
    }
    if (!/refused 1 of 1/i.test(state)) {
      problems.push(`book drill-down state missing refusal pill (got: ${state.slice(0, 120)})`);
    }
    // Colon-qualified scopes (Pinnacle ``MLB:246``) must not collapse.
    health.scopes_failed = 0;
    health.scopes_refused = ['MLB:246: HTTP 403: blocked', 'MLB:247: HTTP 403: blocked'];
    health.scopes_requested = 3;
    globalThis.__renderBook(health.key);
    const html2 = nodes.get('book-stats')?.innerHTML || '';
    const match2 = html2.match(/<span>refused<\/span><b>([^<]*)<\/b>/i);
    if (!match2 || match2[1] !== '2 of 3') {
      problems.push(`colon-qualified scopes collapsed (got: ${match2 && match2[1]})`);
    }
    // Drill-down truncation pill — overview pins cut-short, but renderBook's
    // truncatedNow branch was unpinned and could revert to green silently.
    health.scopes_failed = 0;
    health.scopes_refused = [];
    health.scopes_requested = 1;
    health.scopes_truncated = ['mlb: beyond the page cap'];
    globalThis.__renderBook(health.key);
    const truncState = nodes.get('book-state')?.innerHTML || '';
    if (/responded normally/i.test(truncState)) {
      problems.push('book drill-down shows responded normally for a truncation');
    }
    if (!/cut short/i.test(truncState)) {
      problems.push(`book drill-down state missing truncation pill (got: ${truncState.slice(0, 120)})`);
    }
    health.scopes_failed = original.scopes_failed;
    health.scopes_refused = original.scopes_refused;
    health.scopes_requested = original.scopes_requested;
  }
  console.log(problems.length ? 'REFUSED COUNT WRONG: ' + problems.join('; ')
    : 'refused count uses the distinct-scope numerator');
  if (problems.length) process.exit(1);
}

// Overview source cards must use the same distinct-count / truncation rules as
// the drill-down. Counting ``scopes_refused.length`` printed "refused 2 of 1",
// and a cap-only source stayed green as "responded normally".
{
  const data = globalThis.__DATA;
  const run = (data.runs || []).find((r) => (r.sources || []).length)
    || (data.runs || [])[0];
  const catalog = data.sources || [];
  const problems = [];
  if (!run || !catalog.length || typeof globalThis.__renderSources !== 'function') {
    problems.push('no sources overview to pin against');
  } else {
    const pick = nodes.get('run-pick');
    if (pick) {
      pick.value = String(run.id);
      pick.dispatch('change');
    }
    // Prefer a catalog key that already has a health row on this run.
    const paired = catalog.find((s) => (run.sources || []).some((h) => h.key === s.key));
    const src = paired || catalog[0];
    let health = (run.sources || []).find((h) => h.key === src.key);
    if (!health) {
      health = {
        key: src.key, ok: true, quote_count: 1, event_count: 1,
        request_count: 1, raw_bytes: 1, latency_ms: 1, unchanged_payloads: 0,
        scopes_requested: 1, scopes_failed: 0, scopes_refused: [],
        scopes_truncated: [],
      };
      run.sources.push(health);
    }
    const original = {
      scopes_failed: health.scopes_failed,
      scopes_refused: health.scopes_refused,
      scopes_requested: health.scopes_requested,
      scopes_truncated: health.scopes_truncated,
      ok: health.ok,
    };

    health.ok = true;
    health.scopes_failed = 1;
    health.scopes_refused = ['mlb: metadata half', 'mlb: order-book half'];
    health.scopes_requested = 1;
    health.scopes_truncated = [];
    globalThis.__renderSources();
    let cards = nodes.get('source-cards')?.innerHTML || '';
    if (!cards.includes('refused 1 of 1')) {
      problems.push(`overview missing distinct refusal (got: ${cards.slice(0, 200)})`);
    }
    if (cards.includes('refused 2 of 1')) {
      problems.push('overview still counts refusal messages');
    }

    // Additive migration backfills scopes_failed=0 onto rows that still name
    // refusals.  Hiding them (``??`` alone) or counting messages (``||``) are
    // both wrong; two halves of one scope are still one refusal.
    health.scopes_failed = 0;
    health.scopes_refused = ['mlb: metadata half', 'mlb: order-book half'];
    health.scopes_requested = 1;
    health.scopes_truncated = [];
    globalThis.__renderSources();
    cards = nodes.get('source-cards')?.innerHTML || '';
    if (!cards.includes('refused 1 of 1')) {
      problems.push(`overview legacy refusal wrong (got: ${cards.slice(0, 200)})`);
    }
    if (cards.includes('refused 2 of 1')) {
      problems.push('overview zero-refusal fell back to the message count');
    }

    health.scopes_failed = 0;
    health.scopes_refused = [];
    health.scopes_truncated = ['mlb: beyond the page cap'];
    globalThis.__renderSources();
    cards = nodes.get('source-cards')?.innerHTML || '';
    if (!/cut short/i.test(cards)) {
      problems.push('overview does not name a truncated scope');
    }

    health.scopes_failed = original.scopes_failed;
    health.scopes_refused = original.scopes_refused;
    health.scopes_requested = original.scopes_requested;
    health.scopes_truncated = original.scopes_truncated;
    health.ok = original.ok;
  }
  console.log(problems.length
    ? 'OVERVIEW CARDS WRONG: ' + problems.join('; ')
    : 'overview cards use distinct refusals; overview cards name truncated scopes');
  if (problems.length) process.exit(1);
}
