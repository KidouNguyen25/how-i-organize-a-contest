// Lịch polling dùng chung cho web client (C05).
// - Chế độ "tunnel" (Cloudflare Quick Tunnel): không dùng SSE, mọi poll mạng chạy tối đa 1 lần/pollMs.
// - Tạm dừng khi tab ẩn, chạy bù ngay khi tab hiện lại.
// - Backoff khi gặp 429/502/503/504 hoặc lỗi mạng (ưu tiên Retry-After); các poll cùng dừng chờ.
export function createNet({
  tunnel = false, pollMs = 10000, doc = globalThis.document,
  now = Date.now, setT = setTimeout, clearT = clearTimeout, random = Math.random,
} = {}) {
  const state = { fails: 0, until: 0 };
  const handles = new Set();

  const noteOk = () => { state.fails = 0; state.until = 0; };
  const noteFailure = (retryAfterSec = 0) => {
    state.fails = Math.min(state.fails + 1, 8);
    let delay = retryAfterSec > 0 ? retryAfterSec * 1000 : Math.min(60000, 2000 * 2 ** (state.fails - 1));
    delay = Math.min(delay, 120000) + Math.floor(random() * 500);
    state.until = Math.max(state.until, now() + delay);
  };

  function poll(ms, fn) {
    const h = { stopped: false, paused: false, running: false, t: null, base: tunnel ? Math.max(ms, pollMs) : ms };
    h.loop = async () => {
      h.t = null;
      if (h.stopped) return;
      if (doc && doc.hidden) { h.paused = true; return; }
      const wait = state.until - now();
      if (wait > 0) { h.t = setT(h.loop, wait); return; }
      h.running = true;
      try { await fn(); } catch { /* fn tự xử lý lỗi; api() đã ghi nhận backoff */ }
      h.running = false;
      if (h.stopped) return;
      h.t = setT(h.loop, Math.max(h.base, state.until - now()));
    };
    h.t = setT(h.loop, h.base);
    handles.add(h);
    return h;
  }
  function stopPoll(h) {
    if (!h) return;
    h.stopped = true;
    if (h.t) clearT(h.t);
    h.t = null;
    handles.delete(h);
  }
  const stopAll = () => [...handles].forEach(stopPoll);
  function onVisible() {
    if (doc && doc.hidden) return;
    for (const h of handles) if (h.paused && !h.stopped) { h.paused = false; if (!h.t && !h.running) h.t = setT(h.loop, 0); }
  }
  if (doc && doc.addEventListener) doc.addEventListener('visibilitychange', onVisible);
  return { state, noteOk, noteFailure, poll, stopPoll, stopAll, onVisible };
}
