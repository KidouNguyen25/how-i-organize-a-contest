import { test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import legacy from '../src/worker.js';
import current from '../worker.mjs';

const publicOrigin = 'https://demo.example.workers.dev';
const env = { PUBLIC_ORIGIN: publicOrigin, ORIGIN_URL: 'https://abc-def.trycloudflare.com', ORIGIN_KEY: 'test-origin-key-'.repeat(3) };
const botSource = '/api/v1/bots/00000000-0000-0000-0000-000000000001/source';
const originalFetch = globalThis.fetch;
let originReply, requests;
beforeEach(() => {
  requests = [];
  originReply = () => new Response('{}', { headers: { 'Content-Type': 'application/json' } });
  globalThis.fetch = async (url, options) => { requests.push({ url, options }); return originReply(); };
});
after(() => { globalThis.fetch = originalFetch; });

for (const [name, worker] of [['legacy', legacy], ['current', current]]) {
  const get = (path) => worker.fetch(new Request(publicOrigin + path, { headers: { Cookie: 'ott_session=test' } }), env);

  test(`${name}: rejects cross-site mutations before calling origin`, async () => {
    const result = await worker.fetch(new Request(publicOrigin + '/api/v1/auth/login', {
      method: 'POST', headers: { Origin: publicOrigin, 'Sec-Fetch-Site': 'cross-site', 'Content-Type': 'application/json' }, body: '{}',
    }), env);
    assert.equal(result.status, 403);
    assert.equal(requests.length, 0);
  });

  test(`${name}: forwards fetch metadata without trusting proxy identity`, async () => {
    const result = await worker.fetch(new Request(publicOrigin + '/api/v1/auth/login', {
      method: 'POST', headers: { Origin: publicOrigin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json',
        'X-Contest-Origin-Key': 'forged', 'X-Contest-Client-IP': 'forged' }, body: '{}',
    }), env);
    assert.equal(result.status, 200);
    const headers = requests[0].options.headers;
    assert.equal(headers.get('Sec-Fetch-Site'), 'same-origin');
    assert.equal(headers.get('X-Contest-Origin-Key'), env.ORIGIN_KEY);
    assert.notEqual(headers.get('X-Contest-Client-IP'), 'forged');
  });

  test(`${name}: serves bot template and authenticated source as plain text`, async () => {
    const text = '#include <iostream>\n// Mã nguồn\nconst char* html = "<html>";\n';
    originReply = () => new Response(text, { headers: { 'Content-Type': 'Text/Plain; charset=utf-8' } });
    for (const path of ['/api/v1/bot-template', botSource]) {
      const result = await get(path);
      assert.equal(result.status, 200);
      assert.equal(await result.text(), text);
      assert.equal(result.headers.get('cache-control'), 'no-store');
      assert.equal(result.headers.get('x-content-type-options'), 'nosniff');
      assert.equal(requests.at(-1).options.headers.get('cookie'), 'ott_session=test');
    }
  });

  test(`${name}: plain text stays unavailable on JSON endpoints`, async () => {
    originReply = () => new Response('unexpected', { headers: { 'Content-Type': 'text/plain' } });
    const result = await get('/api/v1/me');
    assert.equal(result.status, 502);
    assert.equal((await result.json()).error.code, 'BAD_ORIGIN_RESPONSE');
  });

  test(`${name}: HTML and false JSON media types never become bot source`, async () => {
    for (const type of ['text/html', 'application/octet-stream', 'application/not-json']) {
      originReply = () => new Response('<html>tunnel error</html>', { headers: { 'Content-Type': type } });
      const result = await get('/api/v1/bot-template');
      assert.equal(result.status, 502, type);
      assert.equal((await result.json()).error.code, 'BAD_ORIGIN_RESPONSE');
    }
    originReply = () => new Response('down', { status: 530, headers: { 'Content-Type': 'text/plain' } });
    assert.equal((await get('/api/v1/bot-template')).status, 503);
  });

  test(`${name}: text endpoints preserve structured authorization errors`, async () => {
    for (const type of ['application/json; charset=utf-8', 'application/problem+json']) {
      originReply = () => new Response('{"error":{"code":"FORBIDDEN"}}', { status: 403, headers: { 'Content-Type': type } });
      const result = await get(botSource);
      assert.equal(result.status, 403);
      assert.equal((await result.json()).error.code, 'FORBIDDEN');
    }
  });

  test(`${name}: rejected origin bodies are cancelled`, async () => {
    for (const status of [200, 302]) {
      let cancelled = 0;
      const body = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('unexpected')); }, cancel() { cancelled++; } });
      originReply = () => new Response(body, { status, headers: { 'Content-Type': 'text/html', Location: 'https://evil.example' } });
      const result = await get('/api/v1/bot-template');
      assert.equal(result.status, 502);
      assert.equal(cancelled, 1);
    }
  });

  test(`${name}: JSON success and empty responses remain valid`, async () => {
    originReply = () => new Response('{"username":"player"}', { headers: { 'Content-Type': 'Application/JSON; charset=utf-8' } });
    const result = await get('/api/v1/me');
    assert.equal(result.status, 200);
    assert.deepEqual(await result.json(), { username: 'player' });
    originReply = () => new Response(null, { status: 204 });
    const empty = await get('/api/v1/me');
    assert.equal(empty.status, 204);
    assert.equal(await empty.text(), '');
  });
}
