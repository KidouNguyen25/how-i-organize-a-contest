#!/usr/bin/env node
// Smoke/nghiệm thu qua portal công khai (C06: AC08, AC09-phần-web, AC11-phần-web).
// Dùng: PORTAL_URL=https://... [ORIGIN_URL=https://...trycloudflare.com] [ORIGIN_KEY=...] [OTT_USER=.. OTT_PASS=..] node smoke.mjs
// Ghi biên bản JSON vào evidence/ (không ghi mật khẩu, cookie hay key).
import { mkdirSync, writeFileSync } from 'node:fs';
import { execSync } from 'node:child_process';

const PORTAL = (process.env.PORTAL_URL || '').replace(/\/+$/, '');
if (!PORTAL) { console.error('Thiếu PORTAL_URL'); process.exit(2); }
const ORIGIN = (process.env.ORIGIN_URL || '').replace(/\/+$/, '');
const KEY = process.env.ORIGIN_KEY || '';
const results = [];
async function check(id, title, fn) {
  try { const detail = await fn(); results.push({ id, title, pass: true, detail: detail ?? '' }); console.log(`PASS ${id} ${title}`); }
  catch (e) { results.push({ id, title, pass: false, detail: String(e.message || e) }); console.log(`FAIL ${id} ${title}: ${e.message || e}`); }
}
const ok = (c, m) => { if (!c) throw new Error(m); };
const j = async (r) => { try { return await r.json(); } catch { return null; } };

await check('W01', 'runtime-config.js ở chế độ tunnel, không cache', async () => {
  const r = await fetch(PORTAL + '/runtime-config.js'); const t = await r.text();
  ok(r.status === 200 && t.includes('"mode":"tunnel"'), `status=${r.status} body=${t.slice(0, 80)}`); ok((r.headers.get('cache-control') || '').includes('no-store'), 'thiếu no-store');
});
await check('W02', '/api/v1/stream bị chặn (Quick Tunnel không hỗ trợ SSE)', async () => {
  const r = await fetch(PORTAL + '/api/v1/stream'); ok(r.status === 404, `status=${r.status}`); ok((await j(r))?.error?.code === 'STREAM_DISABLED', 'sai mã lỗi');
});
await check('W03', 'GET API đọc được và trả JSON, no-store', async () => {
  const r = await fetch(PORTAL + '/api/v1/tournaments'); ok(r.status === 200, `status=${r.status}`); ok((r.headers.get('content-type') || '').includes('json'), 'không phải JSON'); ok((r.headers.get('cache-control') || '').includes('no-store'), 'thiếu no-store');
});
await check('W04', 'API không hợp lệ trả JSON lỗi, không trả index.html', async () => {
  const r = await fetch(PORTAL + '/api/v1/khong-ton-tai'); ok(r.status === 404, `status=${r.status}`); ok((await j(r))?.error?.code === 'NOT_FOUND', 'không phải JSON lỗi');
});
await check('W05', 'Mutation với Origin lạ bị từ chối (403)', async () => {
  const r = await fetch(PORTAL + '/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: 'https://evil.example' }, body: '{"username":"x","password":"y"}' });
  ok(r.status === 403, `status=${r.status}`);
});
await check('W06', 'Body > 1 MiB bị chặn (413)', async () => {
  const r = await fetch(PORTAL + '/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: PORTAL }, body: JSON.stringify({ username: 'x', password: 'a'.repeat(1024 * 1024 + 10) }) });
  ok(r.status === 413, `status=${r.status}`);
});
await check('W07', 'Header danh tính do client giả không có tác dụng', async () => {
  const r = await fetch(PORTAL + '/api/v1/me', { headers: { 'X-Contest-Origin-Key': 'fake', 'X-Forwarded-For': '9.9.9.9' } });
  ok([200, 401].includes(r.status), `status=${r.status}`);
});
await check('W08', 'Bundle web không chứa origin key hay URL tunnel', async () => {
  for (const f of ['/', '/app.js', '/themes.js', '/net.js', '/runtime-config.js', '/styles.css', '/themes.css']) {
    const t = await (await fetch(PORTAL + f)).text();
    ok(!/trycloudflare\.com/i.test(t), `${f} chứa trycloudflare`); if (KEY) ok(!t.includes(KEY), `${f} chứa origin key`);
  }
});
if (ORIGIN) {
  await check('O01', 'Gọi thẳng origin không key bị từ chối (cần B05 phía backend)', async () => {
    const r = await fetch(ORIGIN + '/api/v1/tournaments'); ok([401, 403, 404].includes(r.status), `origin trả ${r.status} khi không có key`);
  });
  await check('O02', 'Gọi thẳng origin với header giả không qua được', async () => {
    const r = await fetch(ORIGIN + '/api/v1/tournaments', { headers: { 'X-Forwarded-Host': new URL(PORTAL).host, 'X-Contest-Origin-Key': 'sai' } }); ok([401, 403, 404].includes(r.status), `origin trả ${r.status}`);
  });
}
if (process.env.OTT_USER && process.env.OTT_PASS) {
  let cookie = '', csrf = '';
  await check('A01', 'Đăng nhập qua portal; cookie host-only, HttpOnly, Secure, SameSite=Lax', async () => {
    const r = await fetch(PORTAL + '/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: PORTAL }, body: JSON.stringify({ username: process.env.OTT_USER, password: process.env.OTT_PASS }) });
    ok(r.status === 200, `status=${r.status}`); const sc = r.headers.getSetCookie()[0] || ''; ok(sc, 'không có Set-Cookie');
    ok(/HttpOnly/i.test(sc) && /Secure/i.test(sc) && /SameSite=Lax/i.test(sc), 'thiếu cờ cookie'); ok(!/domain=/i.test(sc), 'cookie có Domain');
    cookie = sc.split(';')[0]; csrf = (await j(r))?.csrf_token; ok(csrf, 'thiếu csrf_token');
  });
  await check('A02', 'Mutation thiếu/sai CSRF bị từ chối', async () => {
    for (const hdr of [{}, { 'X-CSRF-Token': 'sai' }]) {
      const r = await fetch(PORTAL + '/api/v1/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: PORTAL, Cookie: cookie, ...hdr }, body: '{}' }); ok(r.status === 403, `status=${r.status}`);
    }
  });
  await check('A03', 'Phiên hợp lệ đọc được /me và logout thành công với CSRF đúng', async () => {
    const me = await fetch(PORTAL + '/api/v1/me', { headers: { Cookie: cookie } }); ok(me.status === 200, `me=${me.status}`);
    const out = await fetch(PORTAL + '/api/v1/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: PORTAL, Cookie: cookie, 'X-CSRF-Token': csrf }, body: '{}' }); ok(out.status === 200 || out.status === 204, `logout=${out.status}`);
  });
}

let commit = ''; try { commit = execSync('git rev-parse HEAD', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim(); } catch { /* không có git */ }
const report = { run_at: new Date().toISOString(), portal: PORTAL, origin_checked: Boolean(ORIGIN), commit, node: process.version, results };
mkdirSync('evidence', { recursive: true });
const file = `evidence/smoke-${report.run_at.replace(/[:.]/g, '-')}.json`;
writeFileSync(file, JSON.stringify(report, null, 2));
const failed = results.filter((r) => !r.pass).length;
console.log(`\n${results.length - failed}/${results.length} đạt. Biên bản: ${file}`);
process.exit(failed ? 1 : 0);
