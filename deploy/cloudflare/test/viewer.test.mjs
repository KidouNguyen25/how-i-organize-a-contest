import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createNet } from '../../../mockup/net.js';

const source = readFileSync(new URL('../../../mockup/app.js', import.meta.url), 'utf8')
  .replace("import { createNet } from './net.js';", '');
const flush = () => new Promise(setImmediate);
const match = (id, overrides = {}) => ({
  id, status: 'RUNNING', result: null, result_reason: null, state_version: 1, move_count: 0, moves: [], placements: [], revealed: false,
  placed: { white: 0, black: 0 }, white: { username: 'white' }, black: { username: 'black' },
  clocks_ms: { white: 300000, black: 300000 }, clock_seconds: 300, tournament_name: 'Cup',
  leg_no: 1, round_no: 1, viewer_is_player: false, ...overrides,
});

async function harness(mode = 'local') {
  let now = 0;
  const timers = [], intervals = [], requests = [], streams = [], paints = [], elements = new Map(), listeners = new Map();
  const deferredFetchPaths = new Set(), deferredFetches = [];
  const element = () => ({ innerHTML: '', textContent: '', scrollTop: 0, scrollHeight: 0,
    style: {}, classList: { toggle() {}, add() {}, remove() {} }, querySelector: () => null });
  const document = {
    hidden: false,
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(listener);
    },
    querySelectorAll: () => [],
    querySelector(selector) {
      if (selector === '.round' && !elements.get('#main')?.innerHTML.includes('class="round"')) return null;
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    },
  };
  const setTimeout = (callback, delay) => {
    const timer = { callback, at: now + delay, stopped: false };
    timers.push(timer);
    return timer;
  };
  const clearTimeout = (timer) => { if (timer) timer.stopped = true; };
  class EventSource {
    constructor() { this.listeners = {}; streams.push(this); }
    addEventListener(name, listener) { this.listeners[name] = listener; }
  }
  class ClockDate extends Date { static now() { return now; } }
  const location = { hash: '#/game/g1' };
  const response = (data, status = 200, retry = 0) => ({
    status, ok: status < 400,
    headers: { get: (name) => name === 'Retry-After' ? String(retry) : 'application/json' }, json: async () => data,
  });
  const context = vm.createContext({
    createNet: (options) => createNet({ ...options, doc: document, now: () => now,
      setT: setTimeout, clearT: clearTimeout, random: () => 0 }),
    document, location, EventSource, Date: ClockDate, URLSearchParams, setTimeout, clearTimeout,
    window: { OTT_CONFIG: { mode }, EventSource, addEventListener() {}, scrollTo() {}, scrollY: 0 },
    setInterval(callback, delay) { const timer = { callback, delay, stopped: false }; intervals.push(timer); return timer; },
    clearInterval(timer) { timer.stopped = true; },
    recordPaint(id) { paints.push(id); },
    async fetch(path) {
      if (deferredFetchPaths.has(path)) return new Promise((resolve) => deferredFetches.push({ path, resolve }));
      if (path.endsWith('/me')) return response({ error: { code: 'AUTH_REQUIRED' } }, 401);
      if (path.includes('/matches/')) {
        const url = new URL(path, 'http://test');
        const id = url.pathname.split('/').at(-1);
        return new Promise((resolve) => requests.push({ id, since: url.searchParams.get('since'), reply(data = match(id), status = 200, retry = 0) {
          resolve(response(data, status, retry));
        } }));
      }
      return response({ items: [], users: 0, running_matches: 0 });
    },
  });
  vm.runInContext(source, context);
  vm.runInContext('const originalPaint = paintGame; paintGame = () => { if ($(".round")) recordPaint(G.m?.id); originalPaint(); };', context);
  await flush();
  const run = (code) => vm.runInContext(code, context);
  const advance = async (milliseconds) => {
    const end = now + milliseconds;
    for (;;) {
      const timer = timers.filter((item) => !item.stopped && item.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!timer) break;
      timer.stopped = true;
      now = timer.at;
      timer.callback();
      await flush();
    }
    now = end;
  };
  return {
    run, requests, paints, streams, advance,
    deferFetch(path) { deferredFetchPaths.add(path); },
    replyFetch(path, data, status = 200, retry = 0) {
      const index = deferredFetches.findIndex((request) => request.path === path);
      assert.notEqual(index, -1, `No deferred request for ${path}`);
      deferredFetches.splice(index, 1)[0].resolve(response(data, status, retry));
    },
    async initial(data = match('g1')) { requests[0].reply(data); await flush(); },
    navigate(id, soft = false) { location.hash = `#/game/${id}`; const pending = run(`route(${soft})`); return pending; },
    async hidden(value) { document.hidden = value; for (const listener of listeners.get('visibilitychange') || []) listener(); await flush(); },
    activeIntervals: () => intervals.filter((item) => !item.stopped).length,
  };
}

test('stale initial match response cannot replace the newer game or register polls', async () => {
  const app = await harness();
  const navigating = app.navigate('g2'); await flush();
  app.requests[1].reply(); await navigating;
  app.requests[0].reply(); await flush();
  assert.equal(app.run('G.m.id'), 'g2');
  await app.advance(1000);
  assert.deepEqual(app.requests.map((item) => item.id), ['g1', 'g2', 'g2']);
  assert.equal(app.activeIntervals(), 1);
});

test('stale refresh cannot overwrite a new match or refresh the old match again', async () => {
  const app = await harness(); await app.initial();
  const refreshing = app.run('G.refresh()'); await flush();
  app.run('G.refresh()');
  const navigating = app.navigate('g2'); await flush();
  app.requests[2].reply(); await navigating;
  app.requests[1].reply(match('g1', { placed: { white: 1, black: 0 } })); await refreshing; await flush();
  assert.equal(app.run('G.m.id'), 'g2');
  assert.deepEqual(app.requests.map((item) => item.id), ['g1', 'g1', 'g2']);
  assert.notEqual(app.paints.at(-1), 'g1');
});

test('finished replay starts no network polling or clock intervals', async () => {
  const app = await harness();
  await app.initial(match('g1', { status: 'FINISHED', result: 'DRAW', result_reason: 'REPETITION' }));
  await app.advance(60000);
  assert.equal(app.requests.length, 1);
  assert.equal(app.activeIntervals(), 0);
  assert.equal(app.run('G.refresh'), null);
});

test('finish response discards a queued refresh and paints only once', async () => {
  const app = await harness(); await app.initial(); await app.advance(0);
  const before = app.paints.length;
  const first = app.run('G.refresh()'); await flush();
  app.run('G.refresh()');
  app.requests[1].reply(match('g1', { status: 'FINISHED', result: 'DRAW', result_reason: 'REPETITION' }));
  await first; await flush(); await app.advance(60000);
  assert.equal(app.requests.length, 2);
  assert.equal(app.paints.length, before + 1);
  assert.equal(app.run('G.refresh'), null);
  assert.equal(app.activeIntervals(), 0);
  assert.equal(app.run('$("#game-status").textContent'), 'KẾT THÚC');
});

test('unchanged state poll sends compact delta and preserves replay snapshots', async () => {
  const app = await harness(); await app.initial(match('g1', { state_version: 7 }));
  const positions = app.run('G.positions'), paints = app.paints.length;
  const refreshing = app.run('G.refresh()'); await flush();
  assert.equal(app.requests[1].since, '7');
  app.requests[1].reply({ not_modified: true, state_version: 7, status: 'RUNNING', result: null,
    result_reason: null, clocks_ms: { white: 299000, black: 300000 }, running_side: 'white' });
  await refreshing;
  assert.equal(app.run('G.positions'), positions);
  assert.equal(app.run('G.m.clocks_ms.white'), 299000);
  assert.equal(app.paints.length, paints);
});

test('compact delta still applies terminal result and stops viewer timers', async () => {
  const app = await harness(); await app.initial(match('g1', { state_version: 3 }));
  const refreshing = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ not_modified: true, state_version: 3, status: 'FINISHED', result: 'DRAW',
    result_reason: 'REPETITION', clocks_ms: { white: 290000, black: 290000 }, running_side: null });
  await refreshing; await app.advance(60000);
  assert.equal(app.run('G.m.result_reason'), 'REPETITION');
  assert.equal(app.run('G.refresh'), null);
  assert.equal(app.activeIntervals(), 0);
  assert.equal(app.run('$("#game-status").textContent'), 'KẾT THÚC');
});

test('SSE refresh waits while hidden and resumes once when visible', async () => {
  const app = await harness(); await app.initial();
  const stream = app.streams[0];
  await app.hidden(true);
  for (let index = 0; index < 10; index++) stream.onmessage({ data: JSON.stringify({ type: 'match', id: 'g1' }) });
  await flush(); await app.advance(10000);
  assert.equal(app.requests.length, 1);
  await app.hidden(false); await app.advance(0);
  assert.equal(app.requests.length, 2);
  app.requests[1].reply(); await flush();
});

test('SSE refresh respects shared Retry-After backoff', async () => {
  const app = await harness(); await app.initial();
  const refreshing = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ error: { code: 'BUSY' } }, 429, 5); await refreshing;
  for (let index = 0; index < 10; index++) app.run('G.refresh()');
  await flush(); await app.advance(4999);
  assert.equal(app.requests.length, 2);
  await app.advance(1);
  assert.equal(app.requests.length, 3);
  app.requests[2].reply(); await flush();
});

test('late success from older request cannot clear newer Retry-After backoff', async () => {
  const app = await harness(); await app.initial();
  app.deferFetch('/api/v1/live');
  const older = app.run("api('GET', '/live')"); await flush();
  const failure = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ error: { code: 'BUSY' } }, 429, 5);
  await failure;
  const until = app.run('NET.state.until');
  app.replyFetch('/api/v1/live', { items: [] }); await older;
  assert.equal(app.run('NET.state.until'), until);
  assert.equal(app.run('NET.state.fails'), 1);
});

test('active refresh callers share one promise and one follow-up snapshot', async () => {
  const app = await harness(); await app.initial();
  const first = app.run('G.refresh()');
  const second = app.run('G.refresh()');
  assert.equal(first, second);
  await flush();
  assert.equal(app.requests.length, 2);
  app.requests[1].reply(); await flush();
  assert.equal(app.requests.length, 3);
  let settled = false;
  first.then(() => { settled = true; });
  await flush(); assert.equal(settled, false);
  app.requests[2].reply(match('g1', { placed: { white: 1, black: 0 } }));
  await first;
  assert.equal(app.run('G.m.placed.white'), 1);
});

test('periodic polling joins an existing request without scheduling another snapshot', async () => {
  const app = await harness(); await app.initial();
  const refreshing = app.run('G.refresh()'); await flush();
  await app.advance(1000);
  assert.equal(app.requests.length, 2);
  app.requests[1].reply(); await refreshing; await flush();
  assert.equal(app.requests.length, 2);
});

test('stale finished response cannot disable the new game refresh', async () => {
  const app = await harness(); await app.initial();
  const refreshing = app.run('G.refresh()'); await flush();
  const navigating = app.navigate('g2'); await flush();
  app.requests[2].reply(); await navigating;
  app.requests[1].reply(match('g1', { status: 'FINISHED', result: 'DRAW', result_reason: 'REPETITION' }));
  await refreshing;
  assert.equal(app.run('G.m.id'), 'g2');
  assert.equal(app.run('typeof G.refresh'), 'function');
  await app.advance(1000);
  assert.equal(app.requests.at(-1).id, 'g2');
  app.requests.at(-1).reply(); await flush();
});

test('leaving the game before initial fetch completes registers no viewer work', async () => {
  const app = await harness();
  await app.run("location.hash = '#/rules'; route()");
  app.requests[0].reply(); await flush(); await app.advance(60000);
  assert.equal(app.run('G.refresh'), null);
  assert.equal(app.requests.length, 1);
  assert.equal(app.activeIntervals(), 0);
});

test('soft navigation to the same match still rejects the earlier response', async () => {
  const app = await harness();
  const navigating = app.navigate('g1', true); await flush();
  app.requests[1].reply(match('g1', { placed: { white: 2, black: 0 } })); await navigating;
  app.requests[0].reply(); await flush();
  assert.equal(app.run('G.m.placed.white'), 2);
  assert.equal(app.activeIntervals(), 1);
});

test('tunnel viewer retains the ten second polling minimum', async () => {
  const app = await harness('tunnel'); await app.initial();
  assert.equal(app.streams.length, 0);
  await app.advance(9999); assert.equal(app.requests.length, 1);
  await app.advance(1); assert.equal(app.requests.length, 2);
  app.requests[1].reply(); await flush();
});

test('replay selection and board orientation survive a current match refresh', async () => {
  const initial = match('g1', {
    revealed: true, placed: { white: 10, black: 10 }, move_count: 1,
    placements: [{ side: 'white', piece: 'king', to: 'a1' }, { side: 'black', piece: 'king', to: 'i9' }],
    moves: [{ side: 'white', piece: 'king', from: 'a1', to: 'b1' }],
  });
  const app = await harness(); await app.initial(initial);
  app.run('G.ply = 0; G.flip = true;');
  const refreshing = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ ...initial, move_count: 2,
    moves: [...initial.moves, { side: 'black', piece: 'king', from: 'i9', to: 'h9' }] });
  await refreshing;
  assert.equal(app.run('G.ply'), 0);
  assert.equal(app.run('G.flip'), true);
  assert.equal(app.run('G.positions.length'), 3);
});

test('clock refresh reuses the reconstructed replay history', async () => {
  const initial = match('g1', { revealed: true, placed: { white: 10, black: 10 }, move_count: 1,
    placements: [{ side: 'white', piece: 'king', to: 'a1' }],
    moves: [{ n: 1, side: 'white', piece: 'king', from: 'a1', to: 'b1', promotion: null, captured: null }] });
  const app = await harness(); await app.initial(initial);
  const positions = app.run('G.positions'), paints = app.paints.length;
  const refreshing = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ ...initial, clocks_ms: { white: 299000, black: 300000 } });
  await refreshing;
  assert.equal(app.run('G.positions'), positions);
  assert.equal(app.paints.length, paints);
});

test('new move extends replay history and preserves existing board snapshots', async () => {
  const initial = match('g1', { revealed: true, placed: { white: 10, black: 10 }, move_count: 1,
    placements: [{ side: 'white', piece: 'king', to: 'a1' }, { side: 'black', piece: 'king', to: 'i9' }],
    moves: [{ n: 1, side: 'white', piece: 'king', from: 'a1', to: 'b1', promotion: null, captured: null }] });
  const app = await harness(); await app.initial(initial);
  const positions = app.run('G.positions'), start = app.run('G.positions[0]'), afterWhite = app.run('G.positions[1]');
  const refreshing = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ ...initial, move_count: 2, moves: [...initial.moves,
    { n: 2, side: 'black', piece: 'king', from: 'i9', to: 'h9', promotion: null, captured: null }] });
  await refreshing;
  assert.equal(app.run('G.positions.length'), 3);
  assert.equal(app.run('G.positions'), positions);
  assert.equal(app.run('G.positions[0]'), start);
  assert.equal(app.run('G.positions[1]'), afterWhite);
  assert.ok(app.run('G.positions[2].h9'));
});

test('shortened match history truncates cached positions and keeps replay selection in range', async () => {
  const initial = match('g1', { revealed: true, placed: { white: 10, black: 10 }, move_count: 2,
    placements: [{ side: 'white', piece: 'king', to: 'a1' }],
    moves: [
      { n: 1, side: 'white', piece: 'king', from: 'a1', to: 'b1', promotion: null, captured: null },
      { n: 2, side: 'white', piece: 'king', from: 'b1', to: 'c1', promotion: null, captured: null },
    ] });
  const app = await harness(); await app.initial(initial);
  app.run('G.ply = 1');
  const positions = app.run('G.positions'), firstMove = app.run('G.positions[1]');
  const refreshing = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ ...initial, move_count: 1, moves: initial.moves.slice(0, 1) });
  await refreshing;
  assert.equal(app.run('G.positions'), positions);
  assert.equal(app.run('G.positions.length'), 2);
  assert.equal(app.run('G.positions[1]'), firstMove);
  assert.equal(app.run('G.ply'), 1);
});

test('corrected move rebuilds and repaints replay history at same move count', async () => {
  const initial = match('g1', { revealed: true, placed: { white: 10, black: 10 }, move_count: 1,
    placements: [{ side: 'white', piece: 'king', to: 'a1' }],
    moves: [{ n: 1, side: 'white', piece: 'king', from: 'a1', to: 'b1', promotion: null, captured: null }] });
  const app = await harness(); await app.initial(initial);
  const paints = app.paints.length;
  const refreshing = app.run('G.refresh()'); await flush();
  app.requests[1].reply({ ...initial, moves: [{ ...initial.moves[0], to: 'c1' }] });
  await refreshing;
  assert.equal(app.run('G.positions[1].c1.p'), 'v');
  assert.ok(app.paints.length > paints);
});

test('participant view remains usable if the profile lookup failed', async () => {
  const app = await harness(); await app.initial(match('g1', { viewer_is_player: true }));
  assert.equal(app.run('S.me'), null);
  assert.match(app.run('$("#main").innerHTML'), /class="round"/);
  assert.match(app.run('$("#main").innerHTML'), /Bot của bạn tự đấu/);
});
