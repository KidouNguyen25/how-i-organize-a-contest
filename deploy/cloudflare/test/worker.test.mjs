import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import worker, { MAX_BODY } from '../src/worker.js';

const PUB = 'https://demo.example.workers.dev';
const env = { ORIGIN_URL: 'https://abc-def.trycloudflare.com', ORIGIN_KEY: 'k-secret', PUBLIC_ORIGIN: PUB, RUNTIME_MODE: 'tunnel', POLL_MS: '10000', ASSETS: { fetch: async () => new Response('asset') } };
let calls;
let originReply;
const realFetch = globalThis.fetch;
beforeEach(() => {
  calls = [];
  originReply = () => new Response('{"ok":true}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return originReply(url, init); };
});
const get = (path, headers = {}, e = env) => worker.fetch(new Request(PUB + path, { headers }), e);
const post = (path, body, headers = {}, e = env, method = 'POST') => worker.fetch(new Request(PUB + path, { method, headers: { Origin: PUB, 'Content-Type': 'application/json', ...headers }, body }), e);

test('asset đi thẳng vào ASSETS, không gọi origin', async () => {
  const r = await get('/index.html'); assert.equal(await r.text(), 'asset'); assert.equal(calls.length, 0);
});
test('runtime-config.js trả mode tunnel, không cache', async () => {
  const r = await get('/runtime-config.js'); const t = await r.text();
  assert.match(t, /"mode":"tunnel"/); assert.match(t, /"pollMs":10000/); assert.equal(r.headers.get('cache-control'), 'no-store');
});
test('/api/v1/stream bị chặn, không gọi origin', async () => {
  const r = await get('/api/v1/stream'); assert.equal(r.status, 404); assert.equal((await r.json()).error.code, 'STREAM_DISABLED'); assert.equal(calls.length, 0);
});
test('path ngoài allowlist: 404 JSON; method sai: 405', async () => {
  const a = await get('/api/v1/admin/dump'); assert.equal(a.status, 404); assert.equal((await a.json()).error.code, 'NOT_FOUND');
  const b = await get('/api/v2/me'); assert.equal(b.status, 404);
  const c = await post('/api/v1/me', '{}'); assert.equal(c.status, 405);
  const d = await post('/api/v1/tournaments/t1/hack', '{}'); assert.equal(d.status, 404);
  assert.equal(calls.length, 0);
});
test('header: gắn origin key, xóa key/forwarded do client gửi', async () => {
  await get('/api/v1/me', { 'X-Contest-Origin-Key': 'fake', 'X-Forwarded-For': '1.2.3.4', 'X-Forwarded-Host': 'evil', Host: 'evil', Cookie: 'ott_session=s' });
  const h = calls[0].init.headers;
  assert.equal(h.get('x-contest-origin-key'), 'k-secret'); assert.equal(h.get('x-forwarded-for'), null); assert.equal(h.get('x-forwarded-host'), null);
  assert.equal(h.get('cookie'), 'ott_session=s'); assert.ok(h.get('x-request-id'));
  assert.equal(calls[0].url, 'https://abc-def.trycloudflare.com/api/v1/me');
});
test('đích luôn lấy từ ORIGIN_URL; query được giữ', async () => {
  await get('/api/v1/tournaments?x=1', { 'X-Origin-Url': 'https://evil.test' });
  assert.equal(calls[0].url, 'https://abc-def.trycloudflare.com/api/v1/tournaments?x=1');
});
test('không theo redirect, không retry; origin redirect → 502', async () => {
  originReply = () => new Response(null, { status: 302, headers: { Location: 'https://evil.test/' } });
  const r = await get('/api/v1/me'); assert.equal(r.status, 502); assert.equal((await r.json()).error.code, 'ORIGIN_REDIRECT');
  assert.equal(calls.length, 1); assert.equal(calls[0].init.redirect, 'manual');
});
test('POST không tự retry khi origin lỗi mạng', async () => {
  globalThis.fetch = async (u, i) => { calls.push({ u, i }); throw new TypeError('network'); };
  const r = await post('/api/v1/auth/login', '{"username":"a","password":"b"}');
  assert.equal(r.status, 503); assert.equal(r.headers.get('retry-after'), '10'); assert.equal(calls.length, 1);
});
test('timeout origin → 504', async () => {
  globalThis.fetch = async () => { const e = new Error('t'); e.name = 'TimeoutError'; throw e; };
  const r = await get('/api/v1/me'); assert.equal(r.status, 504); assert.equal((await r.json()).error.code, 'ORIGIN_TIMEOUT');
});
test('mutation: thiếu hoặc sai Origin → 403, không gọi origin', async () => {
  const miss = await worker.fetch(new Request(PUB + '/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }), env);
  const bad = await post('/api/v1/auth/login', '{}', { Origin: 'https://evil.test' });
  assert.equal(miss.status, 403); assert.equal(bad.status, 403); assert.equal(calls.length, 0);
});
test('mutation hợp lệ qua được và chuyển X-CSRF-Token', async () => {
  const r = await post('/api/v1/tournaments', '{"name":"x"}', { 'X-CSRF-Token': 'tok' });
  assert.equal(r.status, 200); assert.equal(calls[0].init.headers.get('x-csrf-token'), 'tok'); assert.equal(calls[0].init.method, 'POST');
});
test('413 khi Content-Length vượt trần', async () => {
  const r = await post('/api/v1/tournaments/t1/code/check', '{}', { 'Content-Length': String(MAX_BODY + 1) });
  assert.equal(r.status, 413); assert.equal(calls.length, 0);
});
test('413 khi body stream vượt trần dù không có Content-Length', async () => {
  const chunk = new Uint8Array(256 * 1024); let n = 0;
  const stream = new ReadableStream({ pull(c) { if (n++ < 6) c.enqueue(chunk); else c.close(); } });
  const r = await worker.fetch(new Request(PUB + '/api/v1/tournaments/t1/code/check', { method: 'POST', headers: { Origin: PUB, 'Content-Type': 'application/json' }, body: stream, duplex: 'half' }), env);
  assert.equal(r.status, 413); assert.equal(calls.length, 0);
});
test('body đúng trần 1 MiB được chuyển tiếp', async () => {
  const r = await post('/api/v1/tournaments/t1/code/check', 'a'.repeat(MAX_BODY));
  assert.equal(r.status, 200); assert.equal(calls[0].init.body.byteLength, MAX_BODY);
});
test('content-type không phải JSON khi có body → 415', async () => {
  const r = await post('/api/v1/tournaments', 'x=1', { 'Content-Type': 'text/plain' }); assert.equal(r.status, 415);
});
test('origin trả HTML (index.html/trang lỗi tunnel) không lọt ra client', async () => {
  originReply = () => new Response('<html>tunnel error</html>', { status: 530, headers: { 'Content-Type': 'text/html' } });
  const a = await get('/api/v1/me'); assert.equal(a.status, 503); assert.equal(a.headers.get('content-type').includes('json'), true);
  originReply = () => new Response('<html>index</html>', { status: 200, headers: { 'Content-Type': 'text/html' } });
  const b = await get('/api/v1/me'); assert.equal(b.status, 502); assert.equal((await b.json()).error.code, 'BAD_ORIGIN_RESPONSE');
});
test('Set-Cookie: bỏ Domain, giữ HttpOnly/Secure/SameSite; response no-store', async () => {
  originReply = () => { const h = new Headers({ 'Content-Type': 'application/json' }); h.append('Set-Cookie', 'ott_session=abc; Domain=abc-def.trycloudflare.com; HttpOnly; Secure; SameSite=Lax; Path=/'); return new Response('{}', { headers: h }); };
  const r = await post('/api/v1/auth/login', '{}');
  const c = r.headers.getSetCookie()[0];
  assert.doesNotMatch(c, /domain=/i); assert.match(c, /HttpOnly/); assert.match(c, /Secure/); assert.match(c, /SameSite=Lax/);
  assert.equal(r.headers.get('cache-control'), 'no-store');
});
test('Retry-After và 429 của origin được giữ nguyên', async () => {
  originReply = () => new Response('{"error":{"code":"RATE"}}', { status: 429, headers: { 'Content-Type': 'application/json', 'Retry-After': '7' } });
  const r = await get('/api/v1/me'); assert.equal(r.status, 429); assert.equal(r.headers.get('retry-after'), '7');
});
test('thiếu ORIGIN_KEY/ORIGIN_URL → 503 ORIGIN_NOT_CONFIGURED, không gọi origin', async () => {
  const r1 = await get('/api/v1/me', {}, { ...env, ORIGIN_KEY: '' }); const r2 = await get('/api/v1/me', {}, { ...env, ORIGIN_URL: '' });
  assert.equal(r1.status, 503); assert.equal(r2.status, 503); assert.equal(calls.length, 0);
});
test('response không lộ origin key hay URL tunnel', async () => {
  const r = await get('/api/v1/me'); const dump = JSON.stringify([...r.headers]) + (await r.text());
  assert.ok(!dump.includes('k-secret')); assert.ok(!dump.includes('trycloudflare'));
});
test.after(() => { globalThis.fetch = realFetch; });
