// Exercises the dashboard's hash routing: every panel, plus a real fixture, book and
// bet key pulled out of the page's own payload.  The DOM stub is the same shape as
// tests/dashboard_smoke.mjs, with a settable location.hash added.
import { readFileSync } from 'node:fs';

const page = readFileSync(process.argv[2], 'utf8');
const payload = page.match(/<script type="application\/json" id="report-data">([\s\S]*?)<\/script>/)[1];
const script = page.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];

const nodes = new Map();
function make(id) {
  return {
    id, tagName: 'DIV', tabIndex: 0, value: '', textContent: '', _html: '',
    dataset: {}, style: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); } else if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; },
    setAttribute() {}, getAttribute(n) { return n === 'href' ? '#overview' : null; },
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    dispatch(type) { for (const fn of this._listeners[type] || []) fn(); },
    querySelectorAll() { return []; }, querySelector() { return null; },
    replaceWith(other) { nodes.set(other.id || id, other); },
    insertAdjacentHTML() {}, appendChild() {},
  };
}
globalThis.document = {
  documentElement: make('root'),
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, make(id)); return nodes.get(id); },
  querySelector(sel) { return this.getElementById(sel.replace(/[^a-z0-9_-]/gi, '')); },
  querySelectorAll() { return []; },
  createElement(tag) { const n = make(''); n.tagName = tag.toUpperCase(); return n; },
};
document.getElementById('report-data').textContent = payload;
globalThis.IntersectionObserver = class { observe() {} };

// A location whose hash can be set, and a window that forwards hashchange.
let handler = null;
globalThis.location = { hash: '' };
globalThis.window = {
  addEventListener(name, fn) { if (name === 'hashchange') handler = fn; },
  scrollTo() {},
  get location() { return globalThis.location; },
};

const data = JSON.parse(payload);
new Function(script + `
  globalThis.__applyRoute = applyRoute;
  globalThis.__PANELS = PANELS;
  globalThis.__betKeyOf = betKeyOf;
  globalThis.__COL = COL;
  globalThis.__here = () => here;
`)();

const visit = (hash) => {
  globalThis.location.hash = hash;
  if (handler) handler(); else globalThis.__applyRoute();
};
const on = () => Object.keys(globalThis.__PANELS).filter((id) => nodes.get(id)?.classList.contains('on'));
const text = (id) => ((nodes.get(id)?.innerHTML || '') + (nodes.get(id)?.textContent || '')).trim();

const problems = [];

// Every panel in the routing table must become the one visible panel.
for (const name of Object.keys(globalThis.__PANELS)) {
  visit('#' + name);
  const showing = on();
  if (showing.length !== 1 || showing[0] !== name) {
    problems.push(`#${name} showed [${showing.join(', ')}]`);
  }
  if (!text('crumbs')) problems.push(`#${name} left the trail empty`);
}

// An unknown hash must land somewhere real rather than on a blank page.
visit('#no-such-panel');
if (on().join() !== 'screen') problems.push(`unknown hash showed [${on().join(', ')}]`);

// Real keys out of the payload: a fixture, a book, and a bet.
const COL = globalThis.__COL;
const row = data.quotes.rows[0];
const fixtureKey = data.strings[row[COL.event_key]];
const bookKey = data.runs[0].sources[0].key;
const betKey = globalThis.__betKeyOf(row);
// Point the page at the run that owns the sample row.  The newest scrape may
// be thin (summary only), and fixture drill-down only sees the selected run.
const pick = nodes.get('run-pick');
if (pick) {
  pick.value = String(row[COL.run_id]);
  pick.dispatch('change');
}

visit('#fixture/' + encodeURIComponent(fixtureKey));
if (!text('event-detail')) problems.push('fixture panel rendered nothing');
if (!text('event-title').trim() || text('event-title').startsWith('Pick a')) {
  problems.push(`fixture title not resolved: ${JSON.stringify(text('event-title'))}`);
}

visit('#book/' + encodeURIComponent(bookKey));
if (!text('book-stats')) problems.push('book panel rendered nothing');
if (text('book-title').startsWith('Pick a')) problems.push('book title not resolved');

visit('#bet/' + encodeURIComponent(betKey));
for (const id of ['bet-books', 'bet-sides', 'bet-history']) {
  if (!text(id)) problems.push(`bet panel left ${id} empty`);
}
if (text('bet-title').startsWith('Pick a')) problems.push('bet title not resolved');
const crumbs = text('crumbs');
const crumbsPlain = crumbs.replace(/&#39;/g, "'").replace(/&rsquo;/g, "'");
if (!crumbsPlain.includes("Today's games")) {
  problems.push(`bet trail missing its parent: ${JSON.stringify(crumbs)}`);
}
// Nothing may leak a placeholder into a detail panel.
const rendered = ['crumbs', 'event-title', 'event-sub', 'event-detail', 'book-title', 'book-stats',
  'book-mix', 'bet-title', 'bet-sub', 'bet-spread', 'bet-books', 'bet-sides', 'bet-history']
  .map(text).join(' ');
for (const bad of ['NaN', 'undefined', '[object Object]']) {
  if (rendered.includes(bad)) problems.push(`detail panels leaked ${bad}`);
}

console.log(`routed ${Object.keys(globalThis.__PANELS).length} panels`);
console.log(`  fixture: ${JSON.stringify(text('event-title'))}`);
console.log(`  book:    ${JSON.stringify(text('book-title'))}`);
console.log(`  bet:     ${JSON.stringify(text('bet-title'))}`);
console.log(`  trail:   ${JSON.stringify(crumbs)}`);
if (problems.length) {
  console.error('ROUTING PROBLEMS:\n  ' + problems.join('\n  '));
  process.exit(1);
}
console.log('routing ok');
