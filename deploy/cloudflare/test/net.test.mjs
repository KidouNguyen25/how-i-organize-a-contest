import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createNet } from '../../../mockup/net.js';

function harness(opts = {}) {
  let t = 0; const q = [];
  const doc = { hidden: false, listeners: [], addEventListener(_e, f) { this.listeners.push(f); } };
  const net = createNet({ doc, now: () => t, setT: (f, ms) => { const e = { f, at: t + ms, off: false }; q.push(e); return e; }, clearT: (e) => { e.off = true; }, random: () => 0, ...opts });
  const advance = async (ms) => { const end = t + ms; for (;;) { const nx = q.filter((e) => !e.off && e.at <= end).sort((a, b) => a.at - b.at)[0]; if (!nx) break; nx.off = true; t = nx.at; await nx.f(); } t = end; };
  return { net, doc, advance, now: () => t };
}

test('tunnel: poll 2s bị nâng lên 10s', async () => {
  const h = harness({ tunnel: true, pollMs: 10000 }); let n = 0;
  h.net.poll(2000, () => { n++; });
  await h.advance(9999); assert.equal(n, 0); await h.advance(1); assert.equal(n, 1); await h.advance(20000); assert.equal(n, 3);
});
test('local: giữ nhịp gốc', async () => {
  const h = harness({ tunnel: false }); let n = 0; h.net.poll(2000, () => { n++; }); await h.advance(6000); assert.equal(n, 3);
});
test('tab ẩn: dừng gọi; hiện lại: chạy bù ngay', async () => {
  const h = harness({ tunnel: true }); let n = 0; h.net.poll(10000, () => { n++; });
  h.doc.hidden = true; await h.advance(60000); assert.equal(n, 0);
  h.doc.hidden = false; h.doc.listeners.forEach((f) => f()); await h.advance(1); assert.equal(n, 1);
});
test('backoff theo Retry-After: không gọi trước khi hết hạn', async () => {
  const h = harness({ tunnel: true }); let n = 0; h.net.poll(10000, () => { n++; });
  await h.advance(10000); assert.equal(n, 1);
  h.net.noteFailure(30);
  await h.advance(29000); assert.equal(n, 1); await h.advance(1000); assert.equal(n, 2);
});
test('backoff lũy thừa khi lỗi liên tiếp và reset khi thành công', async () => {
  const h = harness(); h.net.noteFailure(); const a = h.net.state.until - h.now();
  h.net.noteFailure(); const b = h.net.state.until - h.now(); assert.ok(b > a);
  h.net.noteOk(); assert.equal(h.net.state.until, 0); assert.equal(h.net.state.fails, 0);
});
test('stopPoll/stopAll dừng hẳn', async () => {
  const h = harness({ tunnel: true }); let n = 0; const p = h.net.poll(10000, () => { n++; });
  h.net.stopPoll(p); await h.advance(50000); assert.equal(n, 0);
  h.net.poll(10000, () => { n++; }); h.net.stopAll(); await h.advance(50000); assert.equal(n, 0);
});
test('không chạy chồng: fn chậm thì lần sau chỉ đặt lịch sau khi xong', async () => {
  const h = harness({ tunnel: true }); let running = 0, max = 0;
  h.net.poll(10000, async () => { running++; max = Math.max(max, running); await Promise.resolve(); running--; });
  await h.advance(100000); assert.equal(max, 1);
});
