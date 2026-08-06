// Exercises the dashboard's hash routing: every panel, plus a real fixture, book and
// bet key pulled out of the page's own payload.  The DOM stub is the same shape as
// tests/dashboard_smoke.mjs, with a settable location.hash added.
import { readFileSync } from 'node:fs';

const page = readFileSync(process.argv[2], 'utf8');
const payload = page.match(/<script type="application\/json" id="report-data">([\s\S]*?)<\/script>/)[1];
const script = page.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];

const nodes = new Map();
function make(id) {
  const node = {
    id, tagName: 'DIV', tabIndex: 0, value: '', textContent: '', _html: '',
    dataset: {}, style: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); } else if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    // A browser recomputes which option is selected on ANY option replacement, not only
    // when the old value has disappeared from the list — and `fillSelect` relies on
    // exactly that to drop a choice the new list no longer offers. Kept identical to
    // tests/dashboard_smoke.mjs; an earlier version here preserved a value the new
    // markup still contained, which is the opposite of the rule it claimed to model.
    set innerHTML(v) {
      this._html = v;
      if (/<option/.test(v)) this.value = '';
    },
    get innerHTML() { return this._html; },
    setAttribute() {}, getAttribute(n) { return n === 'href' ? '#overview' : null; },
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    dispatch(type) { for (const fn of this._listeners[type] || []) fn(); },
    querySelectorAll() { return []; },
    // Returns the node itself so `querySelector('tbody')` lands back here: rows are
    // appended into the tbody rather than written with the rest of the markup, and a
    // stub that answered null left every table a header-only shell — so the
    // not-empty assertions below were satisfied by column headings alone.
    querySelector() { return node; },
    replaceWith(other) { nodes.set(other.id || this.id || id, other); },
    // Only the positions the page uses. Anything else throws rather than quietly
    // appending: 'afterend' on a tbody foster-parents the rows out of the table in a
    // browser, leaving a permanently empty table body, and a stub that appends
    // regardless cannot tell that apart from working.
    insertAdjacentHTML(where, markup) {
      if (where !== 'beforeend' && where !== 'afterbegin') {
        throw new Error(`insertAdjacentHTML position not modelled by this stub: ${where}`);
      }
      this._html = where === 'afterbegin' ? markup + this._html : this._html + markup;
    },
    appendChild() {},
    get lastElementChild() { return this._html ? node : null; },
    // Holds the "show more" control so it does not vanish silently. This harness does
    // not assert on it — the doubling and orphaning checks live in
    // tests/dashboard_smoke.mjs — it only has to not lose it.
    _siblings: [],
    get _sibling() { return this._siblings[this._siblings.length - 1] || null; },
    insertAdjacentElement(_where, other) { node._siblings.push(other); other._parent = node; },
    remove() {
      const kin = this._parent && this._parent._siblings;
      if (kin) { const at = kin.indexOf(this); if (at >= 0) kin.splice(at, 1); }
    },
  };
  return node;
}
globalThis.document = {
  documentElement: make('root'),
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, make(id)); return nodes.get(id); },
  querySelector(sel) { return this.getElementById(sel.replace(/[^a-z0-9_-]/gi, '')); },
  querySelectorAll() { return []; },
  createElement(tag) { const n = make(''); n.tagName = tag.toUpperCase(); return n; },
};
document.getElementById('report-data').textContent = payload;
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };

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
if (on().join() !== 'arb') problems.push(`unknown hash showed [${on().join(', ')}]`);

// Empty hash is the front door — Arbitrage, latest scrape.
visit('');
if (on().join() !== 'arb') problems.push(`empty hash showed [${on().join(', ')}]`);

// Real keys out of the payload: a fixture, a book, and a bet.
// Primary panels always pin the latest scrape, so the sample row must come from it.
const COL = globalThis.__COL;
const latestId = data.meta.latest_run_id ?? (data.runs[0] && data.runs[0].id);
const row = data.quotes.rows.find((r) => r[COL.run_id] === latestId) || data.quotes.rows[0];
const fixtureKey = data.strings[row[COL.event_key]];
const bookKey = (data.runs.find((r) => r.id === latestId) || data.runs[0]).sources[0].key;
const betKey = globalThis.__betKeyOf(row);

// "Not empty" is not enough: rows are appended into the tbody after the headings are
// written, so a table with every row missing still carries its column headings and
// satisfied every check below. Dropping the first chunk of `fillInChunks` — which leaves
// no table anywhere on the page holding a single row — passed this file completely. So
// the rows are counted.
// Counts data items of either shape the page uses: body rows in a table, and price
// cards in a grid. A table's column headings are excluded by only looking after the
// head, so a header-only shell counts as zero — which is what it is.
const items = (id) => {
  const html = (nodes.get(id)?.innerHTML) || '';
  const at = html.lastIndexOf('</thead>');
  const rows = ((at < 0 ? html : html.slice(at)).match(/<tr[\s>]/g) || []).length;
  const cards = (html.match(/class="qcard/g) || []).length;
  return rows + cards;
};

visit('#fixture/' + encodeURIComponent(fixtureKey));
if (!text('event-detail')) problems.push('fixture panel rendered nothing');
if (!items('event-detail')) {
  problems.push('fixture panel has column headings but no priced rows');
}
if (!text('event-title').trim() || text('event-title').startsWith('Pick a')) {
  problems.push(`fixture title not resolved: ${JSON.stringify(text('event-title'))}`);
}

visit('#book/' + encodeURIComponent(bookKey));
if (!text('book-stats')) problems.push('book panel rendered nothing');
if (!items('book-mix')) problems.push('book panel left book-mix with headings but no rows');
if (text('book-title').startsWith('Pick a')) problems.push('book title not resolved');

visit('#bet/' + encodeURIComponent(betKey));
for (const id of ['bet-books', 'bet-sides', 'bet-history']) {
  if (!text(id)) problems.push(`bet panel left ${id} empty`);
  if (!items(id)) problems.push(`bet panel left ${id} with headings but no rows`);
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
