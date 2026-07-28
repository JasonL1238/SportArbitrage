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
    addEventListener() {},
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
