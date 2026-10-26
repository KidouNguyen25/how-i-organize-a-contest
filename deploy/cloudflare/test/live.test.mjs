import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createNet } from '../../../mockup/net.js';

const source = readFileSync(new URL('../../../mockup/app.js', import.meta.url), 'utf8')
  .replace("import { createNet } from './net.js';", '');

async function harness(hash, mode = 'local', deferHome = false) {
  let now = 0;
  const queue = [], calls = [], streams = [];
  const intervals = [];
  let releaseHome = null;
  const elements = new Map();
  const location = { hash };
  const doc = {
    hidden: false,
    addEventListener() {},
    querySelectorAll: () => [],
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, { innerHTML: '', classList: { toggle() {} } });
      return elements.get(selector);
    },
  };
  const setTimeout = (callback, delay) => {
    const timer = { callback, at: now + delay, stopped: false };
    queue.push(timer);
    return timer;
  };
  const clearTimeout = (timer) => { if (timer) timer.stopped = true; };
  class EventSource {
    constructor() { this.listeners = {}; streams.push(this); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const context = vm.createContext({
    createNet: (options) => createNet({ ...options, doc, now: () => now, setT: setTimeout, clearT: clearTimeout, random: () => 0 }),
    document: doc,
    window: { OTT_CONFIG: { mode }, EventSource, addEventListener() {}, scrollTo() {}, scrollY: 0 },
    location, EventSource, URLSearchParams, setTimeout, clearTimeout,
    setInterval(callback, delay) { const timer = { callback, delay, stopped: false }; intervals.push(timer); return timer; },
    clearInterval(timer) { if (timer) timer.stopped = true; },
    async fetch(path) {
      calls.push(path);
      if (deferHome && path.endsWith('/tournaments')) return new Promise((resolve) => {
        releaseHome = () => resolve({ status: 200, ok: true, headers: { get: () => 'application/json' }, json: async () => ({ items: [] }) });
      });
      const data = path.endsWith('/me') ? { error: { code: 'AUTH_REQUIRED', message: 'Log in' } }
        : path.endsWith('/tournaments') || path.endsWith('/live') ? { items: [] }
          : { users: 0, running_matches: 0 };
      return {
        status: path.endsWith('/me') ? 401 : 200, ok: !path.endsWith('/me'),
        headers: { get: () => 'application/json' }, json: async () => data,
      };
    },
  });
  const flush = () => new Promise(setImmediate);
  vm.runInContext(source, context);
  await flush();
  const advance = async (milliseconds) => {
    const end = now + milliseconds;
    for (;;) {
      const timer = queue.filter((item) => !item.stopped && item.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!timer) break;
      now = timer.at;
      timer.stopped = true;
      await timer.callback();
      await flush();
    }
    now = end;
  };
  return {
    calls, streams, advance,
    navigate: (nextHash) => { location.hash = nextHash; return vm.runInContext('route()', context); },
    releaseHome: () => releaseHome?.(),
    homePending: () => !!releaseHome,
    liveCalls: () => calls.filter((path) => path.endsWith('/live')).length,
    activeIntervals: () => intervals.filter((timer) => !timer.stopped).length,
  };
}

for (const [page, interval] of [['#/', 3000], ['#/watch', 4000]]) {
  test(`${page}: SSE suppresses polling, failures poll, reconnect refreshes missed changes`, async () => {
    const app = await harness(page);
    assert.equal(app.liveCalls(), 1);
    const stream = app.streams[0];
    stream.listeners.hello();
    await app.advance(0);
    assert.equal(app.liveCalls(), 2, 'Handshake must catch changes between the initial fetch and stream subscription');
    await app.advance(interval * 2);
    assert.equal(app.liveCalls(), 2, 'Healthy SSE must suppress periodic polling');
    stream.onerror();
    await app.advance(interval);
    assert.equal(app.liveCalls(), 3, 'Polling must recover while SSE is unavailable');
    stream.listeners.hello();
    await app.advance(0);
    assert.equal(app.liveCalls(), 4, 'Reconnect must catch events missed during disconnection');
    await app.advance(interval * 2);
    assert.equal(app.liveCalls(), 4);
  });
}

test('tunnel keeps polling without EventSource', async () => {
  const app = await harness('#/', 'tunnel');
  assert.equal(app.streams.length, 0);
  await app.advance(9999);
  assert.equal(app.liveCalls(), 1);
  await app.advance(1);
  assert.equal(app.liveCalls(), 2);
});

test('stale home response does not register clocks or network polling after navigation', async () => {
  const app = await harness('#/', 'local', true);
  for (let i = 0; i < 5 && !app.calls.some((path) => path.endsWith('/tournaments')); i++) await new Promise(setImmediate);
  assert.equal(app.homePending(), true);
  await app.navigate('#/rules');
  app.releaseHome();
  for (let i = 0; i < 3; i++) await new Promise(setImmediate);
  assert.equal(app.activeIntervals(), 0);
  await app.advance(10000);
  assert.equal(app.liveCalls(), 1);
});
