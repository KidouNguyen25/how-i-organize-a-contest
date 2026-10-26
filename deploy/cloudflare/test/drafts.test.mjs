import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createNet } from '../../../mockup/net.js';

const source = readFileSync(new URL('../../../mockup/app.js', import.meta.url), 'utf8')
  .replace("import { createNet } from './net.js';", '');
const flush = () => new Promise(setImmediate);

async function harness(config = {}) {
  let now = 0;
  const timers = [], writes = [], compiles = [], submissions = [], templates = [], confirmations = [], elements = new Map(), stored = new Map(), pending = new Map();
  const documentListeners = new Map();
  const heldGets = new Map();
  const storedLanguages = new Map();
  function element(value = '') {
    const listeners = new Map();
    return {
      value, isConnected: true, textContent: '', innerHTML: '', hidden: true, scrollTop: 0,
      style: {}, classList: { toggle() {}, add() {}, remove() {} },
      addEventListener(name, callback) { listeners.set(name, callback); },
      dispatch(name) { listeners.get(name)?.({ target: this }); },
      dispatchEvent(event) { listeners.get(event.type)?.({ target: this }); },
    };
  }
  const doc = {
    hidden: false, addEventListener(name, callback) { documentListeners.set(name, callback); }, querySelectorAll: () => [],
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    },
  };
  function renderEditor(text) {
    for (const selector of ['#code', '#code-language', '#code-filename', '#save-state', '#gutter', '#submit-modal', '#submit-confirm', '#submit-go', '#btn-check', '#compile-log', '#compile-used']) {
      const old = elements.get(selector);
      if (old) old.isConnected = false;
      elements.set(selector, element(selector === '#code' ? text : ''));
    }
  }
  let html = '';
  Object.defineProperty(doc.querySelector('#main'), 'innerHTML', {
    get: () => html,
    set(value) {
      html = value;
      const text = value.match(/<textarea[^>]*id="code"[^>]*>([\s\S]*?)<\/textarea>/)?.[1];
      if (text !== undefined) renderEditor(text.replace(/&(amp|lt|gt|quot|#39);/g, (_, key) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[key])));
      else if (elements.has('#code')) elements.get('#code').isConnected = false;
    },
  });
  const response = (data, status = 200, type = 'application/json') => ({ status, ok: status < 400, headers: { get: () => type }, json: async () => data, text: async () => data });
  const setTimeout = (callback, delay) => {
    const timer = { callback, at: now + delay, stopped: false };
    timers.push(timer);
    return timer;
  };
  const clearTimeout = (timer) => { if (timer) timer.stopped = true; };
  const location = { hash: '#/code/t1' };
  const context = vm.createContext({
    createNet: (options) => createNet({ ...options, doc, now: () => now, setT: setTimeout, clearT: clearTimeout, random: () => 0 }),
    document: doc, location, URLSearchParams, setTimeout, clearTimeout,
    Event: class { constructor(type) { this.type = type; } },
    confirm(message) { confirmations.push(message); return config.confirm !== false; },
    setInterval: () => 1, clearInterval() {},
    window: { addEventListener() {}, scrollTo() {}, scrollY: 0 },
    async fetch(path, options) {
      if (path.endsWith('/me')) return response({ username: 'player', role: 'CONTESTANT', csrf_token: 'csrf' });
      if (path.split('?')[0].endsWith('/bot-template')) {
        return new Promise((resolve) => {
          templates.push({ language: new URLSearchParams(path.split('?')[1]).get('language') || 'cpp17', reply(text = 'template-source', status = 200, type = 'text/plain; charset=utf-8') {
            resolve(status < 400 ? response(text, status, type) : response({ error: { code: 'TEMPLATE_FAILED', message: 'Template unavailable' } }, status));
          } });
        });
      }
      const tid = path.split('/')[4];
      if (options.method === 'PUT') {
        return new Promise((resolve) => {
          const { source: text, language } = JSON.parse(options.body);
          writes.push({ tid, source: text, language, reply(status = 200) {
            if (status < 400) { stored.set(tid, text); storedLanguages.set(tid, language); }
            resolve(response(status < 400 ? { updated_at: 123 } : { error: { code: 'WRITE_FAILED', message: 'Save failed' } }, status));
          } });
        });
      }
      if (path.endsWith('/code/check')) {
        const { source: text, language } = JSON.parse(options.body);
        compiles.push({ tid, source: text, language });
        return response({ ok: true, compile_used: 1, compile_limit: 10 });
      }
      if (path.endsWith('/code/submit')) {
        return new Promise((resolve) => {
          const { source: text, language } = JSON.parse(options.body);
          submissions.push({ tid, source: text, language, reply(status = 202, accepted = status < 400) {
            if (accepted) {
              stored.set(tid, text);
              storedLanguages.set(tid, language);
              pending.set(tid, { receipt_id: 'receipt', status: 'QUEUED', received_at: 123 });
            }
            resolve(response(status < 400 ? { accepted: true, status: 'QUEUED' } : { error: { code: 'SUBMIT_FAILED', message: 'Response lost' } }, status));
          } });
        });
      }
      if (path.endsWith('/code')) {
        const data = { source: stored.get(tid) ?? `source-${tid}`, editable: !pending.has(tid), is_template: config.template && !stored.has(tid), updated_at: 100,
          language: storedLanguages.get(tid) || config.language || 'cpp17',
          languages: [{ id: 'cpp17', label: 'C++17', filename: 'bot.cpp' }, { id: 'c17', label: 'C17', filename: 'bot.c' }, { id: 'rust2021', label: 'Rust 2021', filename: 'bot.rs' }, { id: 'go', label: 'Go', filename: 'bot.go' }],
          pending_submission: pending.get(tid),
          submitted: false, compile_used: 0, compile_limit: 10, server_time: Date.now() / 1000,
          tournament: { id: tid, name: tid, status: 'CODING', coding_seconds: 600, coding_deadline: Date.now() / 1000 + 600 } };
        if (heldGets.has(tid)) await new Promise((resolve) => { heldGets.set(tid, resolve); });
        return response(data);
      }
      return response({ items: [], users: 1, running_matches: 0 });
    },
  });
  vm.runInContext(source, context);
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
    writes, compiles, submissions, templates, confirmations, stored, storedLanguages, run, advance, elements, heldGets,
    language(value) { const select = doc.querySelector('#code-language'); select.value = value; select.dispatch('change'); },
    input(text) { const editor = doc.querySelector('#code'); editor.value = text; editor.dispatch('input'); },
    save: () => run('saveDraft()'),
    async navigate(tid) { location.hash = `#/code/${tid}`; await run('route()'); await flush(); },
    status: () => doc.querySelector('#save-state').textContent,
    resetTemplate() {
      const target = { dataset: { act: 'reset-template' }, closest(selector) { return selector === '[data-act]' ? this : null; } };
      documentListeners.get('click')({ target, preventDefault() {} });
    },
  };
}

test('autosave drains the latest edit and callers wait for its acknowledgement', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  app.input('B'); await app.advance(1500);
  app.input('C'); await app.advance(1500);
  assert.equal(app.writes.length, 1);
  let settled = false;
  const saving = app.save().then((saved) => { settled = true; return saved; });
  await flush();
  assert.equal(settled, false, 'In-flight save cannot be reported as finished');
  app.writes[0].reply(); await flush();
  assert.equal(app.writes.length, 2);
  assert.equal(app.writes[1].source, 'C');
  assert.equal(settled, false);
  assert.doesNotMatch(app.status(), /^Đã lưu/);
  app.writes[1].reply();
  assert.equal(await saving, true);
  assert.equal(app.stored.get('t1'), 'C');
  assert.match(app.status(), /^Đã lưu/);
});

test('reverting while a write is pending saves the reverted source last', async () => {
  const app = await harness();
  app.input('edited'); await app.advance(1500);
  app.input('source-t1'); await app.advance(1500);
  app.writes[0].reply(); await flush();
  assert.equal(app.writes.length, 2);
  assert.equal(app.writes[1].source, 'source-t1');
  app.writes[1].reply(); await flush();
  assert.equal(app.stored.get('t1'), 'source-t1');
});

test('saving unchanged stored source sends no redundant PUT', async () => {
  const app = await harness();
  const saving = app.save();
  await flush();
  assert.equal(app.writes.length, 0);
  assert.equal(await saving, true);
});

test('failed save retains the latest text and a retry writes that text', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  const saving = app.save();
  app.input('B');
  app.writes[0].reply(500);
  assert.equal(await saving, false);
  assert.match(app.status(), /^Không lưu được/);
  const retry = app.save(); await flush();
  assert.equal(app.writes[1].source, 'B');
  app.writes[1].reply();
  assert.equal(await retry, true);
  assert.equal(app.stored.get('t1'), 'B');
});

test('submission modal waits until the newest source is saved', async () => {
  const app = await harness();
  app.input('A');
  const opening = app.run('openSubmission()'); await flush();
  app.input('B');
  app.writes[0].reply(); await flush();
  assert.equal(app.elements.get('#submit-modal').hidden, true);
  await app.advance(1500);
  assert.equal(app.writes[1].source, 'B');
  app.writes[1].reply(); await opening;
  assert.equal(app.elements.get('#submit-modal').hidden, false);
});

test('failed save blocks submission confirmation and compilation', async () => {
  for (const operation of ['openSubmission()', 'compileCheck()']) {
    const app = await harness();
    app.input('A');
    const waiting = app.run(operation); await flush();
    app.writes[0].reply(500); await waiting;
    assert.equal(app.elements.get('#submit-modal').hidden, true);
    assert.equal(app.compiles.length, 0);
  }
});

test('compilation uses the latest acknowledged source', async () => {
  const app = await harness();
  app.input('A');
  const compiling = app.run('compileCheck()'); await flush();
  app.input('B');
  app.writes[0].reply(); await flush();
  assert.equal(app.compiles.length, 0);
  await app.advance(1500);
  app.writes[1].reply(); await compiling;
  assert.deepEqual(app.compiles, [{ tid: 't1', source: 'B', language: 'cpp17' }]);
});

test('older save completion cannot mark another tournament editor as saved', async () => {
  const app = await harness();
  app.input('old-A'); await app.advance(1500);
  const oldSave = app.save();
  await app.navigate('t2');
  app.input('new-B'); await app.advance(1500);
  assert.equal(app.writes.length, 2);
  assert.equal(app.writes[1].tid, 't2');
  app.writes[0].reply(); await oldSave;
  assert.doesNotMatch(app.status(), /^Đã lưu/);
  app.writes[1].reply(); await flush();
  assert.equal(app.stored.get('t1'), 'old-A');
  assert.equal(app.stored.get('t2'), 'new-B');
});

test('navigation flushes the old debounce without writing into the next editor', async () => {
  const app = await harness();
  app.input('old-A');
  await app.navigate('t2');
  assert.equal(app.writes.length, 1);
  assert.equal(app.writes[0].tid, 't1');
  assert.equal(app.writes[0].source, 'old-A');
  await app.advance(1500);
  assert.equal(app.writes.length, 1);
  app.writes[0].reply(); await flush();
  assert.equal(app.elements.get('#code').value, 'source-t2');
});

test('stale editor GET cannot replace the current editor session', async () => {
  const app = await harness();
  app.heldGets.set('t2', true);
  const older = app.navigate('t2'); await flush();
  await app.navigate('t3');
  app.heldGets.get('t2')(); await older;
  app.input('latest'); await app.advance(1500);
  assert.equal(app.writes.length, 1);
  assert.equal(app.writes[0].tid, 't3');
  app.writes[0].reply(); await flush();
});

test('changing accounts stops the old save queue and does not restore its text', async () => {
  const app = await harness();
  app.input('old-A'); await app.advance(1500);
  app.input('old-B');
  app.run("S.me = { username: 'another', role: 'CONTESTANT' }");
  app.writes[0].reply(); await flush();
  assert.equal(app.writes.length, 1);
  app.stored.set('t1', 'another-account-source');
  await app.navigate('t1');
  assert.equal(app.elements.get('#code').value, 'another-account-source');
});

test('failed saves survive refresh of the same template editor', async () => {
  const app = await harness({ template: true });
  app.input('local-work'); await app.advance(1500);
  app.writes[0].reply(500); await flush();
  const reloading = app.navigate('t1'); await flush();
  assert.equal(app.writes.length, 2);
  app.writes[1].reply(500); await reloading;
  assert.equal(app.elements.get('#code').value, 'local-work');
  assert.match(app.status(), /^Chưa lưu/);
  await app.advance(1500);
  assert.equal(app.writes[2].source, 'local-work');
  app.writes[2].reply(); await flush();
  assert.equal(app.stored.get('t1'), 'local-work');
});

test('untouched template is saved once when explicitly requested', async () => {
  const app = await harness({ template: true });
  const saving = app.save(); await flush();
  assert.equal(app.writes[0].source, 'source-t1');
  app.writes[0].reply();
  assert.equal(await saving, true);
  assert.equal(await app.save(), true);
  assert.equal(app.writes.length, 1);
});

test('deadline stops queued writes and never reports newer unsaved text as saved', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  app.input('B');
  const saving = app.save();
  app.run('C.editable = false');
  app.writes[0].reply();
  assert.equal(await saving, false);
  assert.equal(app.writes.length, 1);
  assert.doesNotMatch(app.status(), /^Đã lưu/);
  assert.equal(await app.save(), false);
});

test('leaving before save acknowledgement cancels the queued compile request', async () => {
  const app = await harness();
  app.input('A');
  const compiling = app.run('compileCheck()'); await flush();
  await app.navigate('t2');
  app.writes[0].reply(); await compiling;
  assert.equal(app.compiles.length, 0);
  assert.equal(app.elements.get('#code').value, 'source-t2');
});

test('accepted submission with a lost response refreshes into the locked receipt', async () => {
  const app = await harness();
  const submitting = app.run('submitCode()'); await flush();
  assert.equal(app.submissions[0].tid, 't1');
  assert.equal(app.submissions[0].source, 'source-t1');
  app.submissions[0].reply(500, true);
  await submitting; await flush();
  assert.equal(app.run('C.editable'), false);
  assert.equal(app.elements.get('#code').value, 'source-t1');
});

test('edits during a request still respect debounce instead of saving each keystroke', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  app.input('B');
  app.writes[0].reply(); await flush();
  assert.equal(app.writes.length, 1);
  await app.advance(1000);
  app.input('C');
  await app.advance(1499);
  assert.equal(app.writes.length, 1);
  await app.advance(1);
  assert.equal(app.writes.length, 2);
  assert.equal(app.writes[1].source, 'C');
  app.writes[1].reply(); await flush();
  assert.equal(app.stored.get('t1'), 'C');
});

test('explicit flush resumes a queue waiting for debounce', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  app.input('B');
  app.writes[0].reply(); await flush();
  assert.equal(app.writes.length, 1);
  const saving = app.save(); await flush();
  assert.equal(app.writes.length, 2);
  assert.equal(app.writes[1].source, 'B');
  app.writes[1].reply();
  assert.equal(await saving, true);
});

test('accepted submission stops pending draft writes immediately', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  app.input('B');
  app.writes[0].reply(); await flush();
  assert.equal(app.writes.length, 1);
  const submitting = app.run('submitCode()'); await flush();
  assert.equal(app.submissions[0].source, 'B');
  app.submissions[0].reply(); await submitting; await flush();
  await app.advance(1500);
  assert.equal(app.writes.length, 1);
  assert.equal(app.run('C.editable'), false);
  assert.equal(app.stored.get('t1'), 'B');
});

test('rejected submission resumes a draft queue that was waiting for debounce', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  app.input('B');
  app.writes[0].reply(); await flush();
  const submitting = app.run('submitCode()'); await flush();
  app.submissions[0].reply(500, false); await submitting; await flush();
  assert.equal(app.writes.length, 2);
  assert.equal(app.writes[1].source, 'B');
  app.writes[1].reply(); await flush();
  assert.equal(app.stored.get('t1'), 'B');
  assert.match(app.status(), /^Đã lưu/);
});

test('reset template response stays with its original tournament editor', async () => {
  const app = await harness();
  app.resetTemplate(); await flush();
  await app.navigate('t2');
  app.templates[0].reply('old-template'); await flush();
  assert.equal(app.elements.get('#code').value, 'source-t2');
  await app.advance(1500);
  assert.equal(app.writes.length, 0);
});

test('reset template never overwrites edits made while its request is pending', async () => {
  const app = await harness();
  app.resetTemplate(); await flush();
  app.input('new-work');
  app.templates[0].reply(); await flush();
  assert.equal(app.elements.get('#code').value, 'new-work');
  await app.advance(1500);
  assert.equal(app.writes[0].source, 'new-work');
  app.writes[0].reply(); await flush();
});

test('duplicate reset clicks share one pending template request', async () => {
  const app = await harness();
  app.resetTemplate(); app.resetTemplate(); app.resetTemplate(); await flush();
  assert.equal(app.templates.length, 1);
  assert.equal(app.confirmations.length, 1);
  app.templates[0].reply('template-source'); await flush();
  assert.equal(app.elements.get('#code').value, 'template-source');
  await app.advance(1500);
  assert.equal(app.writes.length, 1);
  assert.equal(app.writes[0].source, 'template-source');
  app.writes[0].reply(); await flush();
});

test('newer confirmed reset wins even when responses arrive in reverse order', async () => {
  const app = await harness();
  app.resetTemplate(); await flush();
  app.input('intermediate-work');
  app.resetTemplate(); await flush();
  assert.equal(app.templates.length, 2);
  app.templates[1].reply('new-template'); await flush();
  app.templates[0].reply('old-template'); await flush();
  assert.equal(app.elements.get('#code').value, 'new-template');
});

test('template reset respects cancellation, editability, and account changes', async () => {
  const cancelled = await harness({ confirm: false });
  cancelled.resetTemplate(); await flush();
  assert.equal(cancelled.templates.length, 0);
  const readonly = await harness();
  readonly.run('C.editable = false');
  readonly.resetTemplate(); await flush();
  assert.equal(readonly.templates.length, 0);
  const expired = await harness();
  expired.resetTemplate(); await flush();
  expired.run('C.editable = false');
  expired.templates[0].reply(); await flush();
  assert.equal(expired.elements.get('#code').value, 'source-t1');
  const changed = await harness();
  changed.resetTemplate(); await flush();
  changed.run("S.me = { username: 'another', role: 'CONTESTANT' }");
  changed.templates[0].reply('template-source'); await flush();
  assert.equal(changed.elements.get('#code').value, 'source-t1');
});

test('template errors and unexpected JSON cannot replace source', async () => {
  for (const [body, status, type] of [[{}, 200, 'application/json'], ['unavailable', 503, 'text/plain']]) {
    const app = await harness();
    app.resetTemplate(); await flush();
    app.templates[0].reply(body, status, type); await flush();
    assert.equal(app.elements.get('#code').value, 'source-t1');
    assert.equal(app.writes.length, 0);
  }
});

test('editing then reverting still cancels an earlier reset request', async () => {
  const app = await harness();
  app.resetTemplate(); await flush();
  app.input('temporary-edit'); app.input('source-t1');
  app.templates[0].reply('template-source'); await flush();
  assert.equal(app.elements.get('#code').value, 'source-t1');
});

test('stale template failure cannot display errors on the next page', async () => {
  const app = await harness();
  app.resetTemplate(); await flush();
  await app.navigate('t2');
  app.templates[0].reply('unavailable', 503); await flush();
  assert.equal(app.elements.get('#code').value, 'source-t2');
  assert.equal(app.elements.get('#toast')?.textContent || '', '');
});

test('changing language saves the unchanged source with its new compiler', async () => {
  const app = await harness();
  app.language('c17');
  assert.equal(app.elements.get('#code').value, 'source-t1');
  assert.equal(app.elements.get('#code-filename').textContent, 'bot.c');
  await app.advance(1500);
  assert.equal(app.writes.length, 1);
  assert.equal(app.writes[0].source, 'source-t1');
  assert.equal(app.writes[0].language, 'c17');
  app.writes[0].reply(); await flush();
  assert.equal(await app.save(), true);
  assert.equal(app.writes.length, 1);
  assert.equal(app.storedLanguages.get('t1'), 'c17');
});

test('language change during a pending save writes the latest pair last', async () => {
  const app = await harness();
  app.input('A'); await app.advance(1500);
  app.language('rust2021'); app.input('B'); await app.advance(1500);
  assert.equal(app.writes.length, 1);
  app.writes[0].reply(); await flush();
  assert.equal(app.writes.length, 2);
  assert.deepEqual([app.writes[1].source, app.writes[1].language], ['B', 'rust2021']);
  app.writes[1].reply(); await flush();
  assert.equal(app.storedLanguages.get('t1'), 'rust2021');
  assert.equal(app.stored.get('t1'), 'B');
});

test('reset requests the chosen template and ignores a subsequent language change', async () => {
  const app = await harness({ language: 'c17' });
  app.resetTemplate(); await flush();
  assert.equal(app.templates[0].language, 'c17');
  app.language('rust2021');
  app.templates[0].reply('C template'); await flush();
  assert.equal(app.elements.get('#code').value, 'source-t1');
  app.resetTemplate(); await flush();
  assert.equal(app.templates[1].language, 'rust2021');
  app.templates[1].reply('Rust template'); await flush();
  assert.equal(app.elements.get('#code').value, 'Rust template');
});

test('compilation waits for acknowledgement of the selected language', async () => {
  const app = await harness();
  app.language('c17');
  const compiling = app.run('compileCheck()'); await flush();
  assert.equal(app.compiles.length, 0);
  assert.equal(app.writes[0].language, 'c17');
  app.writes[0].reply(); await compiling;
  assert.deepEqual(app.compiles, [{ tid: 't1', source: 'source-t1', language: 'c17' }]);
});

test('failed language save survives a refresh with matching source', async () => {
  const app = await harness();
  app.language('c17'); await app.advance(1500);
  app.writes[0].reply(503); await flush();
  const refresh = app.run('route(true)'); await flush();
  assert.equal(app.writes.length, 2);
  app.writes[1].reply(503); await refresh;
  assert.equal(app.run('C.language'), 'c17');
  assert.equal(app.run('C.dirty'), true);
  await app.advance(1500);
  assert.equal(app.writes[2].language, 'c17');
  app.writes[2].reply(); await flush();
  assert.equal(app.storedLanguages.get('t1'), 'c17');
});
