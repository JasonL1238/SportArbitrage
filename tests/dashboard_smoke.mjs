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
// Writes per region id, rather than per node object. `table` replaces its node outright
// when a filter empties it, so a counter living on the node resets to zero and a delta
// taken across that boundary is meaningless — which is how "did this region repaint"
// ended up being asked of the whole page instead, where any unrelated write answered
// yes. Keyed on the id, it survives replacement.
const writesById = new Map();
const writesTo = (...ids) => ids.reduce((n, id) => n + (writesById.get(id) || 0), 0);

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
    // Replacing a select's options resets its value in a browser, and `fillSelect`
    // relies on exactly that to drop a choice the new list no longer offers. Keyed on
    // the markup rather than on a tag name, because the stub makes every node a DIV.
    // `counted` is every write on the page; `_writes` is the writes to THIS node, and
    // `writesById` the writes to this region id however many nodes have held it. The
    // global counter cannot answer "did this region repaint", because a run change
    // rewrites the masthead and ten nav counts either way — so a panel that stopped
    // following the run still showed a healthy-looking total.
    _writes: 0,
    set innerHTML(v) {
      this._html = v;
      counted += 1;
      node._writes += 1;
      const key = node.id || id;
      if (key) writesById.set(key, (writesById.get(key) || 0) + 1);
      if (/<option/.test(v)) this.value = '';   // a browser resets to option 0 on any rewrite
    },
    get innerHTML() { return this._html; },
    setAttribute() {},
    getAttribute(name) { return name === 'href' ? '#overview' : null; },
    // Kept rather than dropped, so the harness can do what a reader does: the
    // page's whole run-scoped half is reachable only through this handler, and
    // a stub that swallowed it meant every render path was only ever executed
    // against the run the page happens to open on.
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    // An event object is passed through, because the drill-down click path is
    // delegated now: one handler on the container reads `ev.target` and walks
    // `parentNode` looking for a `data-go`. A dispatch that called handlers with no
    // argument made that whole path unreachable from here — `targetOf` could be
    // replaced with `() => null`, killing every row click on every table, and this
    // file stayed green.
    dispatch(type, ev) { for (const fn of this._listeners[type] || []) fn(ev || { target: node }); },
    querySelectorAll() { return []; },
    // Returns the node itself so a `querySelector('tbody')` lands back here: the stub
    // keeps no tree, and rows are appended into the tbody rather than written with the
    // rest of the markup, so answering null left every chunked table looking empty.
    // The selector is recorded because "which element did the rows go into" is
    // otherwise unknowable here — see the tbody assertion in the chunk checks.
    _queries: [],
    querySelector(sel) { node._queries.push(sel); return node; },
    // ``table`` sets the replacement's id *after* replaceWith, so ``other.id`` is
    // still blank here. ``this.id`` is what keeps the key right when the node being
    // replaced is itself a replacement — an empty table rendered twice used to
    // orphan the region under a blank key and read as unfilled.
    replaceWith(other) { nodes.set(other.id || this.id || id, other); },
    // Only the two positions the page actually uses are modelled. Anything else
    // throws rather than quietly appending: `beforeend` and `afterend` differ by
    // whether the rows land *inside* the tbody or beside it, and a browser
    // foster-parents the latter out of the table entirely — a permanently empty
    // table body. A stub that treated every `where` as "append" let that mutation
    // pass. The position used is recorded so a check can assert it.
    _inserts: [],
    insertAdjacentHTML(where, markup) {
      if (where !== 'beforeend' && where !== 'afterbegin') {
        throw new Error(`insertAdjacentHTML position not modelled by this stub: ${where}`);
      }
      node._inserts.push(where);
      // 'afterbegin' instead of 'beforeend' would put each new chunk above the rows
      // already there and scramble the order on every click.
      this._html = where === 'afterbegin' ? markup + this._html : this._html + markup;
      counted += 1;
      node._writes += 1;
      const key = node.id || id;
      if (key) writesById.set(key, (writesById.get(key) || 0) + 1);
    },
    appendChild() {},
    // Enough of a tail for chunked filling to keep watching: it only needs to know
    // that a last row exists, and every appended chunk ends in one.
    get lastElementChild() { return this._html ? node : null; },
    // The "show more" control is inserted beside the container, so the harness has
    // to be able to hold it and click it — that click is the guarantee that rows
    // held back by chunking are reachable without a scroll event.
    // A list, not a single slot: the doubling this harness checks for is *two*
    // controls beside one container, which a one-slot stub could never show.
    _siblings: [],
    get _sibling() { return this._siblings[this._siblings.length - 1] || null; },
    // The position matters as much as it does for the markup variant, and for the same
    // reason: `coverage`, `odds-table` and `findings` are <table> elements, so
    // 'beforeend' puts the control *inside* the table, where the next
    // `node.innerHTML = …` destroys it while `chunkControls` still holds a reference.
    // "Beside, not inside" is the invariant every dropChunkControl path is built on, and
    // a stub that ignored `where` could not express it.
    insertAdjacentElement(where, other) {
      if (where !== 'afterend') {
        throw new Error(`insertAdjacentElement position not modelled by this stub: ${where}`);
      }
      node._siblings.push(other);
      other._parent = node;
    },
    // Detaches from a sibling list, and records that it happened. The flag is what makes
    // removing a node observable for a node with no parent — the JSON island is one, and
    // dropping it is how the page frees the largest single thing it holds (15.6MB of
    // text for a 16-scrape dashboard). Without the flag, never dropping it passed.
    _removed: false,
    remove() {
      node._removed = true;
      const kin = this._parent && this._parent._siblings;
      if (kin) { const at = kin.indexOf(this); if (at >= 0) kin.splice(at, 1); }
    },
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

// Keeps its callback and its targets, so the scroll path of chunked filling can be
// driven from here. Without that, `watchTail` and the re-observe inside the callback
// were wholly uncovered — and that is the *primary* way a real reader reaches row
// 121, so an observer that fires once and then stops watching forever looked fine.
// Nothing fires by itself: a real preview pane is 0x0 and hidden, where an
// IntersectionObserver never fires either, so the tests do the scrolling explicitly.
globalThis.IntersectionObserver = class {
  constructor(cb) { this._cb = cb; this._targets = []; }
  observe(target) { this._targets.push(target); }
  disconnect() { this._targets = []; }
  /** Deliver an intersection for the tail this observer is watching. */
  __scroll() {
    if (!this._targets.length) return false;
    this._cb([{ isIntersecting: true, target: this._targets[this._targets.length - 1] }], this);
    return true;
  }
  /** Deliver a record that was queued *before* disconnect and arrives after it.
   *  `disconnect()` unobserves every target but is not specified to discard records
   *  already queued for delivery, so a real observer retired by a filter change can
   *  still call back once. Modelling disconnect as "targets cleared, callback
   *  unreachable" made that impossible to express, and a fill with no defence against
   *  a late callback looked safe. */
  __scrollAfterDisconnect() { this._cb([{ isIntersecting: true, target: null }], this); }
};
// Routing is how a panel is now built, so the harness needs somewhere for the hash
// to live. Nothing here fires hashchange; the checks below call applyRoute, which
// is what the real listener does.
globalThis.location = { hash: '' };
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
    globalThis.__isUsUnavailable = isUsUnavailable;
    globalThis.__currentRows = currentRows;
    globalThis.__arbBundle = arbBundle;
    globalThis.__showOffshore = () => showOffshore;
    globalThis.__setShowOffshore = (on) => {
      showOffshore = on;
      const box = el('offshore-toggle');
      if (box) box.checked = on;
      buildSportPicker();
      renderRunScoped();
    };
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
    globalThis.__betLink = betLink;
    globalThis.__fillInChunks = fillInChunks;
    globalThis.__table = table;
    globalThis.__cell = cell;
    globalThis.__ROW_CHUNK = ROW_CHUNK;
    globalThis.__PANELS = PANELS;
    globalThis.__visitPanel = (name, arg) => { here = { panel: name, arg: arg ?? null }; showCurrentPanel(); };
    globalThis.__applyRoute = applyRoute;
    globalThis.__invalidatePanels = invalidatePanels;
    globalThis.__eventKeys = () => eventSummaries(currentRows()).map((e) => e.key);
    globalThis.__shownChild = () => shownChild;
    globalThis.__selectedEvent = () => selectedEvent;
    globalThis.__renderEvents = renderEvents;
    globalThis.__movementScope = () => (movementCache ? movementCache.sport : null);
    globalThis.__currentSport = () => currentSport;
    globalThis.__movedCount = movedCount;
    globalThis.__booksBySport = () => {
      const map = {};
      for (const r of runRows()) {
        const sp = str(r[COL.sport]);
        (map[sp] ||= new Set()).add(str(r[COL.source]));
      }
      const rows = {};
      for (const r of runRows()) {
        const sp = str(r[COL.sport]);
        rows[sp] = (rows[sp] || 0) + 1;
      }
      return Object.fromEntries(Object.entries(map)
        .map(([k, v]) => [k, { books: [...v], rows: rows[k] || 0 }]));
    };
    globalThis.__embeddedRuns = () => runs.filter((r) => rowsByRun.has(r.id)).map((r) => r.id);
    globalThis.__renderNavCounts = renderNavCounts;
    globalThis.__defaults = () => ({ book: defaultBook(), fixture: defaultFixture(), bet: defaultBet() });
    globalThis.__here = () => here;
    globalThis.__lastList = () => lastList;
    globalThis.__PANEL_HEAVY = PANEL_HEAVY;
    globalThis.__wireRowLinks = wireRowLinks;
    globalThis.__leaguesOf = () => [...new Set(currentRows().map((r) => str(r[COL.league])).filter(Boolean))];
    globalThis.__filteredGames = () => filteredGameEvents().length;
    globalThis.__screenGames = () => eventSummaries(currentRows()
      .filter((r) => !currentLeague || str(r[COL.league]) === currentLeague)).length;
    globalThis.__promoPlanHtml = promoPlanHtml;
    globalThis.__promoDetailHtml = promoDetailHtml;
    globalThis.__renderPromos = renderPromos;
    globalThis.__promoMoney = promoMoney;
    globalThis.__setPromoPlans = (plans, meta) => { PROMOS.plans = plans; PROMOS.plan_meta = meta; };
    globalThis.__setPromoOffers = (offers) => { PROMOS.offers = offers; };
    globalThis.__setPromoRun = (run) => { PROMOS.run = run; };
    globalThis.__promoSnapshot = { run: PROMOS.run, offers: PROMOS.offers,
                                   plans: PROMOS.plans, plan_meta: PROMOS.plan_meta };
    globalThis.__restorePromos = () => {
      PROMOS.run = globalThis.__promoSnapshot.run;
      PROMOS.offers = globalThis.__promoSnapshot.offers;
      PROMOS.plans = globalThis.__promoSnapshot.plans;
      PROMOS.plan_meta = globalThis.__promoSnapshot.plan_meta;
    };
  `)();
} catch (err) {
  errors.push(err);
}

if (errors.length) {
  for (const e of errors) console.error('RUNTIME ERROR:', e.stack);
  process.exit(1);
}

// Panels are built on arrival rather than all at once, so the harness has to do
// the arriving. Walking every one is what keeps "every render path executes" true
// — without this, loading the page would only ever exercise the panel the URL
// happens to open on, and the other twelve would rot unnoticed.
{
  const visited = [];
  for (const name of Object.keys(globalThis.__PANELS)) {
    try {
      globalThis.__visitPanel(name, null);
      visited.push(name);
    } catch (err) {
      console.error(`RENDER ERROR on panel #${name}:`, err.stack);
      process.exit(1);
    }
  }
  try {
    globalThis.__renderNavCounts();
  } catch (err) {
    console.error('RENDER ERROR in nav counts:', err.stack);
    process.exit(1);
  }
  if (visited.length < 15) {
    console.error(`only ${visited.length} panels walked; PANELS should have more`);
    process.exit(1);
  }
  console.log(`walked ${visited.length} panels: ${visited.join(' ')}`);
}

// Chunked filling must never lose a row. Two ways in, and both are checked: the
// click, and the scroll. The scroll path cannot fire by itself here — a real preview
// pane is 0x0 and hidden, where an IntersectionObserver never fires either — so the
// stub keeps the observer's callback and the checks deliver the intersection
// themselves. Left undriven, `watchTail` and the re-observe inside the callback were
// uncovered, and an observer that yields one extra chunk and then stops watching
// forever passed — that being the way most readers actually reach row 121.
{
  const problems = [];
  const CHUNK = globalThis.__ROW_CHUNK;
  const total = CHUNK * 3 + 7;                        // a partial last chunk on purpose
  const rows = Array.from({ length: total }, (_, i) => i);
  const host = make('chunk-host');
  const seen = [];
  globalThis.__fillInChunks(host, host, rows, (r) => { seen.push(r); return `<tr data-i="${r}"></tr>`; },
    { noun: 'games' });

  if (seen.length !== CHUNK) problems.push(`first chunk built ${seen.length} rows, want ${CHUNK}`);
  const note = () => (host._sibling ? host._sibling.textContent : '');
  if (!note().includes(String(total))) {
    problems.push(`held-back rows not stated; note was ${JSON.stringify(note())}`);
  }

  // Click until it gives up offering more, with a hard stop so a non-advancing
  // step cannot spin here forever.
  let clicks = 0;
  while (host._sibling && clicks < 100) {
    host._sibling.dispatch('click');
    clicks += 1;
  }
  if (seen.length !== total) problems.push(`clicking reached ${seen.length} of ${total} rows`);
  if (new Set(seen).size !== total) problems.push(`rows repeated: ${seen.length - new Set(seen).size} duplicates`);
  // ...and in order. A chunk appended at the wrong end reads as the slate being
  // shuffled every time the reader asks for more.
  const order = [...(host.innerHTML.match(/data-i="(\d+)"/g) || [])].map((m) => Number(m.slice(8, -1)));
  const sorted = [...order].sort((x, y) => x - y);
  if (order.join(',') !== sorted.join(',')) {
    problems.push(`rows are out of order: starts ${order.slice(0, 4).join(',')} after ${clicks} chunk(s)`);
  }
  if (host._sibling) problems.push('control still offering more after every row was built');
  if (clicks !== 3) problems.push(`took ${clicks} clicks to finish 3 remaining chunks`);

  if (problems.length) {
    console.error('CHUNKED FILL LOSES ROWS:', problems.join('; '));
    process.exit(1);
  }
  console.log(`chunked fill reaches every row (${total} in ${clicks + 1} chunks, none repeated)`);
}

// The same guarantee by scrolling instead of clicking. Scrolling to the tail has to
// keep working all the way down: the callback disconnects and must re-observe the new
// tail, or the reader gets exactly one more chunk and then a list that refuses to grow
// no matter how far they scroll — with the control still promising more.
{
  const problems = [];
  const CHUNK = globalThis.__ROW_CHUNK;
  const total = CHUNK * 4;
  const rows = Array.from({ length: total }, (_, i) => i);
  const host = make('scroll-host');
  const seen = [];
  globalThis.__fillInChunks(host, host, rows, (r) => { seen.push(r); return `<tr data-i="${r}"></tr>`; },
    { noun: 'games' });
  if (seen.length !== CHUNK) problems.push(`first chunk built ${seen.length} rows, want ${CHUNK}`);
  if (!host.__chunkIO) problems.push('nothing is watching the tail after the first chunk');

  // Scroll to the bottom, repeatedly, exactly as a reader would.
  let scrolls = 0;
  while (host.__chunkIO && scrolls < 100) {
    if (!host.__chunkIO.__scroll()) break;
    scrolls += 1;
  }
  if (seen.length !== total) problems.push(`scrolling reached ${seen.length} of ${total} rows`);
  if (new Set(seen).size !== total) problems.push(`scrolling repeated rows: ${seen.length - new Set(seen).size} duplicates`);
  if (scrolls !== 3) problems.push(`took ${scrolls} scrolls to finish 3 remaining chunks`);
  // Finished means finished: nothing left watching, and no control still offering.
  if (host.__chunkIO) problems.push('still watching the tail after every row was built');
  if (host._sibling) problems.push('control still offering more after scrolling to the end');

  // A retired fill must not append into a host that has been re-filled. `disconnect`
  // is not specified to discard records already queued, and renderBrowseGames passes
  // the grid as its own tbody — so a superseded callback would drop the previous
  // filter's cards in after the new ones.
  const reused = make('scroll-host-2');
  const first = [];
  globalThis.__fillInChunks(reused, reused, Array.from({ length: CHUNK * 2 }, (_, i) => 'old' + i),
    (r) => { first.push(r); return `<tr data-i="${r}"></tr>`; }, { noun: 'games' });
  const staleIO = reused.__chunkIO;
  const second = [];
  reused._html = '';
  globalThis.__fillInChunks(reused, reused, Array.from({ length: CHUNK * 2 }, (_, i) => 'new' + i),
    (r) => { second.push(r); return `<tr data-i="${r}"></tr>`; }, { noun: 'games' });
  const before = first.length;
  const secondBefore = second.length;
  // A record queued before the retired observer was disconnected, arriving after it.
  if (staleIO) staleIO.__scrollAfterDisconnect();
  if (first.length !== before) {
    problems.push(`a superseded fill appended ${first.length - before} rows after being replaced`);
  }
  if (second.length !== secondBefore) {
    problems.push(`a superseded fill drove the live fill on ${second.length - secondBefore} extra rows`);
  }
  if (reused.innerHTML.includes('data-i="old')) {
    problems.push("the previous fill's rows are in the re-filled host");
  }

  if (problems.length) {
    console.error('SCROLLING DOES NOT REACH EVERY ROW:', problems.join('; '));
    process.exit(1);
  }
  console.log(`scrolling reaches every row too (${total} in ${scrolls + 1} chunks), and a replaced fill stops appending`);
}

// The same guarantee for a fill that is retired WITHOUT a replacement — the case the
// block above cannot see, because it only ever refills. Most render paths drop the
// control and then return: an empty result set, a scrape with no embedded prices, an
// evicted panel. Retiring only on refill left every one of those open, and for the two
// game grids the host is its own tbody, so nothing detaches under a late append.
//
// What that produced: filter Games to something nothing matches, and one queued
// observer record put 120 cards of the previous slate underneath "No games match these
// filters" — then, because the callback re-arms itself, kept going.
//
// Driven through the real Games filter rather than a synthetic host, so the path is the
// one a reader takes.
{
  onAnEmbeddedRun();
  const problems = [];
  globalThis.location.hash = '#events';
  globalThis.__applyRoute();

  const grid = () => nodes.get('events-games');
  const cards = () => ((grid()?.innerHTML || '').match(/class="game-card/g) || []).length;
  const full = cards();
  if (!full) {
    console.log('retired-fill check skipped; the Games grid rendered no cards to retire');
  } else {
    const staleIO = grid().__chunkIO;
    // Filter down to nothing, synchronously (the book select is not debounced).
    const q = nodes.get('events-q');
    q.value = 'zzzz-no-such-team-anywhere';
    globalThis.__renderEvents();
    const emptied = cards();
    if (emptied !== 0) problems.push(`filtering to nothing still shows ${emptied} card(s)`);
    if (!(grid().innerHTML || '').includes('No games match these filters')) {
      problems.push('the empty state is not on screen after filtering to nothing');
    }
    if (grid()._siblings.length) {
      problems.push(`a control survived into the empty state: ${JSON.stringify(grid()._sibling.textContent)}`);
    }

    // Now the record that was queued before that fill was retired.
    if (staleIO) staleIO.__scrollAfterDisconnect();
    if (cards() !== 0) {
      problems.push(`${cards()} card(s) of the previous slate came back under the empty state`);
    }
    // And it must not have re-armed itself to keep going.
    if (grid().__chunkIO) {
      grid().__chunkIO.__scrollAfterDisconnect();
      if (cards() !== 0) problems.push('the retired fill re-armed and kept appending');
    }

    q.value = '';
    globalThis.__renderEvents();
    if (cards() !== full) problems.push(`clearing the filter rebuilt ${cards()} cards, want ${full}`);
  }

  if (problems.length) {
    console.error('A RETIRED FILL STILL APPENDS INTO AN EMPTIED REGION:', problems.join('; '));
    process.exit(1);
  }
  console.log('a fill retired by an empty result set cannot append into it');
}

// Every hand-written blank of a chunked region must go through `blankRegion`.
//
// Read off the source rather than exercised, because the branches that get this wrong
// are the ones no fixture reaches: "this run recorded no per-venue detail" needs a run
// with an empty `sources` array and all sixteen here have one; "no scrapes yet" needs a
// page with zero runs. Both would strand a control over an emptied table and append the
// previous run's rows on a click — the exact bug fixed twice already, in two different
// renderers, where the only difference was which branch a reader could get to. A data
// check would pass on this page and ship the third one.
{
  const problems = [];
  // The regions a chunk control can attach to: everything `PANEL_HEAVY` reclaims, plus
  // the two game grids and the drill-down tables that `table()` fills.
  const chunked = ['findings', 'overround', 'rejections', 'skips', 'coverage', 'events-games',
    'odds-table', 'odds-screen', 'move-table', 'raws', 'sports-gaps', 'matrix',
    'browse-games', 'source-cards', 'promo-list', 'arb-list', 'event-detail',
    'book-mix', 'bet-books', 'bet-sides', 'bet-history'];
  for (const id of chunked) {
    // `el('x').innerHTML = ''` and the `node.innerHTML = ''` form inside a forEach over
    // ids are both spelled out in the source; the first is what regressed twice.
    const bare = new RegExp(`el\\(['"]${id}['"]\\)\\.innerHTML\\s*=\\s*['"]['"]`, 'g');
    const hits = (script.match(bare) || []).length;
    if (hits) {
      problems.push(`${id} is blanked ${hits}x with a bare innerHTML = '' instead of blankRegion()`);
    }
  }
  // And the helper has to still exist and still drop the control, or the rule above is
  // satisfied by a function that does nothing.
  if (!/function blankRegion\(id\)/.test(script)) {
    problems.push('blankRegion is gone; the rule above no longer means anything');
  } else {
    const body = script.slice(script.indexOf('function blankRegion(id)'));
    if (!/dropChunkControl\(node\)/.test(body.slice(0, 260))) {
      problems.push('blankRegion no longer drops the chunk control');
    }
  }

  if (problems.length) {
    console.error('A CHUNKED REGION IS BLANKED WITHOUT DROPPING ITS CONTROL:', problems.join('; '));
    process.exit(1);
  }
  console.log(`all ${chunked.length} chunkable regions are blanked through blankRegion`);
}

// The two biggest regions must drop their control on their own empty branches too. Only
// the Checks panel was checked, and it is the smallest of the three: on the odds board a
// surviving control appended 677,712 chars of the previous scrape's rows — every one
// still carrying `data-go` to that scrape's fixtures — under the not-embedded notice.
{
  const problems = [];
  // Said out loud when a fixture is too small for a case to mean anything. A page with
  // fewer rows than one chunk holds nothing back, so there is no control to orphan and no
  // tail to watch — that is not a pass and it is not a failure, and the small synthetic
  // pages tests/test_report.py builds are all like this.
  const inconclusive = [];
  const embedded = globalThis.__embeddedRuns();
  const allRuns = globalThis.__DATA.runs.map((r) => r.id);
  const thin = allRuns.find((id) => !embedded.includes(id));
  const pick = nodes.get('run-pick');
  const controls = (id) => ((nodes.get(id) || {})._siblings || []).length;
  const bulk = (id) => ((nodes.get(id) || {}).innerHTML || '').length;

  // The board: open it on a scrape with rows held back, then pick one with no prices.
  if (thin === undefined || !embedded.length) {
    console.log('board orphan-control check skipped; every run in this page is embedded');
  } else {
    onAnEmbeddedRun();
    globalThis.location.hash = '#screen';
    globalThis.__applyRoute();
    const had = controls('odds-screen');
    pick.value = String(thin);
    pick.dispatch('change');
    if (controls('odds-screen')) {
      const say = nodes.get('odds-screen')._sibling.textContent;
      problems.push(`the board kept its control over an emptied board: ${JSON.stringify(say)}`);
      const before = bulk('odds-screen');
      nodes.get('odds-screen')._sibling.dispatch('click');
      if (bulk('odds-screen') > before) {
        problems.push(`clicking it appended ${bulk('odds-screen') - before} chars of the previous scrape`);
      }
    }
    if (!had) inconclusive.push('the board held nothing back, so no control could be orphaned');
    onAnEmbeddedRun();
  }

  // The games grid: same thing reached by a filter rather than a run change.
  onAnEmbeddedRun();
  globalThis.location.hash = '#events';
  globalThis.__applyRoute();
  const hadGrid = controls('events-games');
  const q = nodes.get('events-q');
  q.value = 'zzzz-no-such-team-anywhere';
  globalThis.__renderEvents();
  if (controls('events-games')) {
    problems.push(`the games grid kept its control over an empty result set: ${JSON.stringify(nodes.get('events-games')._sibling.textContent)}`);
  }
  if (!hadGrid) inconclusive.push('the games grid held nothing back, so no control could be orphaned');
  q.value = '';
  globalThis.__renderEvents();

  // And leaving a panel has to tear the control and the observer down, not just blank
  // the markup. A live observer, and a button whose closure pins the whole rows array,
  // is exactly the retention unloading exists to reclaim.
  globalThis.location.hash = '#screen';
  globalThis.__applyRoute();
  const boardHadControl = controls('odds-screen');
  // Held onto BEFORE leaving. Reading `__chunkIO` afterwards cannot detect the failure:
  // clearing that reference without calling `disconnect()` leaves the observer alive and
  // still watching a row nobody can see, while the reference the check reads is null —
  // so the check would skip and report nothing.
  const io = (nodes.get('odds-screen') || {}).__chunkIO;
  const watchedBefore = io ? io._targets.length : 0;
  globalThis.location.hash = '#raw';
  globalThis.__applyRoute();
  if (controls('odds-screen')) problems.push('leaving the board left its control behind');
  if (io && io._targets.length) {
    problems.push(`leaving the board left an observer watching ${io._targets.length} target(s)`);
  }
  if (!boardHadControl) inconclusive.push('the board held nothing back before being left');
  if (!watchedBefore) inconclusive.push('nothing was watching the board before it was left');

  if (problems.length) {
    console.error('AN EMPTIED OR EVICTED REGION KEEPS ITS CONTROL:', problems.join('; '));
    process.exit(1);
  }
  for (const why of inconclusive) console.log(`  (control check inconclusive: ${why})`);
  console.log('the board and the games grid drop their controls when emptied, and when left');
}

// The control sits beside its container, so it does not die with it. Twice over on
// the live page: a filter that emptied the coverage table left "Showing 120 of
// 1,702 rows" sitting above "No games match these filters", and clearing the filter
// again added a second control every time.
{
  const problems = [];
  const CHUNK = globalThis.__ROW_CHUNK;
  const many = Array.from({ length: CHUNK * 2 }, (_, i) => i);
  const cols = [{ label: 'n', cell: (r) => globalThis.__cell(String(r)) }];
  const host = make('chunk-empty-host');
  nodes.set('chunk-empty-host', host);

  globalThis.__table(host, cols, many, { empty: 'Nothing matches.' });
  if (!host._sibling) problems.push('no control offered for a table with rows held back');
  // Rows have to go into the tbody. `table` writes the head and an empty tbody, then
  // fills the tbody separately, so the element it asks for is the whole of what says
  // where the rows land — and the stub keeps no tree, so this is the only way to know.
  // `querySelector('thead')` here puts every row of the coverage, odds, movement, raw
  // and bet tables inside the table head, which is not a state the stub can otherwise
  // tell apart from a correct one.
  if (!host._queries.includes('tbody')) {
    problems.push(`table rows were inserted into ${JSON.stringify(host._queries)}, not a tbody`);
  }

  // Same region, now filtered down to nothing.
  const live = () => nodes.get('chunk-empty-host');
  globalThis.__table(live(), cols, [], { empty: 'Nothing matches.' });
  const orphan = [host, live()].flatMap((n) => (n && n._siblings) || []);
  if (orphan.length) {
    problems.push(`control survived into the empty state saying ${JSON.stringify(orphan[0].textContent)}`);
  }

  // ...and back again: exactly one control, not one per cycle.
  globalThis.__table(live(), cols, many, { empty: 'Nothing matches.' });
  globalThis.__table(live(), cols, [], { empty: 'Nothing matches.' });
  globalThis.__table(live(), cols, many, { empty: 'Nothing matches.' });
  const controls = [host, live()].flatMap((n) => (n && n._siblings) || []);
  if (controls.length !== 1) problems.push(`${controls.length} controls after three cycles, want 1`);

  if (problems.length) {
    console.error('CHUNK CONTROL OUTLIVES ITS ROWS:', problems.join('; '));
    process.exit(1);
  }
  console.log('chunk control is dropped with its rows and never doubles up');
}

// A link straight into a panel has to open on something. The eager render used to
// guarantee that by building all thirteen panels up front; now it is routing that
// guarantees it, so routing is what gets checked — hash in, markup out, on a panel
// that has deliberately been emptied first.
{
  const owns = {
    arb: 'arb-list', screen: 'odds-screen', promos: 'promo-list', events: 'coverage',
    overview: 'home-stats', run: 'stat-strip', sports: 'sports-grid', sources: 'source-cards',
    odds: 'odds-table', movement: 'move-table', quality: 'findings', raw: 'raws',
    // The three drill-downs are checked on the region holding their *content*, not on
    // their heading. A heading is written either way: with no subject resolved it reads
    // "Pick a game", so `defaultFixture()` could be made to return nothing and a bare
    // #fixture would render a placeholder over an empty panel while this check passed.
    book: 'book-stats', fixture: 'event-detail', bet: 'bet-books',
  };
  // ...and a placeholder in the heading is a failure in its own right, because a bare
  // #fixture, #bet or #book is a link the page itself hands out.
  const titles = { book: 'book-title', fixture: 'event-title', bet: 'bet-title' };
  const problems = [];
  for (const [panel, region] of Object.entries(owns)) {
    globalThis.__invalidatePanels();
    const node = nodes.get(region) || document.getElementById(region);
    node._html = '';
    node.textContent = '';
    globalThis.location.hash = '#' + panel;
    try {
      globalThis.__applyRoute();
    } catch (err) {
      problems.push(`#${panel} threw: ${err.message}`);
      continue;
    }
    const fresh = nodes.get(region);
    if (!((fresh?.innerHTML || '') + (fresh?.textContent || '')).trim()) {
      problems.push(`#${panel} left ${region} empty`);
    }
    if (titles[panel]) {
      const heading = String(((nodes.get(titles[panel]) || {}).textContent) ?? '');
      if (/^(Pick a|Not in this scrape)/.test(heading)) {
        problems.push(`#${panel} opened on a placeholder: ${JSON.stringify(heading)}`);
      }
    }
  }
  if (problems.length) {
    console.error('ROUTING DOES NOT BUILD THE PANEL:', problems.join('; '));
    process.exit(1);
  }
  console.log(`routing builds the panel it opens (${Object.keys(owns).length} panels, each from empty)`);

  // Deliberately does not re-walk the panels here. The block that needs everything on
  // screen at once does its own walk immediately beforehand, and the next block
  // invalidates everything anyway, so a walk here was provably dead.
}

// Arriving at a panel must build ONE screenful, not the whole slate. Reverting the
// board to `events.map(boardRow).join('')` is a one-line edit that silently restores
// the 399,090-element render this entire change exists to prevent, and every other
// check in this file passed with it reverted. So the count itself is pinned.
{
  const problems = [];
  const CHUNK = globalThis.__ROW_CHUNK;
  if (!(CHUNK >= 20 && CHUNK <= 300)) {
    problems.push(`ROW_CHUNK is ${CHUNK}; a chunk is meant to be a screenful, not a slate`);
  }

  globalThis.__invalidatePanels();
  globalThis.location.hash = '#screen';
  globalThis.__applyRoute();
  const board = nodes.get('odds-screen');
  // Counted by the row link rather than by `<tr`, which would also catch the header.
  const rows = ((board && board.innerHTML) || '').match(/data-go="#fixture/g) || [];
  // The note is written by renderOddsScreen itself, so it is an independent account
  // of how many games there are to show.
  const stated = Number((((nodes.get('screen-note') || {}).textContent) || '').match(/^([\d,]+) game/)?.[1]?.replace(/,/g, '') || 0);

  if (stated > CHUNK) {
    if (rows.length > CHUNK) {
      problems.push(`board built ${rows.length} rows on arrival for ${stated} games; want at most ${CHUNK}`);
    }
    // Rows have to go into the tbody. The stub keeps no tree, so the recorded selector
    // is the only way to tell tbody from thead — and querying the wrong one would put
    // all 120 board rows inside the table header.
    if (board && !board._queries.includes('tbody')) {
      problems.push(`board rows were inserted into ${JSON.stringify(board._queries)}, not a tbody`);
    }
    const control = board && board._sibling;
    if (!control) {
      problems.push(`${stated - rows.length} games held back with no control offering them`);
    } else if (!control.textContent.includes(stated.toLocaleString())) {
      problems.push(`control says ${JSON.stringify(control.textContent)}, which does not name the ${stated} games there are`);
    }
  }

  // The two counts that describe games must not be quoting row counts. renderOdds
  // owns nav-odds (prices); renderOddsScreen and renderEvents own the game counts.
  globalThis.__visitPanel('events', null);
  globalThis.__renderNavCounts();
  const num = (id) => Number((((nodes.get(id) || {}).textContent) || '').replace(/,/g, ''));
  const gamesStated = Number(((((nodes.get('events-games-note') || {}).textContent) || '').match(/^([\d,]+) game/)?.[1] || '0').replace(/,/g, ''));
  if (stated && num('nav-screen') !== stated) {
    problems.push(`nav-screen says ${num('nav-screen')}, the board says ${stated} games`);
  }
  if (gamesStated && num('nav-events') !== gamesStated) {
    problems.push(`nav-events says ${num('nav-events')}, the Games panel says ${gamesStated} games`);
  }

  if (problems.length) {
    console.error('ARRIVAL BUILDS TOO MUCH, OR COUNTS DISAGREE:', problems.join('; '));
    process.exit(1);
  }
  console.log(`arrival builds one chunk of ${CHUNK} (${stated} games available) and the nav counts agree with their panels`);
}

// Switching runs must rebuild the panels, not just the masthead. Dropping
// `invalidatePanels()` from renderRunScoped leaves the rail, run-meta and nav counts
// updating while every panel keeps showing the previous run — the worst kind of
// wrong, because nothing looks broken.
//
// Pinned on whether the panel is rebuilt at all, not on its text changing: two runs
// can legitimately render the same numbers, and on the small fixture pages they do.
{
  const embedded = globalThis.__embeddedRuns();
  if (embedded.length < 2) {
    console.log(`run-switch rebuild check skipped; only ${embedded.length} embedded run(s)`);
  } else {
    const pick = nodes.get('run-pick');
    // Build the All-prices panel, then leave it, so it is up to date and off screen.
    pick.value = String(embedded[0]);
    pick.dispatch('change');
    globalThis.__visitPanel('odds', null);
    globalThis.location.hash = '#arb';
    globalThis.__applyRoute();

    // Switch runs while somewhere else, then arrive at All prices. If the switch
    // marked the panels stale, arriving rebuilds it and writes markup.
    pick.value = String(embedded[1]);
    pick.dispatch('change');
    const before = counted;
    globalThis.__visitPanel('odds', null);
    const wrote = counted - before;

    if (wrote === 0) {
      console.error('SWITCHING RUNS DOES NOT REBUILD THE PANELS: '
        + `arriving at All prices after switching to run ${embedded[1]} wrote nothing, `
        + 'so it is still showing the previous run');
      process.exit(1);
    }
    console.log(`switching runs rebuilds the panel arrived at (${wrote} writes for run ${embedded[1]})`);
  }
}

// A drill-down must repaint on a run change too, and it is guarded differently from
// the list panels: `shownChild` remembers which game/book/bet is on screen so one
// route pass cannot build it twice, and `invalidatePanels` has to clear that. Without
// the reset the masthead, run-meta and every nav count update while the open book's
// own numbers stay on the run the reader just left — nothing looks broken.
//
// Counted rather than compared, for the same reason as the block above: the same book
// can render byte-identical text under two runs, and on these pages it does.
{
  const embedded = globalThis.__embeddedRuns();
  if (embedded.length < 2) {
    console.log(`drill-down repaint check skipped; only ${embedded.length} embedded run(s)`);
  } else {
    const pick = nodes.get('run-pick');
    pick.value = String(embedded[0]);
    pick.dispatch('change');
    const book = globalThis.__defaults().book;
    globalThis.location.hash = '#book/' + encodeURIComponent(book);
    globalThis.__applyRoute();

    // Staying on the panel, switch the run under it. Counted per region, not globally:
    // a run change rewrites the masthead and ten nav counts whether or not the open
    // panel follows, so a page-wide total says nothing about this panel.
    const owned = ['book-stats', 'book-mix'];
    // Keyed on the id, not the node: `book-mix` is a `table()` region, so a book with no
    // rows replaces the node and a per-node delta straddles that boundary — it can even
    // go negative, which reads as success.
    const writes = () => writesTo(...owned);
    const before = writes();
    pick.value = String(embedded[1]);
    pick.dispatch('change');
    const wrote = writes() - before;
    if (wrote === 0) {
      console.error('AN OPEN DRILL-DOWN DOES NOT FOLLOW A RUN CHANGE: '
        + `switching to run ${embedded[1]} with ${book} open rewrote none of ${owned.join(', ')}, `
        + 'so it is still showing the previous run');
      process.exit(1);
    }
    // ...and the dedupe tag has to still name what is on screen, or the next route
    // pass to the same book would skip a rebuild it does need.
    const tag = globalThis.__shownChild();
    if (tag !== 'book\x1f' + book) {
      console.error(`AN OPEN DRILL-DOWN LOST TRACK OF ITS SUBJECT: tag is ${JSON.stringify(tag)}`);
      process.exit(1);
    }
    console.log(`an open drill-down follows a run change (${wrote} writes for ${book} on run ${embedded[1]})`);
  }
}

// Picking a league must fix BOTH rail counts, not just the panel in front of you. The
// league control is duplicated on Odds and on Today's games so a narrowed slate sticks
// when you flip between them — which means one pick changes what both boards hold while
// only one of them renders. Nothing else writes the other one's count: its own renderer
// does, and it did not run.
//
// Reachable in one click, and it read as a data error rather than a stale number:
// picking a league on Odds narrowed the board to 31 games while "Today's games" in the
// rail went on saying 1,702. The earlier version of this check only ever ran with no
// league chosen, where the two agree for free.
{
  onAnEmbeddedRun();
  // Let the run change's own nav-count timer land before anything below measures.
  await new Promise((resolve) => setTimeout(resolve, 0));
  const problems = [];
  const leagues = globalThis.__leaguesOf();
  if (leagues.length < 2) {
    console.log(`league nav-count check skipped; only ${leagues.length} league(s) in this run`);
  } else {
    // Stringified first: some writers set a count to a number and some to a string,
    // and this reader has to survive both.
    const num = (id) => Number(String(((nodes.get(id) || {}).textContent) ?? '').replace(/,/g, ''));
    // The counts are written on a timer, so this waits for them to agree rather than
    // reading them the instant the pick is made. Waiting *for the right answer* means
    // a stale count costs the budget and then fails, and a correct one costs a tick.
    const agree = async (budgetMs = 3000) => {
      const started = Date.now();
      for (;;) {
        const want = { screen: globalThis.__screenGames(), events: globalThis.__filteredGames() };
        const got = { screen: num('nav-screen'), events: num('nav-events') };
        if (got.screen === want.screen && got.events === want.events) return null;
        if (Date.now() - started >= budgetMs) return { want, got };
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
    };

    // Pick the league from each panel in turn: the stale count is the *other* panel's
    // both times, so one direction alone would miss half of it.
    for (const [panel, control] of [['screen', 'league-pick'], ['events', 'events-league']]) {
      globalThis.location.hash = '#' + panel;
      globalThis.__applyRoute();
      const pick = nodes.get(control);
      const check = async (what) => {
        const bad = await agree();
        if (bad) {
          problems.push(`${what} on #${panel}: rail says screen=${bad.got.screen} games=${bad.got.events},`
            + ` the slate holds screen=${bad.want.screen} games=${bad.want.events}`);
        }
      };
      // Set, THEN clear, and assert both. Order matters: clearing from an already-clear
      // state is a no-op that `agree()` satisfies for free, so checking the clear first
      // only ever compared '' with '' — and `if (currentLeague) scheduleNavCounts()`
      // passed, leaving "Today's games 83" in the rail beside a 1,702-game slate.
      pick.value = '';
      pick.dispatch('change');
      await agree();
      // A yield before measuring, so a nav-count timer left pending by whatever set this
      // block up cannot be the thing that renders the counts correctly. Without it the
      // pick's own poll could consume that timer and pass for code that reschedules
      // nothing, which made the `#screen` half of this check pass on broken code.
      await new Promise((resolve) => setTimeout(resolve, 0));

      pick.value = leagues[0];
      pick.dispatch('change');
      await check('picking a league');
      pick.value = '';
      pick.dispatch('change');
      await check('clearing the league again');
    }
  }

  if (problems.length) {
    console.error('A LEAGUE PICK LEAVES THE RAIL WRONG:', problems.join('; '));
    process.exit(1);
  }
  console.log('picking a league fixes both rail counts, from either panel');
}

// An open game that the newly-picked scrape does not hold must say so. The panel has
// two empty states and they are not interchangeable: "Pick a game" is true when nothing
// is open, and a lie when the breadcrumb overhead is still naming the game the reader
// clicked. Switching scrapes with a game open reaches the second one in one click.
{
  const problems = [];
  const embedded = globalThis.__embeddedRuns();
  const runs = globalThis.__DATA.runs.map((r) => r.id);
  const thin = runs.find((id) => !embedded.includes(id));
  if (!embedded.length || thin === undefined) {
    console.log('not-in-this-scrape check skipped; every run in this page is embedded');
  } else {
    const pick = nodes.get('run-pick');
    pick.value = String(embedded[0]);
    pick.dispatch('change');
    const key = globalThis.__eventKeys()[0];
    if (!key) {
      console.log('not-in-this-scrape check skipped; the embedded run holds no games');
    } else {
      globalThis.location.hash = '#fixture/' + encodeURIComponent(key);
      globalThis.__applyRoute();
      const title = () => String(((nodes.get('event-title') || {}).textContent) ?? '');
      if (!title() || /^(Pick a|Not in this)/.test(title())) {
        problems.push(`opening ${key} showed ${JSON.stringify(title())}`);
      }
      // Now switch to a scrape whose prices are not in this file, with that game open.
      pick.value = String(thin);
      pick.dispatch('change');
      if (/^Pick a/.test(title())) {
        problems.push('a game the scrape does not hold reads "Pick a game", as if nothing were open');
      }
      if (!/^Not in this scrape/.test(title())) {
        problems.push(`expected "Not in this scrape", got ${JSON.stringify(title())}`);
      }
      // The trail still names the game, so the panel has to name it too — otherwise the
      // reader is told a game is open and shown nothing that identifies it.
      const named = String(((nodes.get('event-count') || {}).textContent) ?? '');
      if (named !== key) problems.push(`the panel does not name the game it cannot show: ${JSON.stringify(named)}`);

      // The other direction, which matters just as much: with nothing open, the panel
      // must NOT claim a game is missing. Pinning only the first direction left `asked`
      // forced true passing — "Not in this scrape" and "pick another scrape" over a
      // panel where the reader had never opened anything.
      globalThis.location.hash = '#fixture/';
      globalThis.__applyRoute();
      const bare = title();
      if (/^Not in this scrape/.test(bare)) {
        problems.push('a bare #fixture with nothing open claims a game is missing');
      }
      if (String(((nodes.get('event-count') || {}).textContent) ?? '')) {
        problems.push('a bare #fixture names a game it is not showing');
      }
    }
  }

  // Put the page back on a scrape whose prices are embedded. This block deliberately
  // ends on a truncated one, and a later check reads every region expecting content —
  // leaving it here made that check fail on a page with nothing wrong with it.
  onAnEmbeddedRun();

  if (problems.length) {
    console.error('A GAME MISSING FROM A SCRAPE IS NOT REPORTED HONESTLY:', problems.join('; '));
    process.exit(1);
  }
  console.log('a game the scrape does not hold says so, instead of "Pick a game"');
}

// A chunk control must not survive its rows into the not-embedded state either. The
// Checks panel blanks four regions by hand when a scrape's prices are not in the file,
// and the control is a *sibling* — emptying the table cannot take it with it. What was
// left was "Showing 120 of 500 rows" under the not-embedded notice, and clicking it
// appended 120 of the PREVIOUS scrape's findings into the blanked table: headerless rows
// still carrying `data-go`, still navigating to the other scrape's fixtures.
{
  const problems = [];
  const embedded = globalThis.__embeddedRuns();
  const runs = globalThis.__DATA.runs.map((r) => r.id);
  const thin = runs.find((id) => !embedded.includes(id));
  if (!embedded.length || thin === undefined) {
    console.log('Checks orphan-control check skipped; every run in this page is embedded');
  } else {
    const pick = nodes.get('run-pick');
    pick.value = String(embedded[0]);
    pick.dispatch('change');
    globalThis.location.hash = '#quality';
    globalThis.__applyRoute();

    const regions = ['findings', 'overround', 'rejections', 'skips'];
    const controls = () => regions.flatMap((id) => ((nodes.get(id) || {})._siblings) || []);
    const had = controls().length;

    pick.value = String(thin);
    pick.dispatch('change');
    const left = controls();
    if (left.length) {
      problems.push(`${left.length} control(s) survived into the not-embedded state, saying ${JSON.stringify(left[0].textContent)}`);
      // And it is not merely cosmetic: it still appends.
      const host = regions.map((id) => nodes.get(id)).find((n) => n && n._siblings.length);
      const before = (host.innerHTML || '').length;
      left[0].dispatch('click');
      if ((host.innerHTML || '').length > before) {
        problems.push("clicking it appended the previous scrape's rows into the blanked table");
      }
    }
    if (!problems.length && had === 0) {
      console.log('Checks orphan-control check inconclusive; the embedded run holds too few findings to hold any back');
    }
  }

  // As above: this block ends on a truncated scrape on purpose, so restore one.
  onAnEmbeddedRun();

  if (problems.length) {
    console.error('A CHUNK CONTROL SURVIVES INTO THE NOT-EMBEDDED STATE:', problems.join('; '));
    process.exit(1);
  }
  console.log('Checks drops its controls when a scrape has no embedded prices');
}


// Prove the render actually produced markup rather than silently no-oping.
const required = ['stat-strip', 'flow', 'matrix', 'sports-grid', 'leagues-grid', 'sports-gaps',
  'source-cards', 'skips', 'coverage', 'event-detail', 'odds-table', 'runs-chart',
  'move-table', 'quality-strip', 'findings', 'overround', 'rejections', 'raws',
  'schema-table', 'vocab', 'sport-pick', 'sport-meta',
  'run-list', 'run-pick', 'scrape-status', 'nav-history',
  'home-stats', 'browse-games', 'events-games',
  'events-league', 'events-book',
  'odds-screen', 'screen-note', 'league-pick', 'market-tabs', 'nav-screen',
  'arb-list', 'arb-stats', 'arb-summary', 'arb-rejected', 'nav-arb',
  'promo-list', 'promo-stats', 'promo-summary', 'promo-health', 'nav-promos',
  'promo-scrape-status', 'promo-region', 'promo-kind', 'promo-source',
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
// Every region at once is not a state the page is ever really in — panels are built
// on arrival and unloaded when left. Walk them all so this check owns its own
// starting state rather than inheriting whatever the block above last routed to.
for (const name of Object.keys(globalThis.__PANELS)) globalThis.__visitPanel(name, null);
const blank = required.filter((id) => !filled(id));
console.log(`script ran clean; ${counted} markup writes across ${nodes.size} nodes`);

// The payload text is dropped once parsed. It is the largest single thing the page holds
// — 15.6MB of JSON for a 16-scrape dashboard — and keeping both the text and the parsed
// object doubles that for the life of the tab, for a string nothing reads again. Never
// dropping it changes nothing else that this file measures, so it needs saying here.
if (!nodes.get('report-data')._removed) {
  console.error('THE PAYLOAD TEXT IS NEVER DROPPED: the JSON island is still in the document '
    + 'after parsing, so the page holds the text and the parsed object at once');
  process.exit(1);
}
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
    // Switching runs rebuilds only the panel on screen, so the panels that carry
    // these sentences have to be arrived at — as a reader would. Walking them all
    // also checks every panel's truncated-run copy, not just whichever one the
    // page happened to be showing.
    for (const name of Object.keys(globalThis.__PANELS)) globalThis.__visitPanel(name, null);
    globalThis.__renderNavCounts();
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
  // Selects an embedded run rather than inheriting one. The truncation check above
  // leaves the picker on a run with no embedded prices and nothing put it back, so this
  // check disabled itself on every single invocation — it has never once run. On such a
  // run the strip reads "—" and a loose digit match would pick up the next stat instead,
  // which is why the guard is here at all; it just has to be a guard rather than the
  // normal case.
  //
  // And it has to route to the panel the strip lives on: only the panel on screen is
  // built now, so selecting a run alone leaves `stat-strip` holding whatever the last
  // panel to render it happened to leave there.
  onAnEmbeddedRun();
  globalThis.location.hash = '#run';
  globalThis.__applyRoute();
  const pick = nodes.get('run-pick');
  const currentId = Number(pick && pick.value);
  if (!globalThis.__detailLoaded(currentId)) {
    console.log('separate-bets check skipped; current run carries no detail');
  } else {
    const rows = ((data.quotes && data.quotes.rows) || [])
      .filter((r) => r[COL.run_id] === currentId)
      // The strip counts what the page is showing, and the page hides books that
      // cannot be bet from the US unless the switch is on. Reading the raw
      // payload here would assert the pre-toggle semantics and fail against a
      // page that is behaving correctly.
      .filter((r) => globalThis.__showOffshore()
        || !globalThis.__isUsUnavailable(data.strings[r[COL.source]]))
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

// The offshore switch has to move the whole page, not just the panel on screen.
//
// It changes which rows exist, so every count that reads `currentRows()` and the
// arbitrage bundle itself have to follow it. The failure this guards against is
// the quiet one: a switch that filters the board but leaves the arb list showing
// a position whose second leg is at a book the reader cannot reach.
{
  const data = globalThis.__DATA;
  const COL = globalThis.__COL;
  const offshoreKeys = new Set((data.sources || [])
    .filter((s) => s.us_unavailable).map((s) => s.key));

  if (!offshoreKeys.size) {
    // Nothing to move. A page whose sources are all fixtures outside the
    // registry (or all US-bettable) never disagrees with itself on this axis,
    // so there is no switch behavior here to prove one way or the other.
    console.log('offshore switch check skipped; no venue in the payload is flagged us_unavailable');
  } else {
    const problems = [];
    const box = nodes.get('offshore-toggle') || null;
    if (globalThis.__showOffshore() !== false) {
      problems.push('the page did not open in the US-only view');
    }

    // Default view: not one row, and not one arb leg, may come from a book the
    // reader cannot bet at.
    globalThis.location.hash = '#odds';
    globalThis.__applyRoute();
    const shownSources = () => new Set(globalThis.__currentRows()
      .map((r) => data.strings[r[COL.source]]));
    const leaked = [...shownSources()].filter((k) => offshoreKeys.has(k));
    if (leaked.length) {
      problems.push(`US-only view still showed rows from ${leaked.join(', ')}`);
    }
    const legsOf = (bag) => (bag ? (bag.opportunities || []) : [])
      .flatMap((o) => (o.legs || []).map((l) => l.source));
    const usOnlyLegs = legsOf(globalThis.__arbBundle());
    const badLegs = usOnlyLegs.filter((k) => offshoreKeys.has(k));
    if (badLegs.length) {
      problems.push(`US-only arbitrage used unbettable legs: ${[...new Set(badLegs)].join(', ')}`);
    }
    const usOnlyRows = globalThis.__currentRows().length;
    const usOnlyArbs = (globalThis.__arbBundle()?.opportunities || []).length;

    // Flipped on: the same page, with the offshore books admitted everywhere.
    globalThis.__setShowOffshore(true);
    globalThis.location.hash = '#odds';
    globalThis.__applyRoute();
    const withRows = globalThis.__currentRows().length;
    const withArbs = (globalThis.__arbBundle()?.opportunities || []).length;
    const present = [...shownSources()].filter((k) => offshoreKeys.has(k));
    if (withRows < usOnlyRows) {
      problems.push(`turning the switch on lost rows (${usOnlyRows} -> ${withRows})`);
    }
    if (!present.length) {
      // Only a real failure when the scrape actually holds offshore prices.
      const held = (data.quotes?.rows || []).some((r) => offshoreKeys.has(data.strings[r[COL.source]]));
      if (held) problems.push('the switch was on but no offshore rows came back');
    }
    if (withArbs < usOnlyArbs) {
      problems.push(`allowing more books removed positions (${usOnlyArbs} -> ${withArbs})`);
    }

    // And back, so the rest of the harness runs against the default view.
    globalThis.__setShowOffshore(false);
    globalThis.location.hash = '#odds';
    globalThis.__applyRoute();
    if (globalThis.__currentRows().length !== usOnlyRows) {
      problems.push('flipping the switch back did not restore the US-only view');
    }

    // Said out loud rather than left implied. This page's fixtures have usually
    // already started by the time it is built, so both bundles are empty and the
    // leg assertions above pass without proving anything. The non-vacuous check on
    // the arb side is test_the_arb_payload_carries_both_the_us_only_and_the_offshore_view,
    // which controls `as_of`; a silent "ok" here would read as cover it does not give.
    const arbNote = (usOnlyArbs || withArbs)
      ? `arbs (${usOnlyArbs} -> ${withArbs})`
      : 'arb side not exercised: no position is takeable in this page';
    console.log(problems.length ? 'OFFSHORE SWITCH WRONG: ' + problems.join('; ')
      : `offshore switch moves rows (${usOnlyRows} -> ${withRows}); ${arbNote}`);
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

// Promo plans — the detail panel renders the planner's stored numbers, verbatim.
// The case is the planner's own hand-computed conversion: $100 credit at 3.0
// hedged with $133.33 at 1.5.  Every figure asserted here is written down, not
// read back from the code that renders it.
//
// Each metric carries a DIFFERENT number on purpose.  The first version of this
// fixture set guaranteed_cash, settled_cash and conversion_pct all to 66.66, so
// rendering any one of them from any other passed — and ten separate render
// defects shipped green past this file, including the leg roles being swapped,
// which would tell the operator to put the free credit at the hedge book and
// real cash at the promo book.
{
  const problems = [];
  const plan = {
    event_key: 'MLB-PHI@MLB-MIA:2026-07-28', sport: 'baseball', league: 'MLB',
    home_team: 'Miami Marlins', away_team: 'Philadelphia Phillies',
    commence_time: '2026-07-28T22:41:00+00:00', market: 'moneyline', period: 'full_game',
    side: null, line: null,
    legs: [
      { role: 'promo', source: 'draftkings', selection: 'away', line: null,
        decimal_odds: 3.0, american_odds: 200, net_odds: 3.0, stake: 100.0,
        stake_kind: 'bonus', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
      { role: 'hedge', source: 'fanduel', selection: 'home', line: null,
        decimal_odds: 1.5, american_odds: -200, net_odds: 1.5, stake: 133.33,
        stake_kind: 'cash', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
    ],
    // A push window: the floor over every outcome is 0 while the floor over the
    // outcomes where the promo leg settles is 66.66.  Three distinct numbers.
    outcome_profits: [['away', 71.41], ['home', 71.40], ['push', 0.0]],
    guaranteed_cash: 0.0, settled_cash: 66.66, quote_age_seconds: 120,
    notes: ['priced with the stated 50% boost applied'], conversion_pct: 61.25,
  };
  globalThis.__setPromoPlans({
    'draftkings|smoke-plan': {
      strategy: 'bonus_conversion', book: ['draftkings', 'an_draftkings'],
      plans: [plan], skipped: { below_min_odds: 2 },
      caveats: ['the credit is treated as a single stake-not-returned bet'],
      unit: { kind: 'bonus_credit', amount: 100.0, assumed: false },
    },
    'betmgm_on|smoke-dry': {
      strategy: 'no_odds_coverage', book: [], plans: [], skipped: {},
      caveats: ['no odds feed covers this book'], unit: null,
    },
    // Gated-out-with-counts: the ordinary "nothing today" state.  Its caveat
    // points at the counts, so the counts have to be on the page.
    'fanduel|smoke-gated': {
      strategy: 'bonus_conversion', book: ['fanduel'], plans: [],
      skipped: { same_counterparty: 7, observation_spread: 3 },
      caveats: ["no hedgeable market on this book passed every gate; the counts in 'skipped' say what was refused and why"],
      unit: { kind: 'bonus_credit', amount: 100.0, assumed: true },
    },
  }, { odds_run_id: 7 });

  const html = globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 'smoke-plan' });
  for (const expected of [
    'Convert the credit through a real market',
    'Philadelphia Phillies at Miami Marlins',
    '$100.00', '$133.33',                    // the stakes, verbatim
    '61.3% conversion',                      // conversion_pct, not either floor
    'worst case $0.00',                      // guaranteed_cash, the all-outcomes floor
    'if it settles $66.66',                  // settled_cash, by its own wording:
                                             // a bare '$66.66' was also produced
                                             // by the outcomes line below, so the
                                             // pill could be deleted outright
    'outcomes: away $71.41',                 // the outcome table, its own numbers
    'DraftKings', 'FanDuel',                 // books by label, not key
    '+200', '−200',                          // both legs' prices
    'credit',                                // the bonus stake is tagged as credit
    'priced with the stated 50% boost applied',   // notes reach the card
    'push',                                  // every settlement outcome is listed
    'the credit is treated as a single stake-not-returned bet',  // caveats, with plans
    'gated out: below min odds ×2',
    '2m old at build',
  ]) {
    if (!html.includes(expected)) problems.push(`plan card missing ${JSON.stringify(expected)}`);
  }
  if (html.includes('NaN') || html.includes('undefined')) {
    problems.push('plan card rendered NaN/undefined');
  }
  // Roles are load-bearing: which leg takes the credit and which takes cash.
  // Asserted per row, because the strategy heading also contains the word
  // "credit" and a whole-string index comparison quietly passed on that.
  // Cut each row at its close: the trailing row would otherwise carry the
  // rest of the card, including the caveat that contains the word 'credit'.
  const rows = html.split('<tr>').slice(1).map((r) => r.split('</tr>')[0]);
  const promoRow = rows.find((r) => r.includes('DraftKings')) || '';
  const hedgeRow = rows.find((r) => r.includes('FanDuel')) || '';
  if (!promoRow || !hedgeRow) problems.push('a leg row is missing from the card');
  if (!/>promo</.test(promoRow)) problems.push("the DraftKings leg is not labelled promo");
  if (!/>hedge</.test(hedgeRow)) problems.push("the FanDuel leg is not labelled hedge");
  if (!promoRow.includes('credit')) problems.push('the promo leg is not tagged as credit');
  if (hedgeRow.includes('credit')) problems.push('the cash hedge is tagged as credit');
  if (!promoRow.includes('$100.00')) problems.push('the promo stake is not on the promo leg');
  if (!hedgeRow.includes('$133.33')) problems.push('the hedge stake is not on the hedge leg');
  // Which side each leg is on, by team.  Rows are located by book label, so
  // swapping the home/away branches of promoSelectionLabel renamed both legs
  // with the smoke green — and following the card would put the credit and the
  // cash on the SAME side of the game.
  if (!promoRow.includes('Philadelphia Phillies')) {
    problems.push('the away-selection promo leg does not name the away team');
  }
  if (!hedgeRow.includes('Miami Marlins')) {
    problems.push('the home-selection hedge leg does not name the home team');
  }

  // Counts must reach the page in the no-plans state — that is the state whose
  // own caveat promises them.
  const gated = globalThis.__promoPlanHtml({ source: 'fanduel', offer_id: 'smoke-gated' });
  if (gated.includes('plan-card')) problems.push('a gated-out offer must not render a card');
  for (const expected of ['same counterparty ×7', 'observation spread ×3']) {
    if (!gated.includes(expected)) {
      problems.push(`gated-out offer missing count ${JSON.stringify(expected)}`);
    }
  }

  const dry = globalThis.__promoPlanHtml({ source: 'betmgm_on', offer_id: 'smoke-dry' });
  if (dry.includes('plan-card')) problems.push('no-coverage offer must not render a plan card');
  if (!dry.includes('no odds feed covers this book')) {
    problems.push('no-coverage offer lost its reason');
  }
  const none = globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 'nonexistent' });
  if (none !== '') problems.push('an offer with no plan entry must render nothing extra');

  // Scraped text is data, never markup — and never a signal either.  An offer
  // whose caveat contains the literal "plan-card" must not be mistaken for one
  // that has cards, and script tags must not survive into the page.
  globalThis.__setPromoPlans({
    'draftkings|smoke-xss': {
      strategy: 'bonus_conversion', book: ['draftkings'],
      plans: [{ ...plan, home_team: '<img src=x onerror=alert(1)>' }],
      skipped: {}, caveats: ['<script>alert(2)</script>'], unit: null,
    },
    'draftkings|smoke-sniff': {
      strategy: 'bonus_conversion', book: ['draftkings'], plans: [], skipped: {},
      caveats: ['the terms mention a plan-card promotion'], unit: null,
    },
  }, { odds_run_id: 7 });
  const xss = globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 'smoke-xss' });
  if (xss.includes('<img') || xss.includes('<script>')) {
    problems.push('scraped plan text reached the page as markup');
  }
  const sniffed = globalThis.__promoDetailHtml({
    source: 'draftkings', offer_id: 'smoke-sniff', title: 'Sniff', usage_guidance: '',
  });
  if (!sniffed.includes('No strategy generated for this offer.')) {
    problems.push('an offer with no cards lost its fallback to a substring match');
  }
  // …and the mirror case, where cards really are present: the concrete legs
  // lead and the text playbook follows under its own heading.  Without this the
  // whole has-cards branch could be pinned false and nothing noticed.
  const withCards = globalThis.__promoDetailHtml({
    source: 'draftkings', offer_id: 'smoke-xss', title: 'Cards',
    usage_guidance: 'generic advice text',
  });
  if (!withCards.includes('<h4>Playbook</h4>')) {
    problems.push('an offer with cards did not demote the generic playbook');
  }
  if (withCards.includes('Best way to use')) {
    problems.push('an offer with cards still led with the generic heading');
  }
  if (!(withCards.indexOf('plan-card') < withCards.indexOf('generic advice text'))) {
    problems.push('the concrete plan does not lead the generic playbook');
  }

  // Every strategy renders its own card, its own heading and its own metric.
  // Only bonus_conversion had ever been rendered by anything, so deleting the
  // other five labels — which also gates hasCards — made every stake, leg and
  // floor those strategies compute vanish from the panel with the suite green.
  const legPair = [
    { role: 'promo', source: 'draftkings', selection: 'away', line: null,
      decimal_odds: 3.0, american_odds: 200, net_odds: 3.0, stake: 100.0,
      stake_kind: 'cash', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
    { role: 'hedge', source: 'fanduel', selection: 'home', line: null,
      decimal_odds: 1.5, american_odds: -200, net_odds: 1.5, stake: 133.33,
      stake_kind: 'cash', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
  ];
  const card = (extra) => ({ ...plan, legs: legPair, notes: [], conversion_pct: null,
                             ...extra });
  const strategyCases = [
    ['qualify_then_convert', { step: 'qualify', qualifying_cost: 0.5 },
     ['Qualify', 'step 1 · qualify', 'qualifying round-trip $0.50']],
    ['qualify_then_convert', { step: 'convert', conversion_pct: 61.25 },
     ['step 2 · convert', '61.3% conversion']],
    ['no_sweat_hedge', {}, ['Protected bet', 'refund valued at 40.0%']],
    ['boost_locked', {}, ['locks a profit']],
    ['boost_breakeven', { breakeven_boost_pct: 12.5 }, ['needs a 12.5%+ boost']],
    ['rollover_grind', { cost_per_100_wagered: 2.25 }, ['$2.25 cost per $100 wagered']],
  ];
  for (const [strategy, extra, expectations] of strategyCases) {
    globalThis.__setPromoPlans({
      'draftkings|s': {
        strategy, book: ['draftkings'], plans: [card(extra)], skipped: {},
        caveats: [], unit: { kind: 'bonus_credit', amount: 100.0, assumed: false },
        refund_conversion_pct: strategy === 'no_sweat_hedge' ? 40.0 : null,
      },
    }, { odds_run_id: 7 });
    const out = globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 's' });
    if (!out.includes('plan-card')) {
      problems.push(`${strategy} rendered no card at all`);
      continue;
    }
    for (const want of expectations) {
      if (!out.includes(want)) {
        problems.push(`${strategy} card missing ${JSON.stringify(want)}`);
      }
    }
    if (out.includes('NaN') || out.includes('undefined')) {
      problems.push(`${strategy} card rendered NaN/undefined`);
    }
  }
  // Every strategy the planner can emit must have a heading; an unlabelled one
  // silently renders nothing.
  for (const strategy of ['bonus_conversion', 'qualify_then_convert', 'no_sweat_hedge',
                          'boost_locked', 'boost_breakeven', 'rollover_grind']) {
    globalThis.__setPromoPlans({
      'draftkings|s': { strategy, book: [], plans: [card({})], skipped: {},
                        caveats: [], unit: null },
    }, { odds_run_id: 7 });
    if (!globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 's' }).includes('plan-card')) {
      problems.push(`strategy ${strategy} has no label, so its plans do not render`);
    }
  }

  // A spread and a team total.  Every plan fixture until now was a moneyline
  // with line: null and side: null, so the card's market-line and side
  // branches never executed — and both are the ones the source comments call
  // catastrophic: the away side's sign inverted, or two sides of one fixture
  // rendering character-identical.
  const spreadPlan = {
    ...plan, market: 'spread', line: -1.5, side: null,
    legs: [
      { role: 'promo', source: 'draftkings', selection: 'away', line: 1.5,
        decimal_odds: 3.0, american_odds: 200, net_odds: 3.0, stake: 100.0,
        stake_kind: 'bonus', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
      { role: 'hedge', source: 'fanduel', selection: 'home', line: -1.5,
        decimal_odds: 1.5, american_odds: -200, net_odds: 1.5, stake: 133.33,
        stake_kind: 'cash', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
    ],
    notes: [], conversion_pct: 61.25,
  };
  globalThis.__setPromoPlans({
    'draftkings|spread': {
      strategy: 'bonus_conversion', book: ['draftkings'], plans: [spreadPlan],
      skipped: {}, caveats: [], unit: null,
    },
  }, { odds_run_id: 7 });
  // The market line only — the card heading names both teams, so a substring
  // check over the whole card is satisfied by the heading whichever team the
  // side branch picked, and the plan-level line is satisfied by the per-leg one.
  const planSub = (html) => {
    const m = html.match(/<p class="plan-sub">([^<]*)</);
    return m ? m[1] : '';
  };
  const spreadHtml = globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 'spread' });
  {
    const sub = planSub(spreadHtml);
    for (const want of ['spread', '-1.5']) {
      if (!sub.includes(want)) {
        problems.push(`spread market line missing ${JSON.stringify(want)} (got ${JSON.stringify(sub)})`);
      }
    }
  }
  {
    const rows = spreadHtml.split('<tr>').slice(1).map((r) => r.split('</tr>')[0]);
    const promoRow = rows.find((r) => r.includes('DraftKings')) || '';
    const hedgeRow = rows.find((r) => r.includes('FanDuel')) || '';
    // Opposite signs, each from its own leg.  Rendering the group's canonical
    // line on both would put the hedge on the same side of the game.
    if (!promoRow.includes('+1.5')) problems.push('the away leg lost its +1.5');
    if (!hedgeRow.includes('-1.5')) problems.push('the home leg lost its -1.5');
    if (promoRow.includes('-1.5')) problems.push('the away leg carries the home sign');
  }

  const teamTotal = (side) => ({
    ...plan, market: 'team_total', line: 4.5, side,
    legs: [
      { role: 'promo', source: 'draftkings', selection: 'over', line: 4.5,
        decimal_odds: 3.0, american_odds: 200, net_odds: 3.0, stake: 100.0,
        stake_kind: 'bonus', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
      { role: 'hedge', source: 'fanduel', selection: 'under', line: 4.5,
        decimal_odds: 1.5, american_odds: -200, net_odds: 1.5, stake: 133.33,
        stake_kind: 'cash', is_alternate: false, observed_at: '2026-07-28T07:00:00+00:00' },
    ],
    notes: [], conversion_pct: 61.25,
  });
  globalThis.__setPromoPlans({
    'draftkings|tt-home': { strategy: 'bonus_conversion', book: [], skipped: {},
                            caveats: [], unit: null, plans: [teamTotal('home')] },
    'draftkings|tt-away': { strategy: 'bonus_conversion', book: [], skipped: {},
                            caveats: [], unit: null, plans: [teamTotal('away')] },
  }, { odds_run_id: 7 });
  const ttHome = globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 'tt-home' });
  const ttAway = globalThis.__promoPlanHtml({ source: 'draftkings', offer_id: 'tt-away' });
  if (ttHome === ttAway) {
    problems.push('the two sides of one team total render identically');
  }
  if (!planSub(ttHome).includes('Miami Marlins')) {
    problems.push(`the home team total names ${JSON.stringify(planSub(ttHome))}`);
  }
  if (!planSub(ttAway).includes('Philadelphia Phillies')) {
    problems.push(`the away team total names ${JSON.stringify(planSub(ttAway))}`);
  }
  if (planSub(ttHome).includes('Philadelphia')) {
    problems.push('the home team total names the away team');
  }

  // The list note separates "computed, all gated out" from "never computed".
  const noteFor = (meta) => {
    globalThis.__setPromoRun({ id: 4, started_at: '2026-07-28T07:00:00+00:00',
                               finished_at: '2026-07-28T07:01:00+00:00',
                               ok: true, offer_count: 1, source_count: 1 });
    globalThis.__setPromoOffers([{ source: 'draftkings', offer_id: 'smoke-plan',
                                   kind: 'bonus_bet', title: 'x' }]);
    globalThis.__setPromoPlans({}, meta);
    globalThis.__renderPromos();
    return nodes.get('promo-list-note')?.textContent || '';
  };
  if (!noteFor({ odds_run_id: 7 }).includes('plans priced from odds run #7')) {
    problems.push('a successful plan run is not named');
  }
  for (const [meta, want] of [
    [{ reason: 'no_odds_run' }, 'no plans: no odds run'],
    [{ reason: 'empty_odds_run', odds_run_id: 3 }, 'no plans: empty odds run'],
    [{ reason: 'planner_failed: ValueError' }, 'no plans: planner failed'],
  ]) {
    const got = noteFor(meta);
    if (!got.includes(want)) problems.push(`meta ${JSON.stringify(meta)} → ${JSON.stringify(got)}`);
  }

  if (globalThis.__promoMoney(-10) !== '−$10.00') {
    problems.push(`promoMoney(-10) → ${globalThis.__promoMoney(-10)}`);
  }
  // Put the page's own promo state back: this block fabricates a run, offers
  // and plans, and anything appended after it would otherwise render a
  // synthetic one-offer panel and believe it.
  globalThis.__restorePromos();
  console.log(problems.length
    ? 'PROMO PLAN RENDER WRONG: ' + problems.join('; ')
    : 'promo plan cards render the planner\'s numbers verbatim (6 strategies, 3 markets, 7 shapes)');
  if (problems.length) process.exit(1);
}

// ── where each leg gets placed ───────────────────────────────────────────────
// A link is the one thing on an arb card that leaves the page, so the two ways
// it can mislead are checked here: an event link that is not marked as exact
// (the reader cannot tell it apart from a league index) and any book-supplied
// string reaching the markup unescaped.
{
  const betLink = globalThis.__betLink;
  const problems = [];
  const exact = betLink({ url: 'https://sportsbook.draftkings.com/event/34475007',
                          precision: 'event', book: 'draftkings', mirrored: false });
  if (!exact.includes('bet-exact')) problems.push('an event link is not marked exact');
  if (!exact.includes('/event/34475007')) problems.push('the event url is not in the href');

  const league = betLink({ url: 'https://sportsbook.fanduel.com/navigation/mlb',
                           precision: 'league', book: 'fanduel', mirrored: false });
  if (league.includes('bet-exact')) problems.push('a league page is dressed as an exact bet');
  if (!league.includes('>league<')) problems.push('a league page does not say league');

  const mirrored = betLink({ url: 'https://sportsbook.fanduel.com/navigation/mlb',
                             precision: 'league', book: 'fanduel', mirrored: true });
  if (!mirrored.includes('aggregator')) problems.push('a mirrored price is not flagged');

  if (!betLink(null).includes('—')) problems.push('a legless link is not a dash');
  if (betLink(null).includes('undefined')) problems.push('a missing link prints undefined');

  const hostile = betLink({ url: 'https://x.test/?a="><script>alert(1)</script>',
                            precision: 'event', book: 'x"><b>', mirrored: false });
  if (hostile.includes('<script>') || hostile.includes('><b>')) {
    problems.push('a book-supplied string reached the markup unescaped');
  }
  console.log(problems.length
    ? 'BET LINK RENDER WRONG: ' + problems.join('; ')
    : 'bet links state their precision and escape book-supplied text');
  if (problems.length) process.exit(1);
}

// The fixture panel must show the game the URL names. `showCurrentPanel` skips a
// re-render when it believes the panel already holds that subject, and renderEvents
// repoints the panel at its own default whenever the Games filters change — so the
// two have to agree about what is on screen. When they did not, clicking a game you
// had opened before was skipped as redundant and the panel kept whatever the filter
// change had left there: a breadcrumb naming one game above a heading naming another.
{
  const problems = [];
  const keys = globalThis.__eventKeys();
  // The key, not the heading: two halves of a doubleheader are different games with
  // the same team names, and the panel writes the key it is showing into event-count.
  const showing = () => ((nodes.get('event-count') || {}).textContent) || '';
  const title = () => ((nodes.get('event-title') || {}).textContent) || '';

  if (keys.length < 2) {
    console.log(`fixture-identity check skipped; only ${keys.length} game(s) in this run`);
  } else {
    const open = (key) => { globalThis.location.hash = '#fixture/' + encodeURIComponent(key); globalThis.__applyRoute(); return showing(); };

    const firstKey = open(keys[0]);
    const secondKey = open(keys[1]);
    if (firstKey !== keys[0]) {
      problems.push(`routing to ${JSON.stringify(keys[0])} showed ${JSON.stringify(firstKey)}`);
    }
    if (secondKey !== keys[1]) {
      problems.push(`routing to ${JSON.stringify(keys[1])} showed ${JSON.stringify(secondKey)} ("${title()}")`);
    }

    // Now the interference: go back to Games, change a filter, then re-open the game we
    // were just on. Driven through `__renderEvents` rather than `__visitPanel`, because
    // `__visitPanel` renders only a *stale* panel — arriving from #fixture leaves Games
    // fresh, so both calls did nothing at all and the grid's markup was identical either
    // side of them. The interference this block is named for was never applied.
    globalThis.location.hash = '#events';
    globalThis.__applyRoute();
    const q = nodes.get('events-q');
    q.value = 'zzzz-no-such-team';
    globalThis.__renderEvents();
    q.value = '';
    globalThis.__renderEvents();

    const reopened = open(keys[1]);
    if (reopened !== keys[1]) {
      problems.push(`re-opening ${JSON.stringify(keys[1])} showed ${JSON.stringify(reopened)} ("${title()}")`);
    }
  }

  if (problems.length) {
    console.error('FIXTURE PANEL SHOWS THE WRONG GAME:', problems.join('; '));
    process.exit(1);
  }
  console.log('the fixture panel follows the route, even after a Games filter repoints it');
}

// Waits for a debounced render rather than sleeping a fixed amount, so a loaded
// machine cannot turn a slow timer into a failed assertion. A declaration, so a check
// placed above this line can use it — as a `const` it threw "Cannot access 'settles'
// before initialization", which is a block-order trap rather than a real failure.
async function settles(read, changedFrom, budgetMs = 3000) {
  const started = Date.now();
  while (Date.now() - started < budgetMs) {
    if (read() !== changedFrom) return read();
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return read();
}

// The typed filters are debounced, so the render happens on a timer. If that timer
// ever stops calling through, every search box on the page goes dead — typing
// changes nothing, ever — and nothing else in this file would notice.
{
  onAnEmbeddedRun();
  const problems = [];
  globalThis.__visitPanel('odds', null);
  const count = () => ((nodes.get('odds-count') || {}).textContent) || '';
  const before = count();

  const q = nodes.get('q');
  q.value = 'zzzz-no-such-team-anywhere';
  q.dispatch('input');
  if (count() !== before) problems.push('the typed filter rendered synchronously; it is meant to wait for the typing to stop');

  const after = await settles(count, before);
  if (after === before) {
    problems.push(`filter never fired: "${before}" before, "${after}" after waiting out the debounce`);
  } else if (!/^0 of 0\b/.test(after)) {
    problems.push(`filter fired but matched something: ${JSON.stringify(after)}`);
  }

  // A select is a deliberate pick, not a keystroke, and repaints at once.
  q.value = '';
  q.dispatch('input');
  const restored = await settles(count, after);
  if (restored === after) {
    problems.push(`clearing the search box never re-ran the filter; still "${after}"`);
  }
  // Judged on whether it repainted, not on whether the text changed: on a small
  // slate a filter can be a no-op and still have to run.
  const market = nodes.get('f-market');
  market.value = 'moneyline';
  const beforeWrites = counted;
  market.dispatch('input');
  if (counted === beforeWrites) {
    problems.push('picking a market did not repaint immediately');
  }

  // ...and a debounce that fires once per keystroke is not a debounce. Dropping the
  // clearTimeout leaves every character queueing its own full render, 140ms late —
  // which is the cost this change exists to remove, plus a pending render that can
  // land after the reader has navigated away. Only the trailing one should run.
  // Counted page-wide on purpose. A per-region counter cannot be used here: when the
  // search matches nothing, `table` replaces the region with an empty-state div, so
  // the node holding the count is not the node the next render writes. Nothing else
  // on the page is writing at this point in the block — no run change, no route, no
  // chrome — so the page total is the render count.
  const writes = () => counted;
  // What one render of this panel costs, measured rather than assumed — it is a
  // handful of writes across the table and its first chunk, and the exact number is
  // nobody's business here.
  const settle = async (from) => {
    await settles(writes, from);
    // Then keep waiting: the point is what arrives *after* the trailing render. With
    // one timer per keystroke they are all already scheduled and land within a few ms
    // of each other, but the extra wait means a slow machine cannot pass this by
    // simply not having delivered them yet.
    await new Promise((resolve) => setTimeout(resolve, 500));
    return writes() - from;
  };
  q.value = 'q';
  q.dispatch('input');
  const oneRender = await settle(writes());
  if (oneRender === 0) problems.push('a keystroke never rendered the board at all');

  const typed = 'lakers';
  const burstFrom = writes();
  for (const ch of typed) { q.value += ch; q.dispatch('input'); }
  const burst = await settle(burstFrom);
  // A word's worth of typing must not cost a word's worth of renders. Doubled to
  // leave room for a render whose markup differs between the two searches; six times
  // over — one full render per character — cannot hide under that.
  if (oneRender && burst > oneRender * 2) {
    problems.push(`${typed.length} keystrokes cost ${burst} writes where one render costs ${oneRender}`
      + ' — the debounce is not coalescing');
  }

  if (problems.length) {
    console.error('DEBOUNCED FILTERS DO NOT FIRE:', problems.join('; '));
    process.exit(1);
  }
  console.log(`typed filters fire after the typing stops (${typed.length} keystrokes cost ${burst} writes, one render costs ${oneRender}); selects repaint at once`);
}

// EVERY select repaints at once, not just the one the block above happens to use. The
// page's rule is "typed boxes wait for the typing to stop, deliberate picks do not", and
// checking a single control left eleven others free to be debounced by accident — a
// select that renders 140ms late feels broken, and a debounce with a shared timer can
// drop the render entirely if another control fires inside the window.
//
// The value is deliberately NOT changed. What is being asked is whether the handler
// renders synchronously, and a filter renders whether or not its value moved — so this
// separates that question from what the data happens to hold.
{
  onAnEmbeddedRun();
  const problems = [];
  // Counted on the region the control's panel owns, not page-wide: the league pickers
  // rebuild both select lists synchronously whatever else they do, so a page-wide
  // counter says "repainted" for a handler whose actual render is on a timer.
  const controls = [
    ['f-source', 'odds', 'input', ['odds-table']],
    ['f-league', 'odds', 'input', ['odds-table']],
    ['f-market', 'odds', 'input', ['odds-table']],
    ['f-period', 'odds', 'input', ['odds-table']],
    ['f-alt', 'odds', 'input', ['odds-table']],
    ['events-book', 'events', 'input', ['coverage', 'events-games']],
    ['cov-mode', 'events', 'change', ['coverage', 'events-games']],
    ['league-pick', 'screen', 'change', ['odds-screen']],
    ['events-league', 'events', 'change', ['coverage', 'events-games']],
    ['move-source', 'movement', 'change', ['move-table']],
    ['promo-kind', 'promos', 'input', ['promo-list']],
    ['promo-source', 'promos', 'input', ['promo-list']],
    ['promo-region', 'promos', 'input', ['promo-list']],
  ];
  for (const [id, panel, event, regions] of controls) {
    const node = nodes.get(id);
    if (!node || !(node._listeners[event] || []).length) {
      problems.push(`${id} has no ${event} handler at all`);
      continue;
    }
    globalThis.location.hash = '#' + panel;
    globalThis.__applyRoute();
    // EVERY owned region, not any of them: summing them meant a handler that repainted
    // one synchronously and deferred the other passed. On Games those are the coverage
    // table and the games grid, and a reader looking at the grid would see it go stale.
    const before = regions.map((r) => writesTo(r));
    node.dispatch(event);
    const after = regions.map((r) => writesTo(r));
    const stale = regions.filter((_, i) => after[i] === before[i]);
    if (stale.length) {
      problems.push(`${id} on #${panel} did not repaint ${stale.join(', ')} synchronously`);
    }
  }

  if (problems.length) {
    console.error('A DELIBERATE PICK DOES NOT REPAINT AT ONCE:', problems.join('; '));
    process.exit(1);
  }
  console.log(`all ${controls.length} select filters repaint synchronously`);
}

// Several checks below need a run whose prices are embedded — they read the event list
// before routing anywhere. Left to inherit whatever run the blocks above finished on,
// they would silently skip (a truncated run has no games) or fail spuriously (an empty
// slate makes "the filter changed the count" untrue). So they select one explicitly.
function onAnEmbeddedRun() {   // a declaration, so block order cannot matter
  const embedded = globalThis.__embeddedRuns();
  if (!embedded.length) return false;
  const pick = nodes.get('run-pick');
  pick.value = String(embedded[0]);
  pick.dispatch('change');
  return true;
}

// renderEvents must leave the fixture panel alone. It used to repoint it at its own
// top hit, to keep that panel populated while off screen — a job the router now does
// on arrival. What the repoint still did was overwrite a game the reader had opened:
// type into the Games search, click a game inside the 140ms debounce window, and the
// pending renderEvents replaced the heading and the prices with the search's top hit
// while the breadcrumb went on naming the game you clicked. Nothing corrected it,
// because nothing re-rendered.
{
  onAnEmbeddedRun();
  const keys = globalThis.__eventKeys();
  const showing = () => ((nodes.get('event-count') || {}).textContent) || '';
  const title = () => ((nodes.get('event-title') || {}).textContent) || '';

  if (keys.length < 2) {
    console.log(`fixture-repoint check skipped; only ${keys.length} game(s) in this run`);
  } else {
    globalThis.location.hash = '#fixture/' + encodeURIComponent(keys[0]);
    globalThis.__applyRoute();
    if (showing() !== keys[0]) {
      console.error(`FIXTURE PANEL DID NOT OPEN THE ROUTED GAME: wanted ${JSON.stringify(keys[0])}, showing ${JSON.stringify(showing())}`);
      process.exit(1);
    }

    // Narrow Games to a *different* game and repaint it, exactly as a pending
    // debounce would after the reader has already opened this one.
    const q = nodes.get('events-q');
    q.value = keys[1];
    globalThis.__renderEvents();
    q.value = '';

    if (showing() !== keys[0]) {
      console.error('RENDEREVENTS REPOINTED THE OPEN FIXTURE PANEL: '
        + `it now shows ${JSON.stringify(showing())} ("${title()}") while the route still names `
        + `${JSON.stringify(keys[0])} — the reader reads one game's prices under another game's name`);
      process.exit(1);
    }
    if (globalThis.__selectedEvent() !== keys[0]) {
      console.error(`RENDEREVENTS MOVED THE SELECTED GAME: ${JSON.stringify(globalThis.__selectedEvent())}, want ${JSON.stringify(keys[0])}`);
      process.exit(1);
    }
    console.log('renderEvents leaves an open fixture panel alone');
  }
}

// Leaving a panel must unload it. Building one panel at a time bounds what arriving
// costs but not what the document holds: without unloading, browsing five panels and
// scrolling their lists reached 407,670 elements — the number the eager render used
// to reach on load, just spread across a session instead of a page load.
{
  const problems = [];
  const heavy = { screen: 'odds-screen', events: 'coverage', odds: 'odds-table', raw: 'raws' };
  const order = ['screen', 'events', 'odds', 'raw'];
  // Markup only, for judging whether rows were dropped: an empty result set renders
  // as text, and text is not what unloading is trying to reclaim.
  const bulk = (id) => (((nodes.get(id) || {}).innerHTML) || '').length;
  // Anything at all, for judging whether a panel is present — an empty state counts.
  const present = (id) => {
    const n = nodes.get(id);
    return Boolean((((n && n.innerHTML) || '') + ((n && n.textContent) || '')).trim());
  };

  for (const panel of order) {
    globalThis.location.hash = '#' + panel;
    globalThis.__applyRoute();
  }
  // Everything except the last is behind us and should have been dropped.
  for (const panel of order.slice(0, -1)) {
    const size = bulk(heavy[panel]);
    if (size > 500) {
      problems.push(`#${panel} still holds ${size} chars of ${heavy[panel]} after being left`);
    }
  }
  if (!present(heavy[order[order.length - 1]])) {
    problems.push(`the panel still on screen (#${order[order.length - 1]}) was unloaded too`);
  }
  // ...and going back rebuilds it rather than showing a blank.
  globalThis.location.hash = '#' + order[0];
  globalThis.__applyRoute();
  if (!present(heavy[order[0]])) problems.push(`returning to #${order[0]} left ${heavy[order[0]]} blank`);

  if (problems.length) {
    console.error('PANELS ARE NOT UNLOADED WHEN LEFT:', problems.join('; '));
    process.exit(1);
  }
  console.log(`leaving a panel unloads it, and returning rebuilds it (${order.join(' -> ')})`);
}

// ...for EVERY panel that declares regions, not just the four above. `PANEL_HEAVY` is
// the list of what gets reclaimed, so it is the list that has to be walked: deleting its
// whole `sports` entry — leaving `sports-gaps`, the 31KB region the production comment
// calls out as the one thing Coverage never reclaimed, never reclaimed again — passed a
// suite that only knew about screen/events/odds/raw.
{
  onAnEmbeddedRun();
  const problems = [];
  const heavy = globalThis.__PANEL_HEAVY;
  const bulk = (id) => (((nodes.get(id) || {}).innerHTML) || '').length;
  const present = (id) => {
    const n = nodes.get(id);
    return Boolean((((n && n.innerHTML) || '') + ((n && n.textContent) || '')).trim());
  };
  const hop = (to) => { globalThis.location.hash = '#' + to; globalThis.__applyRoute(); };
  // Every panel that owns regions must appear here, so a panel added without an entry
  // is a visible gap rather than a silent one.
  const named = Object.keys(heavy);
  if (named.length < 12) problems.push(`PANEL_HEAVY covers only ${named.length} panels`);

  for (const panel of named) {
    hop(panel);
    const built = heavy[panel].filter(present);
    if (!built.length) {
      // Legitimately empty for this run/sport (e.g. a panel with nothing to show) —
      // say so rather than passing quietly.
      console.log(`  (#${panel} owns no filled region on this run; nothing to reclaim)`);
      continue;
    }
    // Leave sideways to a panel that shares none of its regions.
    hop(panel === 'raw' ? 'sources' : 'raw');
    const kept = built.filter((id) => bulk(id) > 500);
    if (kept.length) {
      problems.push(`#${panel} still holds ${kept.map((id) => `${id}=${bulk(id)}`).join(', ')} after being left`);
    }
    // ...and coming back rebuilds it rather than showing a blank.
    hop(panel);
    const blank = built.filter((id) => !present(id));
    if (blank.length) problems.push(`returning to #${panel} left ${blank.join(', ')} blank`);
  }

  if (problems.length) {
    console.error('SOME PANELS ARE NEVER RECLAIMED:', problems.join('; '));
    process.exit(1);
  }
  console.log(`every one of the ${named.length} panels that declares regions reclaims them`);
}

// Two panel names sharing one renderer must survive a hop between each other. This is
// the case unloading gets wrong, and it shipped: `overview` and `run` are both drawn
// by renderOverview and therefore own one set of regions, so with eviction running
// *after* the arrival render, going from How to use to This scrape left the price-mix
// matrix — the region This scrape shows — blank, under a note reading "1702 games ·
// click one to compare books". `ensurePanel('run')` had already been satisfied by the
// sibling render, so nothing rebuilt it, and toggling between the two never recovered.
//
// Checked in both directions, because the region at risk is a different one each way.
{
  const problems = [];
  // The region each name actually shows, which is the whole point: they are NOT the
  // same region, even though the two panels are rendered by one function.
  const owns = { overview: 'browse-games', run: 'matrix' };
  const filled = (id) => {
    const n = nodes.get(id);
    return (((n && n.innerHTML) || '') + ((n && n.textContent) || '')).trim().length;
  };
  const hop = (to) => { globalThis.location.hash = '#' + to; globalThis.__applyRoute(); };

  for (const [from, to] of [['overview', 'run'], ['run', 'overview']]) {
    // Arrive from somewhere else first, so `lastList` really is `from` on the hop.
    hop('arb');
    hop(from);
    if (!filled(owns[from])) problems.push(`#${from} did not fill ${owns[from]} on arrival`);
    hop(to);
    if (!filled(owns[to])) {
      problems.push(`#${from} -> #${to} left ${owns[to]} blank — the region #${to} shows`);
    }
  }
  // And a drill-down in between must not change that: a child panel leaves `lastList`
  // pointing at the list behind it, so #overview -> a game -> #run is the same hazard.
  hop('arb');
  hop('overview');
  hop('fixture');
  hop('run');
  if (!filled(owns.run)) problems.push('#overview -> a game -> #run left matrix blank');

  if (problems.length) {
    console.error('A PANEL SHARING A RENDERER IS BLANKED BY THE ONE IT SHARES WITH:', problems.join('; '));
    process.exit(1);
  }
  console.log('panels sharing a renderer survive a hop between them (overview <-> run)');
}

// Clicking a row must navigate. This change replaced thousands of per-row listeners
// with one delegated handler per container, which is the whole reason a filter
// keystroke stopped costing 50-95ms — and it was the one part of the change with no
// coverage at all: `targetOf` could be replaced with `() => null`, making every row on
// every table dead to the mouse and the keyboard, and this file stayed green. Asserting
// that `data-go="..."` appears in the markup does not test it; the markup is not the
// handler.
{
  const problems = [];
  const host = document.getElementById('click-probe');
  const rows = [];
  globalThis.__fillInChunks(host, host, ['NFL-A@NFL-B', 'NFL-C@NFL-D'],
    (key) => { rows.push(key); return `<tr class="go" data-go="#fixture/${key}"></tr>`; },
    { noun: 'games' });
  globalThis.__wireRowLinks(host);

  // The chain a browser actually hands the handler. Every clickable pixel on the board
  // is a <b> or a <span> inside a <td>, and there is a tbody and a table between the
  // <tr> and the container — so the real depth from host to the thing under the cursor
  // is six nodes. Built three deep, a `targetOf` that walks only one ancestor passed.
  const table = { dataset: {}, parentNode: host };
  const tbody = { dataset: {}, parentNode: table };
  const row = { dataset: { go: '#fixture/' + rows[1] }, parentNode: tbody };
  const cellInRow = { dataset: {}, parentNode: row };
  const textInCell = { dataset: {}, parentNode: cellInRow };

  globalThis.location.hash = '';
  host.dispatch('click', { target: row });
  if (globalThis.location.hash !== '#fixture/' + rows[1]) {
    problems.push(`clicking a row went to ${JSON.stringify(globalThis.location.hash)}, want #fixture/${rows[1]}`);
  }

  for (const [what, target] of [['a cell inside a row', cellInRow], ['text inside a cell', textInCell]]) {
    globalThis.location.hash = '';
    host.dispatch('click', { target });
    if (globalThis.location.hash !== '#fixture/' + rows[1]) {
      problems.push(`clicking ${what} went to ${JSON.stringify(globalThis.location.hash)}`);
    }
  }

  // Enter and Space on a focused row are the keyboard equivalents, and both must stop
  // the browser's own default — Space scrolls the page otherwise. Production handles
  // both; only Enter was checked, so dropping Space from the guard passed.
  for (const key of ['Enter', ' ']) {
    globalThis.location.hash = '';
    let prevented = false;
    host.dispatch('keydown', { key, target: row, preventDefault() { prevented = true; } });
    const named = key === ' ' ? 'Space' : key;
    if (globalThis.location.hash !== '#fixture/' + rows[1]) problems.push(`${named} on a row did not navigate`);
    if (!prevented) problems.push(`${named} on a row did not preventDefault`);
  }

  // Clicking the empty space around the rows must do nothing rather than navigate to
  // whatever the last row happened to be.
  globalThis.location.hash = '#odds';
  host.dispatch('click', { target: host });
  if (globalThis.location.hash !== '#odds') {
    problems.push(`clicking the container itself navigated to ${JSON.stringify(globalThis.location.hash)}`);
  }
  // A key that is not Enter or Space must not either.
  host.dispatch('keydown', { key: 'a', target: row, preventDefault() {} });
  if (globalThis.location.hash !== '#odds') problems.push('an ordinary keypress on a row navigated');

  // Idempotent across re-renders. A filter change re-runs the renderer, and a handler
  // added each time means one click firing five navigations — plus a closure per render
  // held for the life of the tab, which is what the delegation replaced.
  const beforeListeners = (host._listeners.click || []).length;
  for (let i = 0; i < 4; i += 1) globalThis.__wireRowLinks(host);
  const afterListeners = (host._listeners.click || []).length;
  if (afterListeners !== beforeListeners) {
    problems.push(`re-wiring stacked handlers: ${beforeListeners} -> ${afterListeners}`);
  }

  // ...and the wiring has to actually be applied by the two things that render rows.
  // Everything above tests `wireRowLinks` in isolation, which says nothing about whether
  // anyone calls it: removing the call from `table()` kills every drill-down row on
  // coverage, all-prices, movement, raw and the bet panel, and removing it from
  // `renderOddsScreen` kills all 1,702 board rows — and both passed.
  for (const [panel, region] of [['screen', 'odds-screen'], ['events', 'coverage']]) {
    globalThis.location.hash = '#' + panel;
    globalThis.__applyRoute();
    const live = nodes.get(region);
    const listeners = ((live && live._listeners.click) || []).length;
    if (listeners !== 1) {
      problems.push(`${region} has ${listeners} click handler(s) after rendering #${panel}, want 1`);
      continue;
    }
    // A real target out of the rendered markup, so this cannot pass on a made-up one.
    const target = (live.innerHTML.match(/data-go="([^"]+)"/) || [])[1];
    if (!target) { problems.push(`${region} rendered no clickable row`); continue; }
    // Focusable, or the keydown handler is unreachable for anyone not using a mouse.
    // Checked as the affordance rather than as one renderer's spelling: `table` writes
    // `class="go" tabindex="0" data-go=` and the board writes `data-go=… tabindex="0"`,
    // both correct, and a check pinned to either order would be a check on the wrong
    // thing. A <a> row needs no tabindex — it is focusable by being a link.
    const openTag = (live.innerHTML.match(/<(\w+)\b[^>]*\bdata-go="[^"]*"[^>]*>/) || []);
    if (openTag[1] && openTag[1].toLowerCase() !== 'a' && !/\btabindex="0"/.test(openTag[0])) {
      problems.push(`${region} rows are not focusable: ${JSON.stringify(openTag[0].slice(0, 70))}`);
    }
    const decoded = target.replace(/&amp;/g, '&').replace(/&#39;/g, "'");
    globalThis.location.hash = '';
    live.dispatch('click', { target: { dataset: { go: decoded }, parentNode: live } });
    if (globalThis.location.hash !== decoded) {
      problems.push(`a click on a real ${region} row went to ${JSON.stringify(globalThis.location.hash)},`
        + ` want ${JSON.stringify(decoded)}`);
    }
  }

  if (problems.length) {
    console.error('ROW CLICKS DO NOT NAVIGATE:', problems.join('; '));
    process.exit(1);
  }
  console.log('rows navigate by mouse and keyboard, and both renderers wire their own rows exactly once');
}

// ...but NOT the list you drilled down from. A game, a book and a bet are reached by
// clicking a row and left by going back to it. Unloading that list meant coming back
// to its first chunk: a reader who had scrolled the 1,702-game board, opened one game
// and pressed Back needed thirteen more "show more" clicks to get where they were.
{
  const problems = [];
  const rows = (id) => ((((nodes.get(id) || {}).innerHTML) || '').match(/data-go="#fixture/g) || []).length;

  globalThis.location.hash = '#screen';
  globalThis.__applyRoute();
  // Expand the board past its first chunk, as a reader scrolling would.
  for (let i = 0; i < 3; i += 1) {
    const control = (nodes.get('odds-screen') || {})._sibling;
    if (control) control.dispatch('click');
  }
  const expanded = rows('odds-screen');
  if (expanded <= globalThis.__ROW_CHUNK) {
    console.log(`drill-down state check skipped; board holds only ${expanded} row(s)`);
  } else {
    const keys = globalThis.__eventKeys();
    globalThis.location.hash = '#fixture/' + encodeURIComponent(keys[0]);
    globalThis.__applyRoute();
    globalThis.location.hash = '#screen';
    globalThis.__applyRoute();
    const afterBack = rows('odds-screen');
    if (afterBack < expanded) {
      problems.push(`board had ${expanded} rows, ${afterBack} after opening a game and going back`);
    }
  }

  if (problems.length) {
    console.error('DRILLING DOWN THROWS AWAY THE LIST BEHIND IT:', problems.join('; '));
    process.exit(1);
  }
  console.log(`drilling into a game and back keeps the board where it was (${expanded} rows)`);
}

// Leaving sideways — list, drill-down, then a *different* list — must still unload the
// first list. Keying the unload on "the last panel" instead of "the last list" left the
// expanded board resident for the rest of the session on that route.
{
  const size = (id) => (((nodes.get(id) || {}).innerHTML) || '').length;
  const keys = globalThis.__eventKeys();
  if (keys.length < 1) {
    console.log('sideways-exit check skipped; no games in this run');
  } else {
    globalThis.location.hash = '#screen';
    globalThis.__applyRoute();
    const built = size('odds-screen');
    globalThis.location.hash = '#fixture/' + encodeURIComponent(keys[0]);
    globalThis.__applyRoute();
    globalThis.location.hash = '#events';
    globalThis.__applyRoute();
    const left = size('odds-screen');
    if (left > 500) {
      console.error('LEAVING SIDEWAYS STRANDS THE FIRST LIST: '
        + `the board held ${built} chars, still ${left} after going board -> game -> Games`);
      process.exit(1);
    }
    console.log(`leaving sideways unloads the list behind the drill-down (${built} -> ${left})`);
  }
}

// A filter control shared between two panels must not build the panel nobody is
// looking at. One league pick used to write 792,544 bytes of markup, 647,882 of it
// into the off-screen odds board — which was stale anyway and rebuilt on arrival, so
// the work was waste and the markup accumulation.
{
  const problems = [];
  const size = (id) => (((nodes.get(id) || {}).innerHTML) || '').length;

  globalThis.location.hash = '#events';
  globalThis.__applyRoute();
  const before = size('odds-screen');

  const league = nodes.get('events-league');
  const options = (league.innerHTML.match(/value="([^"]+)"/g) || []).map((m) => m.slice(7, -1));
  if (!options.length) {
    console.log('cross-panel filter check skipped; no leagues to pick');
  } else {
    league.value = options[0];
    league.dispatch('change');
    const after = size('odds-screen');
    if (after > before + 500) {
      problems.push(`picking a league on Games wrote ${after - before} chars into the off-screen board`);
    }
    // And the board must still be correct when arrived at.
    globalThis.location.hash = '#screen';
    globalThis.__applyRoute();
    if (!size('odds-screen')) problems.push('the board was left blank after the league pick');
  }

  if (problems.length) {
    console.error('A FILTER BUILT A PANEL NOBODY IS LOOKING AT:', problems.join('; '));
    process.exit(1);
  }
  console.log('a shared filter marks the other panel stale instead of building it');
}

// A league or book chosen under one sport may not exist under the next. The code that
// clears an impossible choice used to run on every render because every panel
// rendered; with one panel building at a time it has to run on the run/sport change
// itself, or the nav counts read filter state no panel has reconciled — "Games 0"
// beside a scrape holding 381 of them.
{
  const problems = [];
  const bySport = globalThis.__booksBySport();
  const sportNode = nodes.get('sport-pick');
  const bookNode = nodes.get('events-book');
  const num = (id) => Number((((nodes.get(id) || {}).textContent) || '').replace(/,/g, ''));

  // A sport, and a book that prices some other sport but not this one — so leaving
  // the filter set is guaranteed to be impossible after the switch.
  // Busiest sport first, so the check lands on a sport that actually has games —
  // an empty one reads the same whether the filter was reconciled or not.
  let target = null;
  const ranked = Object.entries(bySport).sort((a, b) => b[1].rows - a[1].rows);
  for (const [sport, info] of ranked) {
    const elsewhere = ranked
      .filter(([other]) => other !== sport)
      .flatMap(([, other]) => other.books)
      .find((b) => !info.books.includes(b));
    if (elsewhere) { target = { sport, book: elsewhere }; break; }
  }

  let ran = false;
  if (!target) {
    console.log('stale-filter check skipped; every book prices every sport in this run');
  } else {
    globalThis.__visitPanel('events', null);
    bookNode.value = target.book;
    // Change the sport from somewhere else. On Games itself the panel's own renderer
    // reconciles the filter on the way past, which hides the fault entirely — the
    // reader who hits this picked the sport from the rail while reading another panel.
    globalThis.location.hash = '#arb';
    globalThis.__applyRoute();
    sportNode.value = target.sport;
    sportNode.dispatch('change');

    globalThis.__renderNavCounts();
    const navSaid = num('nav-events');
    // Now open the panel, which reconciles the filters itself, and compare.
    globalThis.__visitPanel('events', null);
    const panelSaid = Number(((((nodes.get('events-games-note') || {}).textContent) || '')
      .match(/^([\d,]+) game/)?.[1] || '0').replace(/,/g, ''));
    const shown = (((nodes.get('events-games') || {}).innerHTML) || '').match(/game-card/g) || [];

    if (!panelSaid && !shown.length) {
      console.log(`stale-filter check inert; ${target.sport} shows no games either way`);
    } else if (((ran = true)) && navSaid !== (panelSaid || shown.length)) {
      problems.push(`${target.sport} with the ${target.book} filter left set: `
        + `rail said ${navSaid}, the panel says ${panelSaid || shown.length}`);
    }

    sportNode.value = '';
    sportNode.dispatch('change');
  }

  if (problems.length) {
    console.error('NAV COUNTS READ UNRECONCILED FILTER STATE:', problems.join('; '));
    process.exit(1);
  }
  console.log(ran
    ? `the rail agrees with the panel on ${target.sport} with an impossible book filter left set`
    : 'stale-filter check did not run — nothing asserted');
}

// The Movement count is scheduled separately from the other nav counts, because it is
// the one that has to join every embedded run's rows. Scheduled work that never runs
// is a blank number beside a populated panel — and the first attempt at this used
// requestIdleCallback, which does not run in a hidden tab at all, so the count simply
// never arrived. Pinned here: it arrives, and it says what the panel says.
{
  const navMove = () => ((nodes.get('nav-move') || {}).textContent) || '';
  const sportNode = nodes.get('sport-pick');

  nodes.get('nav-move').textContent = '';
  sportNode.value = '';
  sportNode.dispatch('change');
  if (navMove() !== '') {
    console.log('movement count filled synchronously; nothing to wait for');
  } else {
    await settles(navMove, '');
    if (navMove() === '') {
      console.error('MOVEMENT COUNT NEVER ARRIVES: the rail is still blank after the schedule should have run');
      process.exit(1);
    }
  }

  // ...and it agrees with the panel that owns the number.
  const railSaid = navMove();
  globalThis.__visitPanel('movement', null);
  const panelSaid = (((nodes.get('move-count') || {}).textContent) || '').match(/^([\d,]+) of/)?.[1] || '';
  if (panelSaid && railSaid !== panelSaid) {
    console.error(`MOVEMENT COUNT DISAGREES: rail says ${railSaid}, the panel says ${panelSaid}`);
    process.exit(1);
  }
  console.log(`the movement count arrives and matches its panel (${railSaid})`);
}

// The movement memo is keyed on the sport, and on nothing else, because price movement
// is a fact about the whole embedded history rather than about the run being viewed.
// That keying is what makes a run switch free — but if it stops distinguishing sports,
// every sport silently reports the all-sports number, and the rail and the panel agree
// with each other while both being wrong.
{
  const sports = Object.keys(globalThis.__booksBySport());
  const seen = new Map();
  for (const sport of ['', ...sports]) {
    const node = nodes.get('sport-pick');
    node.value = sport;
    node.dispatch('change');
    globalThis.__movedCount();                       // force the memo for this scope
    const scope = globalThis.__movementScope();
    if (scope !== globalThis.__currentSport()) {
      console.error('MOVEMENT MEMO IS SERVING THE WRONG SCOPE: '
        + `computed for ${JSON.stringify(scope)} while the page is showing ${JSON.stringify(globalThis.__currentSport())}`);
      process.exit(1);
    }
    seen.set(sport || '(every sport)', globalThis.__movedCount());
  }
  const node = nodes.get('sport-pick');
  node.value = '';
  node.dispatch('change');

  // The label agreeing is not enough: deleting the sport filter inside the scan leaves
  // every scope reporting the all-sports number, with every label still correct. So the
  // numbers have to disagree with each other somewhere.
  const everySport = seen.get('(every sport)');
  const perSport = [...seen.entries()].filter(([k]) => k !== '(every sport)');
  if (perSport.length > 1 && everySport > 0) {
    const distinct = new Set(perSport.map(([, v]) => v));
    if (distinct.size === 1 && distinct.has(everySport)) {
      console.error('MOVEMENT MEMO IGNORES THE SPORT: '
        + `all ${perSport.length} sports report ${everySport}, the same as every sport together`);
      process.exit(1);
    }
    const total = perSport.reduce((acc, [, v]) => acc + v, 0);
    if (total > everySport) {
      console.error('MOVEMENT COUNTS DO NOT ADD UP: '
        + `sports sum to ${total}, more than the ${everySport} counted across every sport`);
      process.exit(1);
    }
  }
  console.log(`the movement memo tracks the sport (${seen.size} scopes: ${[...seen.entries()].map(([k, v]) => k + '=' + v).join(', ')})`);
}

// A filter choice that is still offered must survive its panel re-rendering. The
// selects are rebuilt from the current rows on every render (`fillSelect`), and
// replacing a select's options resets it in a browser — so `fillSelect` has to put the
// choice back. Without that line every filter silently clears itself the next time
// anything re-renders, and the reader watches their filter undo itself.
{
  onAnEmbeddedRun();
  const problems = [];
  globalThis.__visitPanel('odds', null);

  const source = nodes.get('f-source');
  const options = (source.innerHTML.match(/value="([^"]+)"/g) || []).map((m) => m.slice(7, -1));
  if (!options.length) {
    console.log('filter-persistence check skipped; no books to filter by');
  } else {
    const chosen = options[0];
    source.value = chosen;
    source.dispatch('input');
    const narrowed = ((nodes.get('odds-count') || {}).textContent) || '';
    if (source.value !== chosen) {
      problems.push(`picking ${chosen} did not stick; the select reads ${JSON.stringify(source.value)}`);
    }

    // Re-render the panel for an unrelated reason and check the choice is still there.
    globalThis.__visitPanel('events', null);
    globalThis.__visitPanel('odds', null);
    if (source.value !== chosen) {
      problems.push(`${chosen} was dropped by a re-render; the select reads ${JSON.stringify(source.value)}`);
    }
    const after = ((nodes.get('odds-count') || {}).textContent) || '';
    if (after !== narrowed) {
      problems.push(`the filtered count changed across a re-render: "${narrowed}" then "${after}"`);
    }
    source.value = '';
    source.dispatch('input');
  }

  if (problems.length) {
    console.error('A FILTER CHOICE DOES NOT SURVIVE A RE-RENDER:', problems.join('; '));
    process.exit(1);
  }
  console.log('a still-valid filter choice survives its panel re-rendering');
}

// A non-local leg must never render as the state's own price. The payload's
// per-leg `non_local_label` and per-opportunity `no_local_leg` are composed in
// Python; this block proves the page actually PAINTS them. A substring pin on
// the JS source cannot tell a rendered badge from a comment — dead-coding the
// badge while leaving its name in the file survived exactly that pin, which is
// why this executes the renderer and reads the DOM instead.
{
  onAnEmbeddedRun();
  const problems = [];
  // A leftover sport filter would make the bundle-vs-DOM comparison below
  // read a mismatch that is really the filter's doing.
  const sportNode = nodes.get('sport-pick');
  sportNode.value = '';
  sportNode.dispatch('change');
  // The flagged position may exist only in the offshore-admitted view — a
  // US-unavailable leg is dropped from the US-only bundle before locality is
  // even asked — so flip the switch on for this check and back after. And it
  // may live in any embedded run, so scan them rather than trusting the first.
  globalThis.__setShowOffshore(true);
  globalThis.location.hash = '#arb';
  globalThis.__applyRoute();
  const pick = nodes.get('run-pick');
  let opps = [];
  for (const runId of globalThis.__embeddedRuns()) {
    pick.value = String(runId);
    pick.dispatch('change');
    const bag = globalThis.__arbBundle();
    const candidates = bag ? (bag.opportunities || []) : [];
    if (candidates.some((o) => (o.legs || []).some((l) => l.non_local_label))) {
      opps = candidates;
      break;
    }
  }
  const flagged = opps.filter((o) => o.no_local_leg);
  const labelled = opps.filter((o) => (o.legs || []).some((l) => l.non_local_label));
  if (!labelled.length) {
    console.log('locality labels not exercised: no position in this page carries a non-local leg');
  } else {
    const list = ((nodes.get('arb-list') || {}).innerHTML) || '';
    const label = labelled[0].legs.find((l) => l.non_local_label).non_local_label;
    if (!list.includes(label)) {
      problems.push(`a leg labelled ${JSON.stringify(label)} rendered without its badge`);
    }
    if (flagged.length) {
      if (!list.includes('no leg reachable from this jurisdiction')) {
        problems.push('a wholly-foreign position rendered without its position-level pill');
      }
      if (!list.includes('reach from this jurisdiction')) {
        problems.push(`${flagged.length} flagged position(s) rendered with no note accounting for them`);
      }
      // The headline numbers are money claims, so a wholly-foreign position
      // must not count toward any of them — the stat tile, the summary line,
      // or the rail badge. Each is written independently in renderArb, so each
      // is pinned; the tile alone let the other two revert unnoticed.
      const statsHtml = ((nodes.get('arb-stats') || {}).innerHTML) || '';
      const takeable = opps.length - flagged.length;
      const positionsStat = statsHtml.match(/positions<\/span><b>(\d+)</);
      if (!positionsStat) {
        problems.push('the positions stat tile is missing, so the takeable count is unchecked');
      } else if (Number(positionsStat[1]) !== takeable) {
        problems.push(`the positions stat says ${positionsStat[1]}; only ${takeable} are takeable`);
      }
      const summarySaid = (((nodes.get('arb-summary') || {}).textContent) || '');
      const expectedLead = takeable ? `${takeable} takeable` : 'none takeable';
      if (!summarySaid.startsWith(expectedLead)) {
        problems.push(`the summary says ${JSON.stringify(summarySaid)}; expected it to open with ${JSON.stringify(expectedLead)}`);
      }
      const navSaid = (((nodes.get('nav-arb') || {}).textContent) || '');
      if (navSaid !== String(takeable)) {
        problems.push(`the rail badge says ${JSON.stringify(navSaid)}; only ${takeable} are takeable`);
      }
      // The rail is also written by the deferred nav-count refresher, which
      // fires on a timer AFTER renderArb's synchronous write and wins. It
      // counted the flagged positions once, silently overwriting the honest
      // badge — so the badge is read again after the timers land.
      globalThis.__renderNavCounts();
      await new Promise((resolve) => setTimeout(resolve, 0));
      const navLater = (((nodes.get('nav-arb') || {}).textContent) || '');
      if (navLater !== String(takeable)) {
        problems.push(`after the deferred nav refresh the rail badge says ${JSON.stringify(navLater)}; only ${takeable} are takeable`);
      }
    }

    // And the note must stay honest under a sport filter: pick a sport the
    // flagged position is not in, and the labels leave the page — so the note
    // has to stop claiming they are "shown below" and say the filter hid them.
    if (flagged.length) {
      const sports = new Set(opps.map((o) => o.sport));
      const options = (sportNode.innerHTML.match(/value="([^"]+)"/g) || [])
        .map((m) => m.slice(7, -1)).filter((v) => v && !sports.has(v));
      if (!options.length) {
        console.log('  (filtered-note check skipped; every sport in this page holds a flagged position)');
      } else {
        sportNode.value = options[0];
        sportNode.dispatch('change');
        const filteredList = ((nodes.get('arb-list') || {}).innerHTML) || '';
        if (!filteredList.includes('hidden by the sport filter')) {
          problems.push(`filtered to ${options[0]}, the flagged position left the page but the note does not say the filter hid it`);
        }
        if (filteredList.includes('shown below with labels')) {
          problems.push('the note claims labels are shown below while the sport filter hides every flagged card');
        }
        sportNode.value = '';
        sportNode.dispatch('change');
      }
    }
    if (problems.length) {
      console.error('LOCALITY LABELS DO NOT RENDER:', problems.join('; '));
      process.exit(1);
    }
    console.log(`locality labels render (${labelled.length} labelled, ${flagged.length} wholly foreign)`);
  }
  globalThis.__setShowOffshore(false);
}
